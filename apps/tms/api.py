import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from django.db.models import Count, Prefetch, Q, QuerySet, Sum
from django.utils import timezone
from ninja import Field, File, Form, Router, Schema, Status
from ninja.files import UploadedFile

from apps.masterdata.models import Site
from apps.platform.apiutils import Problem, get_scoped, paginate, principal_of, require_roles, require_staff
from apps.platform.auth import Principal
from apps.platform.models import Role

from . import delivery, trips
from .models import (
    DeliveryAttempt,
    FailureReason,
    Shipment,
    ShipmentStatus,
    ShipmentUnit,
    StopAction,
    StopShipment,
    Trip,
    TripStatus,
    UnitDeliveryStatus,
)

router = Router(tags=["transport"])

DISPATCHERS = (Role.DISPATCHER, Role.SITE_MANAGER)
LOADERS = (*DISPATCHERS, Role.WAREHOUSE_SUPERVISOR, Role.WAREHOUSE_OPERATOR)


# --- Shipments ---------------------------------------------------------------------------------------------------


class ShipmentLineOut(Schema):
    order_line_id: uuid.UUID
    item_id: uuid.UUID
    sku: str
    qty_ordered: Decimal
    qty_packed: Decimal

    @staticmethod
    def resolve_sku(obj) -> str:
        return obj.item.sku


class ShipmentUnitOut(Schema):
    id: uuid.UUID
    sscc: str
    kind: str
    weight_kg: Decimal
    volume_m3: Decimal | None
    cartons: int
    loaded_trip_id: uuid.UUID | None
    loaded_at: datetime | None


class ShipmentSummary(Schema):
    id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    ship_to_id: uuid.UUID
    ship_to_name: str
    sales_order_id: uuid.UUID
    order_number: str
    client_ref: str
    required_by: date | None
    status: str
    packed_at: datetime | None
    weight_kg: Decimal
    volume_m3: Decimal

    @staticmethod
    def resolve_ship_to_name(obj) -> str:
        return obj.ship_to.name


class ShipmentOut(ShipmentSummary):
    lines: list[ShipmentLineOut]
    units: list[ShipmentUnitOut]

    @staticmethod
    def resolve_lines(obj: Shipment):
        return list(obj.lines.select_related("item").order_by("item__sku"))

    @staticmethod
    def resolve_units(obj: Shipment):
        return list(obj.units.order_by("sscc"))


class ShipmentPage(Schema):
    items: list[ShipmentSummary]
    next_cursor: uuid.UUID | None


def _shipments(principal: Principal) -> QuerySet:
    # Staff see their sites' shipments; a client sees its own at every site.
    qs = Shipment.objects.select_related("ship_to")
    return principal.scope(qs, site_field=None if principal.is_client_scoped else "site_id")


@router.get("/shipments", response=ShipmentPage)
def list_shipments(
    request,
    site_id: uuid.UUID | None = None,
    status: ShipmentStatus | None = None,
    sales_order_id: uuid.UUID | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = 50,
):
    qs = _shipments(principal_of(request))
    if site_id:
        qs = qs.filter(site_id=site_id)
    if status:
        qs = qs.filter(status=status)
    if sales_order_id:
        qs = qs.filter(sales_order_id=sales_order_id)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/shipments/{shipment_id}", response=ShipmentOut)
def get_shipment(request, shipment_id: uuid.UUID):
    return get_scoped(_shipments(principal_of(request)), shipment_id)


# --- Trips -------------------------------------------------------------------------------------------------------


class TripIn(Schema):
    site_id: uuid.UUID
    planned_start: datetime


class StopIn(Schema):
    site_id: uuid.UUID
    shipment_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=200)
    window_from: datetime | None = None
    window_to: datetime | None = None


class StopsIn(Schema):
    stops: list[StopIn] = Field(..., max_length=100, description="Drop stops in visiting order; [] clears the trip")


class AssignIn(Schema):
    """Either vehicle_id and driver_id (own fleet), or carrier_id and agreed_cost (subcontracted)."""

    vehicle_id: uuid.UUID | None = None
    driver_id: uuid.UUID | None = None
    carrier_id: uuid.UUID | None = None
    agreed_cost: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)] | None = None
    currency: str = Field("ZAR", min_length=3, max_length=3)


class LoadIn(Schema):
    sscc: str = Field(..., min_length=18, max_length=40)


class StopShipmentOut(Schema):
    shipment_id: uuid.UUID
    number: str
    action: str


