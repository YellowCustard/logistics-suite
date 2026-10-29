"""The transport half of the M3 exit test (docs/m3-exit-test.md).

`plan` turns the ready shipments of a `seed_exit_test` batch into one own-fleet trip, one stop per shipment in batch
order. `report` checks a finished trip against the exit criterion and prints what the portal check still needs."""

import re
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.masterdata.models import Person, Vehicle
from apps.platform.apiutils import Problem
from apps.platform.models import User
from apps.tms import telematics, trips
from apps.tms.models import (
    DeliveryAttempt,
    DeliveryOutcome,
    Shipment,
    ShipmentStatus,
    StopShipment,
    Trip,
    TripStatus,
)

# A batch like EXIT-1028193005 (EXIT-10281930 before seconds were added); an order reference from it
# (EXIT-1028193005-2) names the same batch.
BATCH = re.compile(r"^(EXIT-\d{8}(?:\d{2})?)(?:-\d+)?$")
# A POD captured this long before the server got it was recorded offline and synced later.
OFFLINE_LAG = timedelta(minutes=2)
# Phone GPS indoors is poor; further than this from the address is worth a look, not a failure.
POD_DISTANCE_WARN_M = 500


def _when(value) -> str:
    return timezone.localtime(value).strftime("%H:%M:%S") if value else "-"


