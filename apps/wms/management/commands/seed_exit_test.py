"""Set up the M3 exit test on staging (docs/m3-exit-test.md): one demo-client order per delivery address in a CSV,
picked and packed into one carton each through the real services, plus the driver's login and the vehicle.

The trip itself is planned by `exit_test_trip plan`, which lives in the transport module."""

import csv
import io
import secrets
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.masterdata.models import Party, Person, Site, SiteKind, Vehicle
from apps.oms import services as oms
from apps.platform.apiutils import Problem
from apps.platform.models import Role, RoleGrant, User
from apps.wms import packing, picking
from apps.wms.models import HandlingUnitKind, PickTask

from .seed_demo import SITE_CODE, deliver_events

CLIENT_CODE = "KAL"
SKU = "KAL-TEA-80"  # not lot tracked, so packing needs no lot scans
QTY = Decimal(24)  # one case
CARTON_KG = Decimal("4.8")
MAX_STOPS = 10
COLUMNS = ("code", "name", "address", "latitude", "longitude")


@dataclass(frozen=True)
class StopRow:
    code: str
    name: str
    address: str
    latitude: Decimal
    longitude: Decimal


def _parse(f: TextIO, path: str) -> list[dict]:
    reader = csv.DictReader(f)
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise CommandError(f"{path}: missing column(s) {', '.join(missing)}; expected {','.join(COLUMNS)}.")
    return list(reader)


def read_stops(path: str) -> list[StopRow]:
    """`-` reads standard input, so the CSV can be piped into the staging container."""
    if path == "-":
        raw = _parse(io.StringIO(sys.stdin.read().lstrip("\ufeff")), "stdin")
    else:
        try:
            with Path(path).open(newline="", encoding="utf-8-sig") as f:
                raw = _parse(f, path)
        except OSError as exc:
            raise CommandError(f"Can't read {path}: {exc}") from None
    if not 1 <= len(raw) <= MAX_STOPS:
        raise CommandError(f"{path}: give 1 to {MAX_STOPS} stops (the test uses 5), not {len(raw)}.")
    rows = []
    for n, r in enumerate(raw, start=2):  # line 1 is the header
        code = (r["code"] or "").strip().upper()
        name = (r["name"] or "").strip()
        if not code or len(code) > 20 or not name:
            raise CommandError(f"{path} line {n}: code (at most 20 characters) and name are required.")
        try:
            lat, lng = Decimal(r["latitude"].strip()), Decimal(r["longitude"].strip())
        except (InvalidOperation, AttributeError):
            raise CommandError(f"{path} line {n}: latitude and longitude must be decimal degrees.") from None
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise CommandError(f"{path} line {n}: {lat},{lng} is not a coordinate.")
        rows.append(
            StopRow(
                code,
                name,
                (r["address"] or "").strip(),
                lat.quantize(Decimal("0.000001")),
                lng.quantize(Decimal("0.000001")),
            )
        )
    if len({r.code for r in rows}) != len(rows):
        raise CommandError(f"{path}: stop codes must be unique.")
    return rows


