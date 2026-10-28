"""On the road: arrival at a stop, proof of delivery, failed delivery, and trip completion.

The driver's device records these offline and sends them later, so each carries the device time it happened
(`captured_at`) and is applied once through the sync command log. Evidence (signature, photos) is uploaded first
and linked to the stop; a POD names the documents it uses.

A shipment is delivered when the recipient kept at least one of its units (delivered or damaged). If every unit
was refused or missing, or the stop failed, the shipment failed; completing the trip brings failed shipments back to
the warehouse, ready to plan again.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.platform import documents, exceptions_queue, outbox
from apps.platform.apiutils import Problem
from apps.platform.models import Document, DocumentKind, Severity

from .models import (
    RECEIVED,
    ArrivalSource,
    DeliveryAttempt,
    DeliveryOutcome,
    DeliveryPhoto,
    FailureReason,
    Shipment,
    ShipmentStatus,
    ShipmentUnit,
    Stop,
    StopAction,
    StopShipment,
    StopStatus,
    Trip,
    TripStatus,
    UnitDeliveryStatus,
)
from .trips import normalise_sscc

CLOCK_SKEW = timedelta(minutes=10)
EVIDENCE_KINDS = (DocumentKind.POD_SIGNATURE, DocumentKind.POD_PHOTO)
LINK = "tms.stop"


@dataclass(frozen=True, slots=True)
class Where:
    """When and where the device captured something."""

    at: datetime | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None


def _on_trip(trip_id: uuid.UUID, stop_id: uuid.UUID, *, lock: bool = True) -> tuple[Trip, Stop]:
    trips = Trip.objects.select_for_update() if lock else Trip.objects.all()
    trip = trips.filter(pk=trip_id).first()
    stop = Stop.objects.filter(pk=stop_id, trip_id=trip_id).first() if trip else None
    if stop is None:
        raise Problem(404, "Not found")
    if trip.status != TripStatus.DISPATCHED:
        raise Problem(409, "Trip not on the road", f"{trip.number} is {trip.status}.")
    if stop.seq == 1:
        raise Problem(409, "Pickup stop", "Stop 1 is the warehouse; it was done at dispatch.")
    return trip, stop


def _when(trip: Trip, at: datetime | None) -> datetime:
    now = timezone.now()
    if at is None:
        return now
    if at > now + CLOCK_SKEW or (trip.dispatched_at and at < trip.dispatched_at - CLOCK_SKEW):
        raise Problem(422, "Implausible time", "The device clock is off; check its date and time.")
    return at


def stop_client_id(stop: Stop) -> uuid.UUID:
    """A delivery address belongs to one client, so a drop stop's shipments all share it."""
    client_id = StopShipment.objects.filter(stop=stop).values_list("client_id", flat=True).first()
    if client_id is None:
        raise Problem(409, "Nothing to drop", "This stop has no shipments.")
    return client_id


# --- Evidence ----------------------------------------------------------------------------------------------------


@transaction.atomic
def store_evidence(trip_id: uuid.UUID, stop_id: uuid.UUID, upload, *, kind: str, where: Where, user_id) -> Document:
    """Save a signature or photo for a stop. Sending the same bytes again returns the same document, so a device
    can retry an upload whose response it lost."""
    if kind not in EVIDENCE_KINDS:
        raise Problem(422, "Invalid kind", "Evidence is a pod_signature or a pod_photo.")
    trip, stop = _on_trip(trip_id, stop_id, lock=False)
    client_id = stop_client_id(stop)
    digest = documents.sha256_of(upload)
    existing = Document.objects.filter(linked_type=LINK, linked_id=str(stop.pk), kind=kind, sha256=digest).first()
    if existing is not None:
        return existing
    return documents.store(
        upload,
        kind=kind,
        client_id=client_id,
        captured_by=user_id,
        captured_at=_when(trip, where.at),
        linked_type=LINK,
        linked_id=str(stop.pk),
        latitude=where.latitude,
        longitude=where.longitude,
    )


def _evidence(stop: Stop, ids: list[uuid.UUID], kind: str) -> list[Document]:
    found = list(Document.objects.filter(pk__in=ids, kind=kind, linked_type=LINK, linked_id=str(stop.pk)))
    if len(found) != len(set(ids)):
        raise Problem(422, "Unknown evidence", f"Every {kind.replace('_', ' ')} must be uploaded for this stop.")
    return found