class StopOut(Schema):
    id: uuid.UUID
    seq: int
    site_id: uuid.UUID
    site_code: str
    site_name: str
    window_from: datetime | None
    window_to: datetime | None
    status: str
    shipments: list[StopShipmentOut]


class TripSummary(Schema):
    id: uuid.UUID
    number: str
    site_id: uuid.UUID
    planned_start: datetime
    status: str
    vehicle_id: uuid.UUID | None
    vehicle_registration: str | None
    driver_id: uuid.UUID | None
    driver_name: str | None
    carrier_id: uuid.UUID | None
    carrier_code: str | None
    agreed_cost: Decimal | None
    currency: str
    dispatched_at: datetime | None


class TripOut(TripSummary):
    stops: list[StopOut]
    units_total: int
    units_loaded: int


class TripPage(Schema):
    items: list[TripSummary]
    next_cursor: uuid.UUID | None


class LoadedUnitOut(Schema):
    sscc: str
    shipment_id: uuid.UUID
    loaded_trip_id: uuid.UUID | None
    loaded_at: datetime | None
    units_total: int
    units_loaded: int
    loaded_weight_kg: Decimal
    warning: str | None = Field(None, description="Set when the load is heavier than the vehicle's rated capacity")


def _trips(principal: Principal) -> QuerySet:
    require_staff(principal)
    qs = Trip.objects.select_related("vehicle", "driver", "carrier")
    return principal.scope(qs, client_field=None, site_field="site_id")


def _summary(trip: Trip) -> dict:
    return {
        "id": trip.pk,
        "number": trip.number,
        "site_id": trip.site_id,
        "planned_start": trip.planned_start,
        "status": trip.status,
        "vehicle_id": trip.vehicle_id,
        "vehicle_registration": trip.vehicle.registration if trip.vehicle else None,
        "driver_id": trip.driver_id,
        "driver_name": f"{trip.driver.first_name} {trip.driver.last_name}" if trip.driver else None,
        "carrier_id": trip.carrier_id,
        "carrier_code": trip.carrier.code if trip.carrier else None,
        "agreed_cost": trip.agreed_cost,
        "currency": trip.currency,
        "dispatched_at": trip.dispatched_at,
    }


def _unit_counts(trip: Trip) -> dict:
    shipment_ids = StopShipment.objects.filter(trip=trip).values("shipment_id")
    return ShipmentUnit.objects.filter(shipment_id__in=shipment_ids).aggregate(
        units_total=Count("pk"), units_loaded=Count("pk", filter=Q(loaded_trip=trip))
    )


def _detail(trip: Trip, principal: Principal) -> dict:
    """A trip may carry several clients' shipments; staff limited to some clients only see theirs."""
    trip = Trip.objects.select_related("vehicle", "driver", "carrier").get(pk=trip.pk)
    links = StopShipment.objects.filter(trip=trip).select_related("shipment").order_by("shipment__number")
    by_stop: dict[uuid.UUID, list[dict]] = {}
    for link in links:
        if principal.can_see_client(link.client_id):
            by_stop.setdefault(link.stop_id, []).append(
                {"shipment_id": link.shipment_id, "number": link.shipment.number, "action": link.action}
            )
    stops = [
        {
            "id": stop.pk,
            "seq": stop.seq,
            "site_id": stop.site_id,
            "site_code": stop.site.code,
            "site_name": stop.site.name,
            "window_from": stop.window_from,
            "window_to": stop.window_to,
            "status": stop.status,
            "shipments": by_stop.get(stop.pk, []),
        }
        for stop in trip.stops.select_related("site").order_by("seq")
    ]
    return {**_summary(trip), "stops": stops, **_unit_counts(trip)}


def _writable_trip(request, roles: tuple[str, ...] = DISPATCHERS) -> Principal:
    principal = principal_of(request)
    require_staff(principal)
    require_roles(principal, *roles)
    return principal


def _site(principal: Principal, site_id: uuid.UUID) -> Site:
    site = Site.objects.filter(pk=site_id).first()
    if site is None or not principal.can_see_site(site.pk):
        raise Problem(404, "Not found", "Unknown site.")
    return site


@router.post("/trips", response={201: TripOut})
def create_trip(request, body: TripIn):
    """Start planning a trip from a warehouse; add stops and a resource, load, then dispatch."""
    principal = _writable_trip(request)
    trip = trips.create_trip(
        site=_site(principal, body.site_id), planned_start=body.planned_start, created_by=principal.id
    )
    return Status(201, _detail(trip, principal))


