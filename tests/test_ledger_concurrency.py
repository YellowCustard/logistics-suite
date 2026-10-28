"""Two scanners moving the same stock in opposite directions between two bins must not deadlock.

Each transfer must lock both balances, in one fixed order, before it changes either. A threaded reproduction needs
committed data and so a flushed database afterwards, which wipes the rows data migrations seed and can't be undone
with serialized rollback because the audit table is append-only. So this checks the lock order in the SQL; the M3
load test (docs/loadtest-m3.md) exercises the real race.
"""

import re
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from apps.masterdata import services as masterdata
from apps.masterdata.models import Item, PartyRoleType, Site, SiteKind
from apps.wms import ledger
from apps.wms.models import InventoryBalance, Location, LocationKind

pytestmark = pytest.mark.django_db

UUID = re.compile(r"'([0-9a-f]{32})'::uuid")


@pytest.fixture
def bins():
    client = masterdata.create_party(code="LOCK", legal_name="Lock Co", roles=[PartyRoleType.CLIENT], vat_exempt=True)
    item = Item.objects.create(client=client, sku="LOCK-1", description="Lock test")
    site = Site.objects.create(code="LOCK-WH", name="Lock warehouse", kind=SiteKind.WAREHOUSE)
    a, b = (
        Location.objects.create(site=site, code=code, kind=LocationKind.STORAGE, pallet_capacity=10, pick_sequence=1)
        for code in ("A", "B")
    )
    at_a = ledger.StockKey(client.pk, item.pk, a.pk, "", None, "available")
    at_b = at_a.at(b)
    with transaction.atomic():
        ledger.post(at_a, Decimal(10), kind="opening")
        ledger.post(at_b, Decimal(10), kind="opening")
    return at_a, at_b


def _first_lock(source, target) -> str:
    with CaptureQueriesContext(connection) as queries, transaction.atomic():
        ledger.transfer(source, target, Decimal(1), kind="move")
    statements = [q["sql"] for q in queries.captured_queries if '"wms_inventorybalance"' in q["sql"]]
    first_update = next(i for i, sql in enumerate(statements) if sql.startswith("UPDATE"))
    locks = [sql for sql in statements[:first_update] if "FOR UPDATE" in sql]
    assert locks, "transfer changed a balance before locking it"
    return locks[0]


def test_transfers_lock_both_balances_in_id_order_first(bins):
    at_a, at_b = bins
    ids = {b.pk.hex for b in InventoryBalance.objects.all()}
    for source, target in ((at_a, at_b), (at_b, at_a)):  # whichever way the stock moves
        lock = _first_lock(source, target)
        assert set(UUID.findall(lock)) == ids
        assert re.search(r'ORDER BY (1|"wms_inventorybalance"\."id") ASC FOR UPDATE$', lock.rstrip()), lock
    assert sorted(InventoryBalance.objects.values_list("qty_on_hand", flat=True)) == [Decimal(10), Decimal(10)]