# --- Arrival -----------------------------------------------------------------------------------------------------


def _arrive(stop: Stop, at: datetime, source: str) -> bool:
    if stop.status != StopStatus.PENDING:
        return False
    stop.status, stop.arrived_at, stop.arrival_source = StopStatus.ARRIVED, at, source
    stop.save()
    return True


@transaction.atomic
def arrive(trip_id: uuid.UUID, stop_id: uuid.UUID, *, where: Where, source: str = ArrivalSource.MANUAL) -> Stop:
    trip, stop = _on_trip(trip_id, stop_id)
    if _arrive(stop, _when(trip, where.at), source):
        outbox.emit(
            "tms.stop.arrived",
            aggregate_type="tms.trip",
            aggregate_id=trip.pk,
            client_id=stop_client_id(stop),
            site_id=trip.site_id,
            payload={
                "trip_id": str(trip.pk),
                "stop_id": str(stop.pk),
                "seq": stop.seq,
                "site_id": str(stop.site_id),
                "arrived_at": stop.arrived_at.isoformat(),
                "source": source,
            },
        )
    return stop


# --- Proof of delivery -------------------------------------------------------------------------------------------


def _drop_shipments(trip: Trip, stop: Stop) -> list[Shipment]:
    ids = StopShipment.objects.filter(trip=trip, stop=stop, action=StopAction.DROP).values_list("shipment_id")
    return list(Shipment.objects.select_for_update().filter(pk__in=ids).order_by("pk"))


def _require_open(stop: Stop) -> None:
    if stop.status == StopStatus.DONE:
        raise Problem(409, "Already recorded", "This stop already has its delivery recorded.")


@transaction.atomic
def record_pod(
    trip_id: uuid.UUID,
    stop_id: uuid.UUID,
    *,
    recipient_name: str,
    where: Where,
    signature_id: uuid.UUID | None,
    photo_ids: list[uuid.UUID],
    units: dict[str, str] | None,
    note: str = "",
    user_id: uuid.UUID | None,
) -> DeliveryAttempt:
    """`units` maps SSCC to its outcome. Units not named are missing; leave `units` out (a POD keyed in from a
    paper slip) to mark every unit delivered."""
    trip, stop = _on_trip(trip_id, stop_id)
    _require_open(stop)
    at = _when(trip, where.at)
    if not recipient_name.strip():
        raise Problem(422, "Recipient required", "Record who received the goods.")
    signature = _evidence(stop, [signature_id], DocumentKind.POD_SIGNATURE)[0] if signature_id else None
    photos = _evidence(stop, photo_ids, DocumentKind.POD_PHOTO)
    if signature is None and not photos:
        raise Problem(422, "Evidence required", "A proof of delivery needs a signature, a photo, or both.")

    shipments = _drop_shipments(trip, stop)
    by_id = {s.pk: s for s in shipments}
    stop_units = list(ShipmentUnit.objects.select_for_update().filter(shipment_id__in=by_id).order_by("sscc"))
    if units is not None:
        wanted = {normalise_sscc(code): status for code, status in units.items()}
        strangers = sorted(set(wanted) - {u.sscc for u in stop_units})
        if strangers:
            raise Problem(422, "Not for this stop", f"Not dropped here: {', '.join(strangers)}.")
        if bad := sorted({s for s in wanted.values() if s not in UnitDeliveryStatus.values}):
            raise Problem(422, "Invalid outcome", f"Unknown unit outcome {', '.join(bad)}.")
    for unit in stop_units:
        unit.delivery_status = UnitDeliveryStatus.DELIVERED if units is None else wanted.get(unit.sscc, "missing")
        unit.save()

    client_id = stop_client_id(stop)
    _arrive(stop, at, ArrivalSource.POD)
    stop.status = StopStatus.DONE
    stop.save()
    attempt = DeliveryAttempt.objects.create(
        stop=stop,
        trip=trip,
        client_id=client_id,
        outcome=DeliveryOutcome.DELIVERED,
        recipient_name=recipient_name.strip()[:120],
        note=note.strip(),
        captured_at=at,
        captured_by=user_id,
        latitude=where.latitude,
        longitude=where.longitude,
        signature=signature,
    )
    for photo in photos:
        DeliveryPhoto.objects.create(attempt=attempt, client_id=client_id, document=photo)

    exceptions = [{"sscc": u.sscc, "status": u.delivery_status} for u in stop_units if u.delivery_status != "delivered"]
    for shipment in shipments:
        mine = [u for u in stop_units if u.shipment_id == shipment.pk]
        if any(u.delivery_status in RECEIVED for u in mine):
            _delivered(shipment, trip, attempt, mine, photos)
        else:
            _failed(shipment, trip, attempt, FailureReason.REFUSED if mine else FailureReason.OTHER, note)
    if exceptions:
        exceptions_queue.raise_case(
            kind="delivery.exception",
            severity=Severity.MEDIUM,
            summary=f"{trip.number} stop {stop.seq}: {len(exceptions)} unit(s) not delivered cleanly",
            detail={"trip_id": str(trip.pk), "stop_id": str(stop.pk), "units": exceptions},
            client_id=client_id,
            site_id=trip.site_id,
            entity_type="tms.stop",
            entity_id=str(stop.pk),
            dedupe_key=f"delivery.exception:{stop.pk}",
        )
    return attempt