@router.get("/trips", response=TripPage)
def list_trips(
    request,
    site_id: uuid.UUID | None = None,
    status: TripStatus | None = None,
    on: date | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = 50,
):
    """The dispatch board. `on` limits it to trips planned to start on that date."""
    qs = _trips(principal_of(request))
    if site_id:
        qs = qs.filter(site_id=site_id)
    if status:
        qs = qs.filter(status=status)
    if on:
        qs = qs.filter(planned_start__date=on)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": [_summary(t) for t in items], "next_cursor": next_cursor}


@router.get("/trips/{trip_id}", response=TripOut)
def get_trip(request, trip_id: uuid.UUID):
    principal = principal_of(request)
    return _detail(get_scoped(_trips(principal), trip_id), principal)


@router.put("/trips/{trip_id}/stops", response=TripOut)
def set_stops(request, trip_id: uuid.UUID, body: StopsIn):
    """Replace the trip's drop stops. Stop 1, the pickup at the warehouse, is added for you."""
    principal = _writable_trip(request)
    trip = get_scoped(_trips(principal), trip_id)
    wanted = {sid for stop in body.stops for sid in stop.shipment_ids}
    visible = set(principal.scope(Shipment.objects.filter(pk__in=wanted)).values_list("pk", flat=True))
    if wanted - visible:
        raise Problem(422, "Unknown shipment", f"{len(wanted - visible)} unknown shipment(s).")
    stops = [trips.StopIn(s.site_id, list(s.shipment_ids), s.window_from, s.window_to) for s in body.stops]
    return _detail(trips.set_stops(trip.pk, stops), principal)


@router.post("/trips/{trip_id}/assign", response=TripOut)
def assign(request, trip_id: uuid.UUID, body: AssignIn):
    principal = _writable_trip(request)
    trip = get_scoped(_trips(principal), trip_id)
    own = body.vehicle_id is not None or body.driver_id is not None
    carrier = body.carrier_id is not None or body.agreed_cost is not None
    if own == carrier:
        raise Problem(422, "Choose one", "Send vehicle_id and driver_id, or carrier_id and agreed_cost.")
    if own:
        if body.vehicle_id is None or body.driver_id is None:
            raise Problem(422, "Vehicle and driver", "An own-fleet trip needs both a vehicle and a driver.")
        trip = trips.assign_own(trip.pk, vehicle_id=body.vehicle_id, driver_id=body.driver_id)
    else:
        if body.carrier_id is None or body.agreed_cost is None:
            raise Problem(422, "Carrier and cost", "A subcontracted trip needs the carrier and the agreed cost.")
        trip = trips.assign_carrier(
            trip.pk, carrier_id=body.carrier_id, agreed_cost=body.agreed_cost, currency=body.currency
        )
    return _detail(trip, principal)


def _loaded(unit: ShipmentUnit, trip: Trip) -> dict:
    weight = ShipmentUnit.objects.filter(loaded_trip=trip).aggregate(total=Sum("weight_kg"))["total"] or Decimal(0)
    capacity = trip.vehicle.capacity_kg if trip.vehicle_id and trip.vehicle else None
    warning = f"Loaded {weight} kg is over the vehicle's {capacity} kg." if capacity and weight > capacity else None
    return {
        "sscc": unit.sscc,
        "shipment_id": unit.shipment_id,
        "loaded_trip_id": unit.loaded_trip_id,
        "loaded_at": unit.loaded_at,
        **_unit_counts(trip),
        "loaded_weight_kg": weight,
        "warning": warning,
    }


@router.post("/trips/{trip_id}/load", response=LoadedUnitOut)
def load(request, trip_id: uuid.UUID, body: LoadIn):
    """Scan a handling unit onto the trip. Scanning it again is harmless."""
    principal = _writable_trip(request, LOADERS)
    trip = get_scoped(_trips(principal), trip_id)
    return _loaded(trips.load(trip.pk, body.sscc, user_id=principal.id), trip)


@router.delete("/trips/{trip_id}/load/{sscc}", response=LoadedUnitOut)
def unload(request, trip_id: uuid.UUID, sscc: str):
    principal = _writable_trip(request, LOADERS)
    trip = get_scoped(_trips(principal), trip_id)
    return _loaded(trips.unload(trip.pk, sscc), trip)


