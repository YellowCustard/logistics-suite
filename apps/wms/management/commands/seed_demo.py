"""Demo data for staging: two fictional clients, a warehouse with bins, items, users, and stock that was
received and put away through the real services, so every movement, event and audit row is genuine."""

import secrets
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.masterdata import services as masterdata
from apps.masterdata.models import Item, ItemBarcode, ItemUom, PartyRoleType, Site, SiteKind
from apps.oms import services as oms
from apps.platform import outbox
from apps.platform.models import Role, RoleGrant, User
from apps.wms import putaway, receiving
from apps.wms.models import Location, LocationKind, PutawayTask, TaskStatus

SITE_CODE = "DEMO-JHB"

CLIENTS = [
    ("KAL", "Kalahari Foods (Pty) Ltd", "4900000017"),
    ("BAO", "Baobab Home (Pty) Ltd", "4900000025"),
]

# sku, description, case size, units per pallet, lot and expiry tracked, barcode
ITEMS = {
    "KAL": [
        ("KAL-RUS-500", "Rooibos rusks 500g", 12, 960, True, "6009900000011"),
        ("KAL-BIL-250", "Beef biltong 250g", 20, 1200, True, "6009900000028"),
        ("KAL-MAZ-10K", "Maize meal 10kg", 1, 100, False, "6009900000035"),
        ("KAL-JUI-1L", "Mango juice 1l", 6, 720, True, "6009900000042"),
        ("KAL-TEA-80", "Rooibos tea 80s", 24, 2400, False, "6009900000059"),
    ],
    "BAO": [
        ("BAO-MUG-01", "Stoneware mug", 12, 864, False, "6009900000066"),
        ("BAO-CUS-45", "Linen cushion 45cm", 8, 320, False, "6009900000073"),
        ("BAO-LMP-01", "Ceramic table lamp", 2, 96, False, "6009900000080"),
        ("BAO-THR-01", "Mohair throw", 6, 240, False, "6009900000097"),
    ],
}

# Customer addresses that outbound orders deliver to.
DELIVERY_ADDRESSES = {
    "KAL": [
        ("KAL-SANDTON", "Sandton store", -26.107407, 28.056229),
        ("KAL-PTA", "Pretoria store", -25.747868, 28.229271),
    ],
    "BAO": [
        ("BAO-ROSEBANK", "Rosebank store", -26.145730, 28.041340),
    ],
}

USERS = [
    ("demo.supervisor@nuronteq.test", Role.WAREHOUSE_SUPERVISOR, None),
    ("demo.operator@nuronteq.test", Role.WAREHOUSE_OPERATOR, None),
    ("demo.client@nuronteq.test", Role.CLIENT_ADMIN, "KAL"),
]


def deliver_events() -> None:
    """Publish and deliver pending outbox events in this process instead of waiting for the worker."""
    for _ in range(20):  # handlers can emit follow-up events; bounded in case one keeps failing
        published = outbox.publish_pending()
        stats = outbox.deliver_pending()
        if not published and not (stats.delivered or stats.duplicates):
            return