def _delivered_lines(units: list[ShipmentUnit]) -> list[dict]:
    totals: dict[tuple[str, str], Decimal] = {}
    for unit in units:
        if unit.delivery_status in RECEIVED:
            for content in unit.contents:
                key = (content["order_line_id"], content["item_id"])
                totals[key] = totals.get(key, Decimal(0)) + Decimal(content["qty"])
    return [
        {"order_line_id": line, "item_id": item, "qty_delivered": str(qty)}
        for (line, item), qty in sorted(totals.items())
    ]


def _base_payload(shipment: Shipment, trip: Trip, attempt: DeliveryAttempt) -> dict:
    return {
        "shipment_id": str(shipment.pk),
        "number": shipment.number,
        "sales_order_id": str(shipment.sales_order_id),
        "order_number": shipment.order_number,
        "client_ref": shipment.client_ref,
        "trip_id": str(trip.pk),
        "trip_number": trip.number,
        "stop_id": str(attempt.stop_id),
        "captured_at": attempt.captured_at.isoformat(),
        "latitude": str(attempt.latitude) if attempt.latitude is not None else None,
        "longitude": str(attempt.longitude) if attempt.longitude is not None else None,
    }


def _delivered(shipment, trip, attempt: DeliveryAttempt, units: list[ShipmentUnit], photos: list[Document]) -> None:
    shipment.status, shipment.delivered_at = ShipmentStatus.DELIVERED, attempt.captured_at
    shipment.save()
    outbox.emit(
        "tms.shipment.delivered",
        aggregate_type="tms.shipment",
        aggregate_id=shipment.pk,
        client_id=shipment.client_id,
        site_id=shipment.site_id,
        payload=_base_payload(shipment, trip, attempt)
        | {
            "recipient_name": attempt.recipient_name,
            "signature_document_id": str(attempt.signature_id) if attempt.signature_id else None,
            "photo_document_ids": [str(p.pk) for p in photos],
            "units": [{"sscc": u.sscc, "status": u.delivery_status} for u in units],
            "lines": _delivered_lines(units),
        },
    )


def _failed(shipment, trip, attempt: DeliveryAttempt, reason: str, note: str) -> None:
    shipment.status = ShipmentStatus.FAILED
    shipment.save()
    outbox.emit(
        "tms.shipment.delivery_failed",
        aggregate_type="tms.shipment",
        aggregate_id=shipment.pk,
        client_id=shipment.client_id,
        site_id=shipment.site_id,
        payload=_base_payload(shipment, trip, attempt) | {"reason": reason, "note": note},
    )


# --- Failed delivery ---------------------------------------------------------------------------------------------