@router.post("/trips/{trip_id}/dispatch", response=TripOut)
def dispatch(request, trip_id: uuid.UUID):
    """The truck leaves: stops are frozen, shipments go in transit, and clients are told their orders shipped."""
    principal = _writable_trip(request)
    trip = get_scoped(_trips(principal), trip_id)
    return _detail(trips.dispatch(trip.pk, user_id=principal.id), principal)


@router.post("/trips/{trip_id}/cancel", response=TripOut)
def cancel(request, trip_id: uuid.UUID):
    principal = _writable_trip(request)
    trip = get_scoped(_trips(principal), trip_id)
    return _detail(trips.cancel(trip.pk), principal)


# --- On the road ------------------------------------------------------------------------------------------------
# The trip's own driver acts from the PWA (online or through sync); a dispatcher at the trip's site can act for
# them, for instance keying in a carrier's paper POD.

Latitude = Annotated[Decimal, Field(ge=-90, le=90, max_digits=9, decimal_places=6)]
Longitude = Annotated[Decimal, Field(ge=-180, le=180, max_digits=9, decimal_places=6)]


class RoadUnitOut(Schema):
    sscc: str
    kind: str
    cartons: int
    weight_kg: Decimal
    delivery_status: str


class RoadShipmentOut(Schema):
    id: uuid.UUID
    number: str
    order_number: str
    client_ref: str
    status: str
    units: list[RoadUnitOut]


class RoadStopOut(Schema):
    id: uuid.UUID
    seq: int
    status: str
    site_code: str
    site_name: str
    address: str
    latitude: Decimal | None
    longitude: Decimal | None
    window_from: datetime | None
    window_to: datetime | None
    arrived_at: datetime | None
    outcome: str | None
    shipments: list[RoadShipmentOut]


class RoadTripOut(Schema):
    id: uuid.UUID
    number: str
    status: str
    planned_start: datetime
    dispatched_at: datetime | None
    completed_at: datetime | None
    vehicle_registration: str | None
    stops: list[RoadStopOut]


def _road_trip(principal: Principal, trip_id: uuid.UUID) -> Trip:
    trip = Trip.objects.select_related("driver").filter(pk=trip_id).first()
    if trip is not None:
        if principal.has_role(*DISPATCHERS) and not principal.is_client_scoped and principal.can_see_site(trip.site_id):
            return trip
        if Role.DRIVER in principal.roles and trip.driver is not None and trip.driver.user_id == principal.id:
            return trip
    raise Problem(404, "Not found")


def _road(trip: Trip) -> dict:
    trip = Trip.objects.select_related("vehicle").get(pk=trip.pk)
    stops = list(trip.stops.select_related("site").order_by("seq"))
    attempts = {a.stop_id: a.outcome for a in DeliveryAttempt.objects.filter(trip=trip)}
    links = (
        StopShipment.objects.filter(trip=trip, action=StopAction.DROP)
        .select_related("shipment")
        .prefetch_related(Prefetch("shipment__units", queryset=ShipmentUnit.objects.order_by("sscc")))
        .order_by("shipment__number")
    )
    by_stop: dict[uuid.UUID, list[dict]] = {}
    for link in links:
        shipment = link.shipment
        by_stop.setdefault(link.stop_id, []).append(
            {
                "id": shipment.pk,
                "number": shipment.number,
                "order_number": shipment.order_number,
                "client_ref": shipment.client_ref,
                "status": shipment.status,
                "units": list(shipment.units.all()),
            }
        )
    return {
        "id": trip.pk,
        "number": trip.number,
        "status": trip.status,
        "planned_start": trip.planned_start,
        "dispatched_at": trip.dispatched_at,
        "completed_at": trip.completed_at,
        "vehicle_registration": trip.vehicle.registration if trip.vehicle else None,
        "stops": [
            {
                "id": stop.pk,
                "seq": stop.seq,
                "status": stop.status,
                "site_code": stop.site.code,
                "site_name": stop.site.name,
                "address": stop.site.address,
                "latitude": stop.site.latitude,
                "longitude": stop.site.longitude,
                "window_from": stop.window_from,
                "window_to": stop.window_to,
                "arrived_at": stop.arrived_at,
                "outcome": attempts.get(stop.pk),
                "shipments": by_stop.get(stop.pk, []),
            }
            for stop in stops
        ],
    }


