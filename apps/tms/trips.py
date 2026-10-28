"""Trips: plan stops, assign a vehicle and driver or a carrier, load handling units, and dispatch.

A trip is planned at one warehouse. Its first stop is always the pickup there, carrying every shipment on the trip;
each later stop drops shipments at their delivery address. While planning, stops can be replaced freely; dispatch
freezes them. Dispatch needs a resource, every shipment packed, and every one of its units scanned onto this trip.

Writes to one trip are serialised on its row; shipments are locked in key order before they move between trips, so
two dispatchers can't put the same shipment on two trips.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.masterdata.models import Party, PartyRoleType, Person, Site, SiteKind, Vehicle
from apps.platform import outbox
from apps.platform.apiutils import Problem
from apps.platform.numbers import next_number

from .models import (
    PLANNABLE,
    Shipment,
    ShipmentStatus,
    ShipmentUnit,
    Stop,
    StopAction,
    StopShipment,
    Trip,
    TripStatus,
)
from .shipments import remove_from_trip

ACTIVE = (TripStatus.PLANNING, TripStatus.DISPATCHED)


@dataclass(frozen=True, slots=True)
class StopIn:
    site_id: uuid.UUID
    shipment_ids: list[uuid.UUID] = field(default_factory=list)
    window_from: datetime | None = None
    window_to: datetime | None = None


def _lock(trip_id: uuid.UUID) -> Trip:
    trip = Trip.objects.select_for_update().filter(pk=trip_id).first()
    if trip is None:
        raise Problem(404, "Not found")
    return trip


def _require_planning(trip: Trip) -> None:
    if trip.status != TripStatus.PLANNING:
        raise Problem(409, "Trip is not being planned", f"{trip.number} is {trip.status}.")


@transaction.atomic
def create_trip(*, site: Site, planned_start: datetime, created_by: uuid.UUID | None) -> Trip:
    if site.kind != SiteKind.WAREHOUSE or not site.is_active:
        raise Problem(422, "Not a warehouse", f"{site.code} does not dispatch trips.")
    return Trip.objects.create(
        site=site, planned_start=planned_start, created_by=created_by, number=next_number("trip", "TRP")
    )


@transaction.atomic
def set_stops(trip_id: uuid.UUID, stops: list[StopIn]) -> Trip:
    """Replace the drop stops (in visiting order). The pickup at the trip's warehouse is added as stop 1."""
    trip = _lock(trip_id)
    _require_planning(trip)
    wanted = [sid for stop in stops for sid in stop.shipment_ids]
    if len(wanted) != len(set(wanted)):
        raise Problem(422, "Shipment on two stops", "Each shipment is dropped at exactly one stop.")
    if any(not stop.shipment_ids for stop in stops):
        raise Problem(422, "Empty stop", "Every stop needs at least one shipment to drop.")
    for stop in stops:
        if stop.window_from and stop.window_to and stop.window_to < stop.window_from:
            raise Problem(422, "Invalid window", "A stop's window must end after it starts.")

    current = set(StopShipment.objects.filter(trip=trip).values_list("shipment_id", flat=True))
    locked = {s.pk: s for s in Shipment.objects.select_for_update().filter(pk__in=set(wanted) | current).order_by("pk")}
    missing = [str(sid) for sid in wanted if sid not in locked]
    if missing:
        raise Problem(422, "Unknown shipment", f"No shipment {', '.join(missing)}.")
    for stop in stops:
        for sid in stop.shipment_ids:
            shipment = locked[sid]
            if shipment.site_id != trip.site_id:
                raise Problem(422, "Wrong warehouse", f"{shipment.number} ships from another warehouse.")
            if shipment.ship_to_id != stop.site_id:
                raise Problem(422, "Wrong stop", f"{shipment.number} is delivered to another address.")
            if sid not in current and shipment.status not in PLANNABLE:
                raise Problem(409, "Shipment not available", f"{shipment.number} is {shipment.status}.")

    released = current - set(wanted)
    if ShipmentUnit.objects.filter(shipment_id__in=released, loaded_trip=trip).exists():
        raise Problem(409, "Unload first", "Units of a shipment you are taking off are loaded on this trip.")

    StopShipment.objects.filter(trip=trip).delete()
    Stop.objects.filter(trip=trip).delete()
    if stops:
        pickup = Stop.objects.create(trip=trip, seq=1, site_id=trip.site_id)
        for sid in wanted:
            _link(trip, pickup, locked[sid], StopAction.PICKUP)
        for seq, stop in enumerate(stops, start=2):
            drop = Stop.objects.create(
                trip=trip, seq=seq, site_id=stop.site_id, window_from=stop.window_from, window_to=stop.window_to
            )
            for sid in stop.shipment_ids:
                _link(trip, drop, locked[sid], StopAction.DROP)
    for sid in wanted:
        _set_status(locked[sid], ShipmentStatus.PLANNED)
    for sid in released:
        _release(locked[sid])
    trip.save()  # bumps updated_at so the board sees the change
    return trip


