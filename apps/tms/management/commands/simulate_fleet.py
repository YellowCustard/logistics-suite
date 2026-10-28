"""Drive the vehicles of dispatched trips toward their stops, for demos on staging before real telematics exists.

Each tick moves every own-fleet vehicle on the road a step toward its next pending stop and records the fix through
the same ingest path as the vendor poller, so geofence arrival, ETAs and the dispatch board behave as they will with
live data. A vehicle waits at an arrived stop until the driver records the POD or a failure, then drives on.
"""

import logging
import math
import signal
import time
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from apps.integrations.ports import Position
from apps.masterdata.models import Vehicle
from apps.tms import telematics
from apps.tms.models import Stop, StopStatus, Trip, TripStatus

log = logging.getLogger(__name__)

SIM_PREFIX = "sim-"


def _step(lat: float, lon: float, to_lat: float, to_lon: float, metres: float) -> tuple[float, float, bool]:
    """Move up to `metres` along the straight line toward the target. Returns the new point and whether it
    reached the target."""
    remaining = telematics.distance_m(lat, lon, to_lat, to_lon)
    if remaining <= metres:
        return to_lat, to_lon, True
    f = metres / remaining
    return lat + (to_lat - lat) * f, lon + (to_lon - lon) * f, False


def _heading(lat: float, lon: float, to_lat: float, to_lon: float) -> float:
    y = math.sin(math.radians(to_lon - lon)) * math.cos(math.radians(to_lat))
    x = math.cos(math.radians(lat)) * math.sin(math.radians(to_lat)) - math.sin(math.radians(lat)) * math.cos(
        math.radians(to_lat)
    ) * math.cos(math.radians(to_lon - lon))
    return round((math.degrees(math.atan2(y, x)) + 360) % 360, 1)


def tick(*, interval_s: float, speed_kmh: float, link: bool) -> dict:
    """One simulation step for every dispatched own-fleet trip. Returns counts for logging."""
    now = timezone.now()
    moved = waiting = skipped = 0
    positions = []
    for trip in Trip.objects.filter(status=TripStatus.DISPATCHED, vehicle__isnull=False).select_related(
        "vehicle", "site"
    ):
        vehicle = trip.vehicle
        if not vehicle.telematics_id:
            if not link:
                skipped += 1
                continue
            vehicle.telematics_id = f"{SIM_PREFIX}{vehicle.registration}"
            vehicle.save()
        fix = telematics.last_position(vehicle.pk)
        if fix is not None and fix.recorded_at >= (trip.dispatched_at or now) - timedelta(minutes=1):
            lat, lon = fix.latitude, fix.longitude
        elif trip.site.latitude is not None and trip.site.longitude is not None:
            lat, lon = float(trip.site.latitude), float(trip.site.longitude)
        else:
            skipped += 1
            continue
        nxt = (
            Stop.objects.filter(trip=trip, seq__gt=1)
            .exclude(status=StopStatus.DONE)
            .select_related("site")
            .order_by("seq")
            .first()
        )
        if nxt is None or nxt.status == StopStatus.ARRIVED or nxt.site.latitude is None:
            positions.append(Position(vehicle.telematics_id, now, lat, lon, 0.0, None, True))
            waiting += 1
            continue
        to_lat, to_lon = float(nxt.site.latitude), float(nxt.site.longitude)
        lat2, lon2, there = _step(lat, lon, to_lat, to_lon, speed_kmh * 1000 / 3600 * interval_s)
        speed = 0.0 if there else speed_kmh
        positions.append(
            Position(vehicle.telematics_id, now, lat2, lon2, speed, _heading(lat, lon, to_lat, to_lon), True)
        )
        moved += 1
    stats = telematics.ingest(positions)
    return {"moved": moved, "waiting": waiting, "skipped": skipped, "arrivals": stats.arrivals}


class Command(BaseCommand):
    help = (
        "Simulate telematics for dispatched own-fleet trips (demo and staging only): vehicles drive toward their "
        "next stop and report a fix every --interval seconds. Loops until Ctrl-C, or --once."
    )

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=30.0, help="Seconds between fixes (default 30).")
        parser.add_argument("--speed-kmh", type=float, default=60.0, help="Driving speed (default 60).")
        parser.add_argument(
            "--link",
            action="store_true",
            help=f"Give vehicles without a telematics ID a simulated one ({SIM_PREFIX}<registration>).",
        )
        parser.add_argument("--once", action="store_true", help="Run one tick, then exit.")

    def handle(self, *args, interval: float, speed_kmh: float, link: bool, once: bool, **options):
        if interval <= 0 or speed_kmh <= 0:
            raise CommandError("--interval and --speed-kmh must be positive.")
        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopping:
            if not once:
                close_old_connections()  # drop a connection the database closed while we slept
            counts = tick(interval_s=interval, speed_kmh=speed_kmh, link=link)
            self.stdout.write(" ".join(f"{k}={v}" for k, v in counts.items()))
            if once:
                return
            deadline = time.monotonic() + interval
            while not stopping and time.monotonic() < deadline:
                time.sleep(0.5)
        if Vehicle.objects.filter(telematics_id__startswith=SIM_PREFIX).exists():
            self.stdout.write(
                f"Simulated telematics IDs ({SIM_PREFIX}...) stay on vehicles; clear them before go-live."
            )