@router.get("/driver/trips", response=list[RoadTripOut])
def my_trips(request):
    """The signed-in driver's trips: being planned, on the road, or finished in the last day. The PWA keeps the
    answer on the device so the driver can work without signal."""
    principal = principal_of(request)
    if Role.DRIVER not in principal.roles:
        raise Problem(403, "Forbidden", "Drivers only.")
    recent = timezone.now() - timedelta(days=1)
    qs = Trip.objects.filter(driver__user_id=principal.id).filter(
        Q(status__in=[TripStatus.PLANNING, TripStatus.DISPATCHED])
        | Q(status=TripStatus.COMPLETED, completed_at__gte=recent)
    )
    return [_road(trip) for trip in qs.order_by("planned_start")[:20]]


class ArriveIn(Schema):
    arrived_at: datetime | None = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None


class UnitOutcomeIn(Schema):
    sscc: str = Field(..., min_length=18, max_length=40)
    status: UnitDeliveryStatus


class PodIn(Schema):
    recipient_name: str = Field(..., min_length=1, max_length=120)
    signed_at: datetime | None = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    signature_document_id: uuid.UUID | None = None
    photo_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    units: list[UnitOutcomeIn] | None = Field(
        None,
        max_length=500,
        description="Every unit scanned at the stop; units left out are missing. Omit to mark all delivered.",
    )
    note: str = Field("", max_length=2000)


class FailedIn(Schema):
    reason: FailureReason
    note: str = Field("", max_length=2000)
    failed_at: datetime | None = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    photo_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)


class CompleteIn(Schema):
    completed_at: datetime | None = None


class EvidenceOut(Schema):
    id: uuid.UUID
    kind: str
    sha256: str
    size_bytes: int
    captured_at: datetime


@router.post("/trips/{trip_id}/stops/{stop_id}/arrive", response=RoadTripOut)
def arrive(request, trip_id: uuid.UUID, stop_id: uuid.UUID, body: ArriveIn):
    trip = _road_trip(principal_of(request), trip_id)
    delivery.arrive(trip.pk, stop_id, where=delivery.Where(body.arrived_at, body.latitude, body.longitude))
    return _road(trip)


@router.post("/trips/{trip_id}/stops/{stop_id}/evidence", response={201: EvidenceOut})
def upload_evidence(
    request,
    trip_id: uuid.UUID,
    stop_id: uuid.UUID,
    file: File[UploadedFile],
    kind: Form[str],
    captured_at: Form[datetime | None] = None,
    latitude: Form[Latitude | None] = None,
    longitude: Form[Longitude | None] = None,
):
    """Upload a signature (pod_signature) or photo (pod_photo) before sending the POD that uses it. Uploading the
    same file again returns the same document."""
    principal = principal_of(request)
    trip = _road_trip(principal, trip_id)
    doc = delivery.store_evidence(
        trip.pk, stop_id, file, kind=kind, where=delivery.Where(captured_at, latitude, longitude), user_id=principal.id
    )
    return Status(201, doc)


@router.post("/trips/{trip_id}/stops/{stop_id}/pod", response=RoadTripOut)
def record_pod(request, trip_id: uuid.UUID, stop_id: uuid.UUID, body: PodIn):
    principal = principal_of(request)
    trip = _road_trip(principal, trip_id)
    units = None if body.units is None else {u.sscc: u.status for u in body.units}
    if units is not None and len(units) != len(body.units):
        raise Problem(422, "Unit listed twice", "Give each unit one outcome.")
    delivery.record_pod(
        trip.pk,
        stop_id,
        recipient_name=body.recipient_name,
        where=delivery.Where(body.signed_at, body.latitude, body.longitude),
        signature_id=body.signature_document_id,
        photo_ids=body.photo_document_ids,
        units=units,
        note=body.note,
        user_id=principal.id,
    )
    return _road(trip)


@router.post("/trips/{trip_id}/stops/{stop_id}/failed", response=RoadTripOut)
def record_failure(request, trip_id: uuid.UUID, stop_id: uuid.UUID, body: FailedIn):
    principal = principal_of(request)
    trip = _road_trip(principal, trip_id)
    delivery.record_failure(
        trip.pk,
        stop_id,
        reason=body.reason,
        note=body.note,
        where=delivery.Where(body.failed_at, body.latitude, body.longitude),
        photo_ids=body.photo_document_ids,
        user_id=principal.id,
    )
    return _road(trip)


@router.post("/trips/{trip_id}/complete", response=RoadTripOut)
def complete(request, trip_id: uuid.UUID, body: CompleteIn):
    """Back at base: every drop stop has a POD or a failure. Failed shipments are ready to plan again."""
    principal = principal_of(request)
    trip = _road_trip(principal, trip_id)
    return _road(delivery.complete(trip.pk, at=body.completed_at, user_id=principal.id))