def _link(trip: Trip, stop: Stop, shipment: Shipment, action: str) -> None:
    StopShipment.objects.create(trip=trip, stop=stop, shipment=shipment, client_id=shipment.client_id, action=action)


def _set_status(shipment: Shipment, status: str) -> None:
    if shipment.status != status:
        shipment.status = status
        shipment.save()


def _release(shipment: Shipment) -> None:
    if shipment.status == ShipmentStatus.PLANNED:
        _set_status(shipment, ShipmentStatus.READY if shipment.packed_at else ShipmentStatus.DRAFT)


# --- Resources ---------------------------------------------------------------------------------------------------


def _check_not_expired(on: date, label: str, **expiries: date | None) -> None:
    expired = [name.replace("_", " ") for name, value in expiries.items() if value is not None and value < on]
    if expired:
        raise Problem(422, "Expired", f"{label}: {', '.join(expired)} expired before the trip starts.")


@transaction.atomic
def assign_own(trip_id: uuid.UUID, *, vehicle_id: uuid.UUID, driver_id: uuid.UUID) -> Trip:
    trip = _lock(trip_id)
    _require_planning(trip)
    on = timezone.localtime(trip.planned_start).date()
    vehicle = Vehicle.objects.filter(pk=vehicle_id, is_active=True).first()
    if vehicle is None:
        raise Problem(422, "Unknown vehicle", "No active vehicle with that id.")
    if vehicle.carrier_id is not None:
        raise Problem(422, "Not own fleet", f"{vehicle.registration} belongs to a carrier; assign the carrier.")
    _check_not_expired(
        on, vehicle.registration, licence_disc=vehicle.licence_disc_expiry, roadworthy=vehicle.roadworthy_expiry
    )
    driver = Person.objects.filter(pk=driver_id, is_active=True, is_driver=True).first()
    if driver is None:
        raise Problem(422, "Unknown driver", "No active driver with that id.")
    if driver.carrier_id is not None:
        raise Problem(422, "Not own driver", f"{driver} drives for a carrier; assign the carrier.")
    _check_not_expired(on, str(driver), licence=driver.licence_expiry, prdp=driver.prdp_expiry)
    trip.vehicle, trip.driver = vehicle, driver
    trip.carrier, trip.agreed_cost = None, None
    trip.save()
    return trip


@transaction.atomic
def assign_carrier(trip_id: uuid.UUID, *, carrier_id: uuid.UUID, agreed_cost: Decimal, currency: str = "ZAR") -> Trip:
    trip = _lock(trip_id)
    _require_planning(trip)
    carrier = Party.objects.filter(pk=carrier_id, is_active=True, roles__role=PartyRoleType.CARRIER).first()
    if carrier is None:
        raise Problem(422, "Unknown carrier", "No active carrier with that id.")
    if agreed_cost <= 0:
        raise Problem(422, "Agreed cost required", "A subcontracted trip needs the agreed cost.")
    trip.carrier, trip.agreed_cost, trip.currency = carrier, agreed_cost, currency.upper()
    trip.vehicle, trip.driver = None, None
    trip.save()
    return trip


# --- Loading -----------------------------------------------------------------------------------------------------


def normalise_sscc(raw: str) -> str:
    """Accept "(00) 0061…", a scanned "000061…" with its AI, or the bare 18 digits."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[2:] if len(digits) == 20 and digits.startswith("00") else digits


@transaction.atomic
def load(trip_id: uuid.UUID, sscc: str, *, user_id: uuid.UUID | None) -> ShipmentUnit:
    trip = _lock(trip_id)
    _require_planning(trip)
    code = normalise_sscc(sscc)
    unit = ShipmentUnit.objects.select_for_update().select_related("shipment").filter(sscc=code).first()
    if unit is None:
        raise Problem(404, "Unknown unit", f"No packed unit with SSCC {code}.")
    if unit.loaded_trip_id == trip.pk:
        return unit  # a repeated scan
    if unit.loaded_trip_id is not None:
        raise Problem(409, "Loaded on another trip", f"{code} is already on {unit.loaded_trip.number}.")
    if not StopShipment.objects.filter(trip=trip, shipment_id=unit.shipment_id).exists():
        raise Problem(
            409, "Not on this trip", f"{code} belongs to {unit.shipment.number}, which is not on {trip.number}."
        )
    unit.loaded_trip, unit.loaded_at, unit.loaded_by = trip, timezone.now(), user_id
    unit.save()
    return unit


@transaction.atomic
def unload(trip_id: uuid.UUID, sscc: str) -> ShipmentUnit:
    trip = _lock(trip_id)
    _require_planning(trip)
    unit = ShipmentUnit.objects.select_for_update().filter(sscc=normalise_sscc(sscc), loaded_trip=trip).first()
    if unit is None:
        raise Problem(404, "Not loaded", "That unit is not loaded on this trip.")
    unit.loaded_trip, unit.loaded_at, unit.loaded_by = None, None, None
    unit.save()
    return unit


# --- Dispatch ----------------------------------------------------------------------------------------------------


def _busy(trip: Trip) -> Q:
    """Other dispatched trips using this trip's vehicle or driver."""
    clash = Q()
    if trip.vehicle_id:
        clash |= Q(vehicle_id=trip.vehicle_id)
    if trip.driver_id:
        clash |= Q(driver_id=trip.driver_id)
    return clash


