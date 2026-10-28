"""Vehicle positions from the telematics vendor: storage, geofence arrival and a rough ETA.

Positions arrive two ways, and both end in `ingest`: `poll` asks the vendor for everything recorded since the last
poll (run by `manage.py run_telematics`), and the vendor may also push to the webhook. A position is stored once,
keyed by vehicle and fix time, so overlapping polls and repeated pushes are harmless.

Arrival: while a trip is on the road, a fix inside a drop stop's geofence (Site.geofence_radius_m around the site's
coordinates) at walking pace marks that stop arrived, as if the driver had tapped Arrive. Fast fixes are ignored so
that driving past a later stop on the way to an earlier one doesn't count. GEOFENCE_AUTO_ARRIVE turns this off.

ETA: straight-line distance through the stops still ahead, times ETA_ROAD_FACTOR, at ETA_AVERAGE_KMH, plus
ETA_MINUTES_PER_STOP at each stop before the one asked about. It is only as fresh as the vehicle's last fix, and is
withheld when that fix is older than POSITION_STALE_AFTER.
"""

import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.integrations.ports import Position, TelematicsPort
from apps.masterdata.models import Vehicle
from apps.platform import exceptions_queue
from apps.platform.apiutils import Problem
from apps.platform.models import Severity

from . import delivery
from .models import ArrivalSource, Stop, StopStatus, TelematicsCursor, Trip, TripStatus, VehiclePosition

log = logging.getLogger(__name__)

EARTH_RADIUS_M = 6_371_008.8
ARRIVAL_MAX_SPEED_KMH = 15.0
FUTURE_TOLERANCE = timedelta(minutes=10)
POSITION_STALE_AFTER = timedelta(minutes=15)
# Re-read this far behind the cursor: vendors publish some fixes late (a unit out of coverage uploads its buffer).
POLL_OVERLAP = timedelta(minutes=5)
POLL_FIRST_WINDOW = timedelta(hours=1)
POLL_MAX_WINDOW = timedelta(hours=1)  # catch up after an outage in bounded steps
CURSOR = "positions"


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


# --- Ingest ------------------------------------------------------------------------------------------------------


@dataclass
class IngestStats:
    received: int = 0
    accepted: int = 0
    rejected: int = 0
    unknown: int = 0
    arrivals: int = 0
    unknown_units: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "unknown": self.unknown,
            "arrivals": self.arrivals,
        }


def _valid(p: Position, now: datetime) -> bool:
    numbers = [p.lat, p.lon] + [v for v in (p.speed_kmh, p.heading) if v is not None]
    return (
        all(math.isfinite(v) for v in numbers)
        and -90 <= p.lat <= 90
        and -180 <= p.lon <= 180
        and not (p.lat == 0 and p.lon == 0)  # "null island": a unit without a fix
        and p.recorded_at.tzinfo is not None
        and p.recorded_at <= now + FUTURE_TOLERANCE
    )


def ingest(positions: Iterable[Position]) -> IngestStats:
    """Store positions and apply geofence arrivals. Positions for units no vehicle is linked to are dropped, and
    open one exception case per unit so someone links it."""
    now = timezone.now()
    stats = IngestStats()
    batch = list(positions)
    stats.received = len(batch)
    good = [p for p in batch if _valid(p, now)]
    stats.rejected = len(batch) - len(good)
    units = {p.telematics_id for p in good}
    vehicle_of = dict(Vehicle.objects.filter(telematics_id__in=units).values_list("telematics_id", "pk"))
    rows, by_vehicle = [], {}
    for p in good:
        vehicle_id = vehicle_of.get(p.telematics_id)
        if vehicle_id is None:
            stats.unknown += 1
            stats.unknown_units.add(p.telematics_id)
            continue
        rows.append(
            VehiclePosition(
                vehicle_id=vehicle_id,
                recorded_at=p.recorded_at,
                latitude=p.lat,
                longitude=p.lon,
                speed_kmh=p.speed_kmh,
                heading=p.heading,
                ignition=p.ignition,
            )
        )
        by_vehicle.setdefault(vehicle_id, []).append(p)
    VehiclePosition.objects.bulk_create(rows, batch_size=1000, ignore_conflicts=True)
    stats.accepted = len(rows)
    for unit in sorted(stats.unknown_units):
        exceptions_queue.raise_case(
            kind="telematics.unknown_vehicle",
            summary=f"Telematics unit {unit} isn't linked to a vehicle",
            severity=Severity.LOW,
            detail={"telematics_id": unit, "hint": "Set the vehicle's telematics ID, or run sync_telematics_vehicles."},
            entity_type="telematics.unit",
            entity_id=unit,
            dedupe_key=f"telematics.unknown_vehicle:{unit}",
        )
    if settings.GEOFENCE_AUTO_ARRIVE and by_vehicle:
        stats.arrivals = _geofence_arrivals(by_vehicle)
    if stats.received:
        log.info("telematics ingest", extra={"data": stats.as_dict()})
    return stats