class Command(BaseCommand):
    help = "Staging only: plan the M3 exit-test trip from a seed_exit_test batch, or report on it afterwards."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        plan = sub.add_parser("plan", help="Create the trip, its stops and the vehicle and driver.")
        plan.add_argument("--not-production", action="store_true", help="Required: confirms this is not production.")
        plan.add_argument("--batch", required=True, help="The EXIT-... reference seed_exit_test printed.")
        plan.add_argument("--vehicle", required=True, help="Vehicle registration.")
        plan.add_argument("--driver-email", required=True, help="The driver's login.")
        plan.add_argument(
            "--load-and-dispatch",
            action="store_true",
            help="Also load every carton and dispatch, for a rehearsal without the warehouse steps.",
        )
        dispatch = sub.add_parser("dispatch", help="Dispatch a planned trip once every carton is loaded.")
        dispatch.add_argument(
            "--not-production", action="store_true", help="Required: confirms this is not production."
        )
        dispatch.add_argument("trip", help="Trip number, e.g. TRP-2026-000012.")
        report = sub.add_parser("report", help="Check a trip against the exit criterion.")
        report.add_argument("trip", help="Trip number, e.g. TRP-2026-000012.")

    def handle(self, *args, action: str, **options):
        try:
            if action == "plan":
                self._plan(**options)
            elif action == "dispatch":
                self._dispatch(**options)
            else:
                self._report(options["trip"])
        except Problem as exc:
            raise CommandError(f"{exc.title}: {exc.detail}") from None

    def _plan(self, *, not_production: bool, batch: str, vehicle: str, driver_email: str, load_and_dispatch: bool, **_):
        if not not_production:
            raise CommandError("Refusing to run without --not-production.")
        batch = batch.strip().upper()
        match = BATCH.match(batch)
        if match is None:
            raise CommandError("--batch is the EXIT-… reference seed_exit_test printed, e.g. EXIT-1028193005.")
        batch = match.group(1)
        shipments = sorted(
            Shipment.objects.filter(client_ref__startswith=f"{batch}-").select_related("ship_to"),
            key=lambda s: int(s.client_ref.rsplit("-", 1)[1]),
        )
        if not shipments:
            raise CommandError(f"No shipments for batch {batch}.")
        not_ready = [f"{s.number} is {s.status}" for s in shipments if s.status != ShipmentStatus.READY]
        if not_ready:
            raise CommandError(f"Every shipment must be ready to plan: {'; '.join(not_ready)}.")
        truck = Vehicle.objects.filter(registration__iexact=vehicle.strip()).first()
        if truck is None:
            raise CommandError(f"No vehicle {vehicle}.")
        user = User.objects.filter(email__iexact=driver_email.strip()).first()
        driver = Person.objects.filter(user_id=user.pk).first() if user else None
        if driver is None:
            raise CommandError(f"No driver with the login {driver_email}.")

        trip = trips.create_trip(
            site=shipments[0].site, planned_start=timezone.now() + timedelta(minutes=30), created_by=None
        )
        trips.set_stops(trip.pk, [trips.StopIn(s.ship_to_id, [s.pk]) for s in shipments])
        trips.assign_own(trip.pk, vehicle_id=truck.pk, driver_id=driver.pk)
        units = [u.sscc for s in shipments for u in s.units.order_by("sscc")]
        if load_and_dispatch:
            for sscc in units:
                trips.load(trip.pk, sscc, user_id=None)
            trips.dispatch(trip.pk, user_id=None)

        out = self.stdout
        trip.refresh_from_db()
        out.write(self.style.SUCCESS(f"{trip.number} ({trip.pk}) is {trip.status}: {truck.registration}, {driver}."))
        for n, s in enumerate(shipments, start=1):
            cartons = ", ".join(u.sscc for u in s.units.all())
            out.write(f"  Stop {n}: {s.ship_to.code} {s.ship_to.name}  {s.number}  cartons {cartons}")
        if not load_and_dispatch:
            out.write(f"Next: scan the {len(units)} cartons onto {trip.number} on the scanner's Load tile, then run")
            out.write(f"  manage.py exit_test_trip dispatch --not-production {trip.number}")
        out.write(f"Afterwards: manage.py exit_test_trip report {trip.number}")

    def _trip(self, number: str) -> Trip:
        trip = Trip.objects.select_related("vehicle", "driver").filter(number=number.strip().upper()).first()
        if trip is None:
            raise CommandError(f"No trip {number}.")
        return trip

    def _dispatch(self, *, not_production: bool, trip: str, **_):
        if not not_production:
            raise CommandError("Refusing to run without --not-production.")
        dispatched = trips.dispatch(self._trip(trip).pk, user_id=None)
        self.stdout.write(self.style.SUCCESS(f"{dispatched.number} dispatched. The driver can start."))

    def _report(self, number: str):
        trip = self._trip(number)
        out = self.stdout
        out.write(f"{trip.number}: {trip.status}, {trip.vehicle or '-'} / {trip.driver or '-'}")
        out.write(f"  dispatched {_when(trip.dispatched_at)}, completed {_when(trip.completed_at)}")
        drops = list(trip.stops.filter(seq__gt=1).select_related("site").order_by("seq"))
        attempts = {
            a.stop_id: a
            for a in DeliveryAttempt.objects.filter(trip=trip).select_related("signature").prefetch_related("photos")
        }
        failures: list[str] = []
        warnings: list[str] = []
        offline = 0
        header = (
            "  stop  site                  arrived (how)        outcome    signed    received  sig photos  from site"
        )
        out.write(header)
        for stop in drops:
            attempt = attempts.get(stop.pk)
            label = f"stop {stop.seq - 1} ({stop.site.code})"
            if attempt is None:
                failures.append(f"{label} has no POD or failed-delivery record")
                arrived = f"{_when(stop.arrived_at)} ({stop.arrival_source or '-'})"
                out.write(f"  {stop.seq - 1:>4}  {stop.site.code:<20}  {arrived}  -")
                continue
            lag = attempt.created_at - attempt.captured_at
            if lag >= OFFLINE_LAG:
                offline += 1
            photos = len(attempt.photos.all())
            distance = "-"
            if None not in (attempt.latitude, attempt.longitude, stop.site.latitude, stop.site.longitude):
                metres = telematics.distance_m(
                    float(attempt.latitude),
                    float(attempt.longitude),
                    float(stop.site.latitude),
                    float(stop.site.longitude),
                )
                distance = f"{metres:.0f} m"
                if metres > POD_DISTANCE_WARN_M:
                    warnings.append(f"{label}: POD recorded {metres:.0f} m from the address")
            else:
                warnings.append(f"{label}: no phone location on the POD")
            if attempt.outcome == DeliveryOutcome.DELIVERED and attempt.signature is None and not photos:
                failures.append(f"{label} was delivered without a signature or photo")
            out.write(
                f"  {stop.seq - 1:>4}  {stop.site.code:<20}  {_when(stop.arrived_at)} ({stop.arrival_source or '-':<8})"
                f"  {attempt.outcome:<9}  {_when(attempt.captured_at)}  {_when(attempt.created_at)}"
                f"  {'yes' if attempt.signature else 'no':<3} {photos:>6}  {distance}"
                + ("  synced late" if lag >= OFFLINE_LAG else "")
            )

        shipments = Shipment.objects.filter(
            pk__in=StopShipment.objects.filter(trip=trip).values("shipment_id")
        ).order_by("number")
        out.write("  shipments: " + ", ".join(f"{s.number} {s.status}" for s in shipments))

        if len(drops) < 5:
            failures.append(f"the trip has {len(drops)} stops; the test needs 5")
        if trip.status != TripStatus.COMPLETED:
            failures.append(f"the trip is {trip.status}, not completed")
        if not offline:
            failures.append("no POD was synced late, so the dropped-signal leg didn't happen or wasn't offline")
        for line in warnings:
            out.write(self.style.WARNING(f"  check: {line}"))
        if failures:
            for line in failures:
                out.write(self.style.ERROR(f"  FAIL: {line}"))
            raise CommandError(f"{trip.number} does not meet the exit criterion yet.")
        out.write(
            self.style.SUCCESS(f"  PASS (server side): {len(drops)} stops done, {offline} POD(s) captured offline.")
        )
        out.write(
            "  Still to check by hand: each POD in the portal (Tracking and Documents), as in docs/m3-exit-test.md."
        )