@transaction.atomic
def dispatch(trip_id: uuid.UUID, *, user_id: uuid.UUID | None) -> Trip:
    trip = _lock(trip_id)
    _require_planning(trip)
    stops = list(trip.stops.order_by("seq"))
    if len(stops) < 2:
        raise Problem(409, "No stops", f"{trip.number} has nothing to deliver.")
    if trip.carrier_id is None and trip.vehicle_id is None:
        raise Problem(409, "No resource", "Assign a vehicle and driver, or a carrier, first.")
    if trip.vehicle_id and Trip.objects.filter(_busy(trip), status=TripStatus.DISPATCHED).exists():
        raise Problem(409, "Resource busy", "The vehicle or driver is still out on another trip.")

    links = list(StopShipment.objects.filter(trip=trip, action=StopAction.DROP).select_related("stop"))
    shipment_ids = sorted({link.shipment_id for link in links})
    shipments = list(Shipment.objects.select_for_update().filter(pk__in=shipment_ids).order_by("pk"))
    unpacked = [s.number for s in shipments if s.packed_at is None or s.status != ShipmentStatus.PLANNED]
    if unpacked:
        raise Problem(409, "Not packed", f"Still being packed: {', '.join(unpacked)}.")
    not_loaded = list(
        ShipmentUnit.objects.filter(shipment_id__in=shipment_ids)
        .filter(Q(loaded_trip__isnull=True) | ~Q(loaded_trip=trip))
        .values_list("sscc", flat=True)
    )
    if not_loaded:
        raise Problem(
            409, "Not loaded", f"{len(not_loaded)} unit(s) not scanned onto the trip: {', '.join(not_loaded)}."
        )
    if not ShipmentUnit.objects.filter(loaded_trip=trip).exists():
        raise Problem(409, "Not loaded", "Nothing is loaded on this trip.")

    now = timezone.now()
    for shipment in shipments:
        shipment.status = ShipmentStatus.IN_TRANSIT
        shipment.save()
    trip.status, trip.dispatched_at, trip.dispatched_by = TripStatus.DISPATCHED, now, user_id
    trip.save()
    by_stop: dict[uuid.UUID, list[str]] = {}
    for link in links:
        by_stop.setdefault(link.stop_id, []).append(str(link.shipment_id))
    outbox.emit(
        "tms.trip.dispatched",
        aggregate_type="tms.trip",
        aggregate_id=trip.pk,
        site_id=trip.site_id,
        payload={
            "trip_id": str(trip.pk),
            "number": trip.number,
            "vehicle_id": str(trip.vehicle_id) if trip.vehicle_id else None,
            "driver_id": str(trip.driver_id) if trip.driver_id else None,
            "carrier_id": str(trip.carrier_id) if trip.carrier_id else None,
            "stops": [
                {
                    "stop_id": str(stop.pk),
                    "seq": stop.seq,
                    "site_id": str(stop.site_id),
                    "shipment_ids": sorted(by_stop.get(stop.pk, [])),
                }
                for stop in stops
            ],
            "shipments": [
                {
                    "shipment_id": str(s.pk),
                    "number": s.number,
                    "sales_order_id": str(s.sales_order_id),
                    "client_id": str(s.client_id),
                }
                for s in shipments
            ],
            "dispatched_at": now.isoformat(),
        },
    )
    return trip


@transaction.atomic
def cancel(trip_id: uuid.UUID) -> Trip:
    trip = _lock(trip_id)
    if trip.status == TripStatus.CANCELLED:
        return trip
    _require_planning(trip)
    if ShipmentUnit.objects.filter(loaded_trip=trip).exists():
        raise Problem(409, "Unload first", f"Units are still loaded on {trip.number}.")
    shipment_ids = list(StopShipment.objects.filter(trip=trip).values_list("shipment_id", flat=True).distinct())
    remove_from_trip(trip.pk, shipment_ids)
    for shipment in Shipment.objects.select_for_update().filter(pk__in=shipment_ids).order_by("pk"):
        _release(shipment)
    trip.status, trip.cancelled_at = TripStatus.CANCELLED, timezone.now()
    trip.save()
    return trip