def _geofence_arrivals(by_vehicle: dict) -> int:
    trips = {t.vehicle_id: t for t in Trip.objects.filter(status=TripStatus.DISPATCHED, vehicle_id__in=by_vehicle)}
    if not trips:
        return 0
    pending = (
        Stop.objects.filter(trip__in=trips.values(), status=StopStatus.PENDING, seq__gt=1)
        .filter(site__latitude__isnull=False, site__longitude__isnull=False)
        .select_related("site")
        .order_by("seq")
    )
    stops_of: dict = {}
    for stop in pending:
        stops_of.setdefault(stop.trip_id, []).append(stop)
    arrived = 0
    for vehicle_id, trip in trips.items():
        stops = stops_of.get(trip.pk, [])
        for p in sorted(by_vehicle[vehicle_id], key=lambda p: p.recorded_at):
            if not stops:
                break
            if p.speed_kmh is not None and p.speed_kmh > ARRIVAL_MAX_SPEED_KMH:
                continue
            if trip.dispatched_at and p.recorded_at < trip.dispatched_at:
                continue  # a fix from before the trip left can't be an arrival on it
            for stop in list(stops):
                site = stop.site
                radius = site.geofence_radius_m
                if distance_m(p.lat, p.lon, float(site.latitude), float(site.longitude)) > radius:
                    continue
                stops.remove(stop)
                where = delivery.Where(at=p.recorded_at, latitude=_coord(p.lat), longitude=_coord(p.lon))
                try:
                    done = delivery.arrive(trip.pk, stop.pk, where=where, source=ArrivalSource.GEOFENCE)
                except Problem as problem:  # trip finished meanwhile, stop has no shipments, clock skew
                    log.info(
                        "geofence arrival skipped", extra={"data": {"stop_id": str(stop.pk), "why": problem.title}}
                    )
                    continue
                if done.arrival_source == ArrivalSource.GEOFENCE and done.arrived_at == p.recorded_at:
                    arrived += 1
    return arrived


def _coord(value: float) -> Decimal:
    return Decimal(str(round(value, 6)))


# --- Polling -----------------------------------------------------------------------------------------------------


@dataclass
class PollResult:
    since: datetime
    until: datetime
    stats: IngestStats


def poll(port: TelematicsPort, *, now: datetime | None = None) -> PollResult:
    """Fetch and ingest one window of positions, then advance the cursor. If the vendor call fails the cursor stays
    put, so the next poll asks for the same window again."""
    now = now or timezone.now()
    cursor, _ = TelematicsCursor.objects.get_or_create(name=CURSOR, defaults={"polled_until": now - POLL_FIRST_WINDOW})
    since = cursor.polled_until - POLL_OVERLAP
    until = min(now, cursor.polled_until + POLL_MAX_WINDOW)
    stats = ingest(port.fetch_positions(since, until))
    with transaction.atomic():
        TelematicsCursor.objects.filter(pk=cursor.pk, polled_until__lt=until).update(polled_until=until)
    return PollResult(since=since, until=until, stats=stats)