class Command(BaseCommand):
    help = (
        "Staging only: create one packed demo order per delivery address in --stops, the driver's login and the "
        "vehicle for the M3 exit test. Prints the batch reference and the command that plans the trip."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--not-production", action="store_true", help="Required: confirms this database is not production."
        )
        parser.add_argument(
            "--stops", required=True, help=f"CSV with columns {','.join(COLUMNS)}, or - to read it from standard input."
        )
        parser.add_argument("--driver-email", required=True, help="The driver's login; created if new.")
        parser.add_argument("--driver-name", required=True, help='First and last name, e.g. "Sipho Dlamini".')
        parser.add_argument("--vehicle", required=True, help="Registration of an own-fleet vehicle; created if new.")

    def handle(self, *args, not_production, stops, driver_email, driver_name, vehicle, **options):
        if not not_production:
            raise CommandError("Refusing to run without --not-production.")
        rows = read_stops(stops)
        site = Site.objects.filter(code=SITE_CODE, kind=SiteKind.WAREHOUSE).first()
        client = Party.objects.filter(code=CLIENT_CODE).first()
        if site is None or client is None:
            raise CommandError("Run `seed_demo --not-production` first.")
        first, _, last = driver_name.strip().partition(" ")
        if not first or not last.strip():
            raise CommandError('--driver-name needs a first and a last name, e.g. "Sipho Dlamini".')
        batch = f"EXIT-{timezone.localtime():%m%d%H%M}"

        try:
            with transaction.atomic():
                addresses = [self._address(client, row) for row in rows]
                password = self._driver(driver_email.strip().lower(), first, last.strip())
                truck = self._vehicle(vehicle.strip().upper(), site)
                orders = [
                    oms.create_sales_order(
                        client=client,
                        site=site,
                        ship_to=address,
                        client_ref=f"{batch}-{n}",
                        lines=[oms.OrderLineIn(sku=SKU, qty=QTY)],
                        delivery_instructions="M3 exit test",
                    )
                    for n, address in enumerate(addresses, start=1)
                ]
            deliver_events()  # allocation and the shipments run on the accepted events
            short = [o.number for o in orders if oms.order_summary(o.pk).status != oms.OrderStatus.ALLOCATED]
            if short:
                raise CommandError(f"Not enough {SKU} in stock for {', '.join(short)}; cancel them and add stock.")
            ssccs = self._pick_and_pack(site, orders)
            deliver_events()  # the closed cartons make the shipments ready to plan
        except Problem as exc:
            raise CommandError(f"{exc.title}: {exc.detail} {exc.errors or ''}".strip()) from None

        out = self.stdout
        out.write(self.style.SUCCESS(f"Batch {batch}: {len(orders)} orders picked and packed at {SITE_CODE}."))
        for n, (row, order, sscc) in enumerate(zip(rows, orders, ssccs, strict=True), start=1):
            out.write(f"  {n}. {order.number}  {batch}-{n}  {row.code} {row.name}  carton {sscc}")
        out.write(f"Vehicle {truck.registration}.")
        if password:
            out.write(f"Driver login (shown once, give it to the driver): {driver_email.strip().lower()}  {password}")
        else:
            out.write(f"Driver login {driver_email.strip().lower()} already existed; its password is unchanged.")
        out.write("Next, plan the trip:")
        out.write(
            f'  manage.py exit_test_trip plan --not-production --batch {batch} --vehicle "{truck.registration}" '
            f"--driver-email {driver_email.strip().lower()}"
        )

    def _address(self, client: Party, row: StopRow) -> Site:
        site = Site.objects.filter(code=row.code).first()
        if site is None:
            site = Site(code=row.code, kind=SiteKind.DELIVERY_ADDRESS, party=client)
        elif site.kind != SiteKind.DELIVERY_ADDRESS or site.party_id != client.pk:
            raise CommandError(f"Site code {row.code} is already used by something else; pick another code.")
        site.name, site.address, site.latitude, site.longitude = row.name, row.address, row.latitude, row.longitude
        site.is_active = True
        site.save()
        return site

    def _driver(self, email: str, first: str, last: str) -> str | None:
        """Returns the new password, or None if the login already existed."""
        user = User.objects.filter(email__iexact=email).first()
        password = None
        if user is None:
            password = secrets.token_urlsafe(9)  # 12 characters: typed once on a phone, then remembered
            user = User.objects.create_user(email=email, password=password, full_name=f"{first} {last}")
            RoleGrant.objects.create(user=user, role=Role.DRIVER)
        elif (
            user.is_staff or user.is_superuser or set(user.role_grants.values_list("role", flat=True)) != {Role.DRIVER}
        ):
            raise CommandError(f"{email} is not a driver-only login; use a separate email for the driver.")
        person = Person.objects.filter(user_id=user.pk).first()
        if person is None:
            Person.objects.create(first_name=first, last_name=last, is_driver=True, user_id=user.pk)
        elif not (person.is_driver and person.is_active and person.carrier_id is None):
            raise CommandError(f"{person} is not an active own-fleet driver.")
        return password

    def _vehicle(self, registration: str, site: Site) -> Vehicle:
        truck, _ = Vehicle.objects.get_or_create(registration=registration, defaults={"home_site": site})
        if truck.carrier_id is not None or not truck.is_active:
            raise CommandError(f"{registration} is not an active own-fleet vehicle.")
        return truck

    def _pick_and_pack(self, site: Site, orders) -> list[str]:
        wave = picking.plan_wave(site_id=site.pk, order_ids=[o.pk for o in orders], created_by=None)
        picking.release_wave(wave.pk)
        for task in PickTask.objects.filter(wave=wave).select_related("from_location", "item"):
            picking.confirm(task.pk, location_code=task.from_location.code, item_code=task.item.sku, qty=task.qty)
        ssccs = []
        for order in orders:
            unit = packing.open_unit(order_id=order.pk, kind=HandlingUnitKind.CARTON, created_by=None)
            packing.add(unit.pk, item_code=SKU, qty=QTY)
            packing.close(unit.pk, weight_kg=CARTON_KG, closed_by=None)
            ssccs.append(unit.sscc)
        return ssccs