@transaction.atomic
def record_failure(
    trip_id: uuid.UUID,
    stop_id: uuid.UUID,
    *,
    reason: str,
    note: str,
    where: Where,
    photo_ids: list[uuid.UUID],
    user_id: uuid.UUID | None,
) -> DeliveryAttempt:
    trip, stop = _on_trip(trip_id, stop_id)
    _require_open(stop)
    if reason not in FailureReason.values:
        raise Problem(422, "Invalid reason", f"Use one of {', '.join(FailureReason.values)}.")
    if reason == FailureReason.OTHER and not note.strip():
        raise Problem(422, "Note required", "Say what went wrong.")
    at = _when(trip, where.at)
    photos = _evidence(stop, photo_ids, DocumentKind.POD_PHOTO)
    client_id = stop_client_id(stop)
    stop.status = StopStatus.DONE
    stop.save()
    attempt = DeliveryAttempt.objects.create(
        stop=stop,
        trip=trip,
        client_id=client_id,
        outcome=DeliveryOutcome.FAILED,
        reason=reason,
        note=note.strip(),
        captured_at=at,
        captured_by=user_id,
        latitude=where.latitude,
        longitude=where.longitude,
    )
    for photo in photos:
        DeliveryPhoto.objects.create(attempt=attempt, client_id=client_id, document=photo)
    shipments = _drop_shipments(trip, stop)
    for shipment in shipments:
        _failed(shipment, trip, attempt, reason, attempt.note)
    exceptions_queue.raise_case(
        kind="delivery.failed",
        severity=Severity.MEDIUM,
        summary=f"{trip.number} stop {stop.seq}: delivery failed ({FailureReason(reason).label})",
        detail={
            "trip_id": str(trip.pk),
            "stop_id": str(stop.pk),
            "reason": reason,
            "note": attempt.note,
            "shipments": [s.number for s in shipments],
        },
        client_id=client_id,
        site_id=trip.site_id,
        entity_type="tms.stop",
        entity_id=str(stop.pk),
        dedupe_key=f"delivery.failed:{stop.pk}",
    )
    return attempt


# --- Completion --------------------------------------------------------------------------------------------------


@transaction.atomic
def complete(trip_id: uuid.UUID, *, at: datetime | None, user_id: uuid.UUID | None) -> Trip:
    """Back at base. Failed shipments are unloaded and ready to plan again."""
    trip = Trip.objects.select_for_update().filter(pk=trip_id).first()
    if trip is None:
        raise Problem(404, "Not found")
    if trip.status == TripStatus.COMPLETED:
        return trip
    if trip.status != TripStatus.DISPATCHED:
        raise Problem(409, "Trip not on the road", f"{trip.number} is {trip.status}.")
    open_stops = list(trip.stops.filter(seq__gt=1).exclude(status=StopStatus.DONE).values_list("seq", flat=True))
    if open_stops:
        raise Problem(409, "Stops still open", f"Record a delivery or a failure at stop(s) {open_stops}.")
    when = _when(trip, at)
    shipment_ids = StopShipment.objects.filter(trip=trip, action=StopAction.DROP).values_list("shipment_id")
    failed = list(
        Shipment.objects.select_for_update().filter(pk__in=shipment_ids, status=ShipmentStatus.FAILED).order_by("pk")
    )
    for unit in ShipmentUnit.objects.select_for_update().filter(shipment__in=failed, loaded_trip=trip):
        unit.loaded_trip, unit.loaded_at, unit.loaded_by, unit.delivery_status = None, None, None, ""
        unit.save()
    for shipment in failed:
        shipment.status = ShipmentStatus.READY
        shipment.save()
    trip.status, trip.completed_at = TripStatus.COMPLETED, when
    trip.save()
    attempts = DeliveryAttempt.objects.filter(trip=trip)
    outbox.emit(
        "tms.trip.completed",
        aggregate_type="tms.trip",
        aggregate_id=trip.pk,
        site_id=trip.site_id,
        payload={
            "trip_id": str(trip.pk),
            "number": trip.number,
            "completed_at": when.isoformat(),
            "completed_by": str(user_id) if user_id else None,
            "stops_delivered": attempts.filter(outcome=DeliveryOutcome.DELIVERED).count(),
            "stops_failed": attempts.filter(outcome=DeliveryOutcome.FAILED).count(),
            "returned_shipment_ids": [str(s.pk) for s in failed],
        },
    )
    return trip