def purge_positions(*, now: datetime | None = None) -> int:
    """Delete positions past the retention period. TimescaleDB drops whole chunks itself; this covers plain
    PostgreSQL and is a no-op there."""
    cutoff = (now or timezone.now()) - timedelta(days=settings.TELEMATICS_RETENTION_DAYS)
    deleted, _ = VehiclePosition.objects.filter(recorded_at__lt=cutoff).delete()
    return deleted


# --- Vehicle matching --------------------------------------------------------------------------------------------


def _plate(registration: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", registration.upper())


@dataclass
class SyncResult:
    linked: list[str] = field(default_factory=list)
    unchanged: int = 0
    unmatched: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@transaction.atomic
def sync_vehicles(port: TelematicsPort) -> SyncResult:
    """Link the vendor's units to fleet vehicles by registration. A vehicle already linked to a different unit is
    left alone and reported; units with no matching vehicle open a case."""
    result = SyncResult()
    vehicles = {_plate(v.registration): v for v in Vehicle.objects.select_for_update()}
    taken = {v.telematics_id: v for v in vehicles.values() if v.telematics_id}
    for unit in port.list_vehicles():
        vehicle = vehicles.get(_plate(unit.registration))
        holder = taken.get(unit.telematics_id)
        if vehicle is None:
            result.unmatched.append(unit.registration)
            exceptions_queue.raise_case(
                kind="telematics.unknown_vehicle",
                summary=f"Telematics unit {unit.telematics_id} ({unit.registration}) matches no fleet vehicle",
                severity=Severity.LOW,
                detail={"telematics_id": unit.telematics_id, "registration": unit.registration},
                entity_type="telematics.unit",
                entity_id=unit.telematics_id,
                dedupe_key=f"telematics.unknown_vehicle:{unit.telematics_id}",
            )
        elif vehicle.telematics_id == unit.telematics_id:
            result.unchanged += 1
        elif vehicle.telematics_id or (holder is not None and holder.pk != vehicle.pk):
            result.conflicts.append(unit.registration)
        else:
            vehicle.telematics_id = unit.telematics_id
            vehicle.save()
            taken[unit.telematics_id] = vehicle
            result.linked.append(vehicle.registration)
    return result


# --- Where and when ----------------------------------------------------------------------------------------------


def last_position(vehicle_id) -> VehiclePosition | None:
    if vehicle_id is None:
        return None
    return VehiclePosition.objects.filter(vehicle_id=vehicle_id).order_by("-recorded_at").first()


def etas(trip: Trip, *, now: datetime | None = None, fix: VehiclePosition | None = None) -> dict:
    """Estimated arrival at each pending stop of a trip that's on the road, keyed by stop id. Empty when the trip
    has no recent vehicle fix; stops from the first one without coordinates onwards get none."""
    if trip.status != TripStatus.DISPATCHED or trip.vehicle_id is None:
        return {}
    now = now or timezone.now()
    fix = fix or last_position(trip.vehicle_id)
    if fix is None or now - fix.recorded_at > POSITION_STALE_AFTER:
        return {}
    ahead = list(
        Stop.objects.filter(trip=trip, seq__gt=1).exclude(status=StopStatus.DONE).select_related("site").order_by("seq")
    )
    speed_m_per_min = settings.ETA_AVERAGE_KMH * 1000 / 60
    dwell = timedelta(minutes=settings.ETA_MINUTES_PER_STOP)
    out, at = {}, now
    lat, lon = fix.latitude, fix.longitude
    for stop in ahead:
        if stop.status == StopStatus.ARRIVED:
            at += dwell  # still unloading there; the fix is at that stop
            continue
        if stop.site.latitude is None or stop.site.longitude is None:
            break
        slat, slon = float(stop.site.latitude), float(stop.site.longitude)
        minutes = distance_m(lat, lon, slat, slon) * settings.ETA_ROAD_FACTOR / speed_m_per_min
        at += timedelta(minutes=minutes)
        out[stop.pk] = at.replace(microsecond=0)
        at += dwell
        lat, lon = slat, slon
    return out


def eta_for(trip: Trip, stop: Stop) -> datetime | None:
    if stop.status != StopStatus.PENDING:
        return None
    return etas(trip).get(stop.pk)