class Command(BaseCommand):
    help = (
        "Load demo data (fictional clients KAL and BAO at site DEMO-JHB) for staging. Never run it in "
        "production. Prints the demo users' passwords once."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--not-production", action="store_true", help="Required: confirms this database is not production."
        )

    def handle(self, *args, not_production: bool, **options):
        if not not_production:
            raise CommandError("Refusing to run without --not-production.")
        if Site.objects.filter(code=SITE_CODE).exists():
            self.stdout.write(f"Demo data already loaded (site {SITE_CODE} exists); nothing to do.")
            return
        passwords = self._seed()
        deliver_events()  # the worker would too; this way notices close before anyone looks
        self.stdout.write(self.style.SUCCESS(f"Demo data loaded at site {SITE_CODE}."))
        self.stdout.write("Demo logins (shown once, store them now):")
        for email, password in passwords:
            self.stdout.write(f"  {email}  {password}")

    @transaction.atomic
    def _seed(self) -> list[tuple[str, str]]:
        site = Site.objects.create(
            code=SITE_CODE,
            name="Demo warehouse, Johannesburg",
            kind=SiteKind.WAREHOUSE,
            address="1 Demo Road, Kempton Park, 1619",
            latitude=Decimal("-26.100000"),
            longitude=Decimal("28.230000"),
            pallet_capacity=400,
        )
        docks = self._locations(site)
        clients = {
            code: masterdata.create_party(
                code=code,
                legal_name=name,
                roles=[PartyRoleType.CLIENT],
                vat_number=vat,
                billing_email=f"accounts@{code.lower()}.test",
            )
            for code, name, vat in CLIENTS
        }
        for code, client in clients.items():
            for item in ITEMS[code]:
                self._item(client, *item)
            for addr_code, name, lat, lng in DELIVERY_ADDRESSES[code]:
                Site.objects.create(
                    code=addr_code,
                    name=name,
                    kind=SiteKind.DELIVERY_ADDRESS,
                    party=client,
                    latitude=Decimal(str(lat)),
                    longitude=Decimal(str(lng)),
                )
        passwords = self._users(site, clients)

        # Two received and put-away ASNs per client, and one still waiting at the dock.
        today = timezone.localdate()
        for code, client in clients.items():
            for n in (1, 2):
                notice = self._asn(client, site, f"DEMO-{code}-PO-{n}", days_ago=10 - n * 3, today=today)
                self._receive_and_put_away(notice.pk, docks[0])
            self._asn(client, site, f"DEMO-{code}-PO-3", days_ago=-1, today=today)
        return passwords

    def _locations(self, site: Site) -> list[Location]:
        docks = [
            Location.objects.create(
                site=site, code=f"DOCK-{n}", kind=LocationKind.DOCK, pallet_capacity=0, pick_sequence=0
            )
            for n in (1, 2)
        ]
        Location.objects.create(
            site=site, code="STAGE-1", kind=LocationKind.STAGING, pallet_capacity=0, pick_sequence=1
        )
        Location.objects.create(
            site=site, code="Q-01", kind=LocationKind.QUARANTINE, pallet_capacity=10, pick_sequence=9000
        )
        seq = 10
        for aisle in "ABC":
            for bay in range(1, 11):
                Location.objects.create(
                    site=site,
                    code=f"{aisle}-{bay:02d}",
                    zone=aisle,
                    kind=LocationKind.STORAGE,
                    pallet_capacity=2,
                    pick_sequence=seq,
                )
                seq += 10
        return docks

    def _item(self, client, sku, description, case, per_pallet, tracked, barcode) -> Item:
        item = Item.objects.create(
            client=client,
            sku=sku,
            description=description,
            base_uom="EA",
            units_per_pallet=per_pallet,
            lot_tracked=tracked,
            expiry_tracked=tracked,
        )
        if case > 1:
            ItemUom.objects.create(item=item, uom="CS", factor=case)
        ItemUom.objects.create(item=item, uom="PL", factor=per_pallet)
        ItemBarcode.objects.create(item=item, client=client, barcode=barcode, uom="EA")
        return item

    def _users(self, site: Site, clients: dict) -> list[tuple[str, str]]:
        created = []
        for email, role, client_code in USERS:
            if User.objects.filter(email=email).exists():
                continue
            password = secrets.token_urlsafe(12)
            user = User.objects.create_user(email=email, password=password)
            grant = {"site_ids": [site.pk]}
            if client_code:
                grant = {"client_ids": [clients[client_code].pk]}
            else:
                grant["all_clients"] = True
            RoleGrant.objects.create(user=user, role=role, **grant)
            created.append((email, password))
        return created

    def _asn(self, client, site, ref, *, days_ago: int, today):
        lines = []
        for sku, _desc, _case, per_pallet, tracked, _bc in ITEMS[client.code]:
            lines.append(
                oms.NoticeLineIn(
                    sku=sku,
                    qty=Decimal(per_pallet),
                    uom="EA",
                    lot=f"L{today:%y%m}{len(lines) + 1:02d}" if tracked else "",
                    expiry=today + timedelta(days=120 + 30 * len(lines)) if tracked else None,
                )
            )
        expected = timezone.now() - timedelta(days=days_ago)
        return oms.create_inbound_notice(client=client, site=site, expected_at=expected, lines=lines, client_ref=ref)

    def _receive_and_put_away(self, notice_id, dock: Location) -> None:
        receipt = receiving.open_receipt(inbound_notice_id=notice_id, dock=dock)
        for line in oms.expected_notice(notice_id).lines:
            receiving.add_line(
                receipt.pk,
                receiving.LineIn(
                    qty=line.qty,
                    uom=line.uom,
                    sku=Item.objects.get(pk=line.item_id).sku,
                    lot=line.lot,
                    expiry=line.expiry,
                    inbound_notice_line_id=line.line_id,
                ),
            )
        receiving.complete_receipt(receipt.pk)
        for task in PutawayTask.objects.filter(receipt_line__receipt=receipt, status=TaskStatus.OPEN):
            if task.suggested_location is None:
                continue  # warehouse full: leave the pallet at the dock for the demo
            putaway.confirm(task.pk, to_location_code=task.suggested_location.code)
