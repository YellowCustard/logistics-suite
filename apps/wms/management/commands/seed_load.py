"""Fixtures for the M3 load test (loadtest/locustfile.py): six warehouses with scanner operators and stock to move,
and the 250-vehicle fleet linked to telematics units LOAD-001 to LOAD-250. Optionally back-fills a day of
positions, so queries run against a realistic table size from the first request.

Never run it in production: it creates users with a shared password."""

import os
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.masterdata import services as masterdata
from apps.masterdata.models import Item, PartyRoleType, Site, SiteKind, Vehicle
from apps.platform.models import Role, RoleGrant, User
from apps.wms import ledger
from apps.wms.models import Location, LocationKind

CLIENT_CODE = "LOADCO"
SKU = "LOAD-SKU"
SITES = 6
SCANNERS_PER_SITE = 10
VEHICLES = 250
STOCK_PER_BIN = Decimal(100_000)
# Around Johannesburg, about 10 km apart.
ORIGIN = (-26.20, 28.04)


def site_code(n: int) -> str:
    return f"LOAD-{n}"


def operator_email(site: int, n: int) -> str:
    return f"load-op-{site}-{n:02d}@load.invalid"


def unit_id(n: int) -> str:
    return f"LOAD-{n:03d}"


class Command(BaseCommand):
    help = (
        "Create load-test fixtures: 6 sites x 10 scanner operators (password from LOAD_PASSWORD), stock in bins "
        "L-A and L-B, and 250 vehicles on telematics units LOAD-001..250. Safe to run again."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--not-production", action="store_true", help="Required: confirms this database is not production."
        )
        parser.add_argument(
            "--history-hours",
            type=int,
            default=0,
            help="Also insert this many hours of 30-second positions for every vehicle (24 = about 720,000 rows).",
        )

    def handle(self, *args, not_production: bool, history_hours: int, **options):
        if not not_production:
            raise CommandError("Refusing to run without --not-production.")
        password = os.environ.get("LOAD_PASSWORD", "")
        if len(password) < 12:
            raise CommandError("Set LOAD_PASSWORD (12 characters or more) for the operators.")
        if not 0 <= history_hours <= 24 * 14:
            raise CommandError("--history-hours must be between 0 and 336.")
        sites = self._seed(password)
        self.stdout.write(f"sites={len(sites)} operators={SITES * SCANNERS_PER_SITE} vehicles={VEHICLES}")
        for code, pk in sites.items():
            self.stdout.write(f"  {code} {pk}")
        if history_hours:
            self.stdout.write(f"positions inserted={self._history(history_hours)}")

    @transaction.atomic
    def _seed(self, password: str) -> dict[str, str]:
        client = Item.objects.filter(sku=SKU).values_list("client_id", flat=True).first()
        if client is None:
            party = masterdata.create_party(
                code=CLIENT_CODE, legal_name="Load Test Co", roles=[PartyRoleType.CLIENT], vat_exempt=True
            )
            item = Item.objects.create(client=party, sku=SKU, description="Load test item", units_per_pallet=1000)
        else:
            item = Item.objects.get(sku=SKU, client_id=client)
        sites = {}
        for n in range(1, SITES + 1):
            site, created = Site.objects.get_or_create(
                code=site_code(n),
                defaults={
                    "name": f"Load test warehouse {n}",
                    "kind": SiteKind.WAREHOUSE,
                    "latitude": Decimal(str(round(ORIGIN[0] + 0.09 * (n % 3), 6))),
                    "longitude": Decimal(str(round(ORIGIN[1] + 0.1 * (n // 3), 6))),
                },
            )
            sites[site.code] = str(site.pk)
            if created:
                for code in ("L-A", "L-B"):
                    location = Location.objects.create(
                        site=site, code=code, kind=LocationKind.STORAGE, pallet_capacity=1000, pick_sequence=10
                    )
                    key = ledger.StockKey(item.client_id, item.pk, location.pk, "", None, "available")
                    ledger.post(key, STOCK_PER_BIN, kind="opening")
            for s in range(1, SCANNERS_PER_SITE + 1):
                email = operator_email(n, s)
                user = User.objects.filter(email=email).first()
                if user is None:
                    user = User.objects.create_user(email=email, password=password)
                    RoleGrant.objects.create(
                        user=user, role=Role.WAREHOUSE_OPERATOR, site_ids=[site.pk], client_ids=[item.client_id]
                    )
                else:
                    user.set_password(password)
                    user.save()
        for n in range(1, VEHICLES + 1):
            Vehicle.objects.get_or_create(registration=f"LD {n:03d}-GP", defaults={"telematics_id": unit_id(n)})
        return sites

    def _history(self, hours: int) -> int:
        """Positions every 30 s for each load vehicle over the last `hours`, generated in the database an hour at a
        time so each statement stays well inside the statement timeout."""
        inserted = 0
        with connection.cursor() as cursor:
            for hour in range(hours, 0, -1):
                cursor.execute(
                    """
                    INSERT INTO tms_vehicleposition
                        (id, vehicle_id, recorded_at, latitude, longitude, speed_kmh, heading, ignition, received_at)
                    SELECT gen_random_uuid(), v.id, t,
                           %s + random() * 0.3, %s + random() * 0.3, random() * 90, random() * 360, true, t
                    FROM masterdata_vehicle v
                    CROSS JOIN generate_series(
                        date_trunc('minute', now()) - make_interval(hours => %s),
                        date_trunc('minute', now()) - make_interval(hours => %s) - interval '30 seconds',
                        interval '30 seconds'
                    ) AS t
                    WHERE v.telematics_id LIKE 'LOAD-%%'
                    ON CONFLICT DO NOTHING
                    """,
                    [ORIGIN[0], ORIGIN[1], hour, hour - 1],
                )
                inserted += cursor.rowcount
        return inserted
