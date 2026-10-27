"""M2 allocation: FEFO then FIFO, shorts and backorders, top-up re-runs, release on cancel."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apps.masterdata.models import Item, Site
from apps.oms import services as oms
from apps.oms.models import SalesOrder
from apps.platform import outbox
from apps.platform.apiutils import Problem
from apps.platform.models import ExceptionCase, OutboxEvent, Role
from apps.wms import allocation, ledger
from apps.wms.models import Allocation, InventoryBalance, Location

from .conftest import api_for, make_user

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = timezone.localdate()


@pytest.fixture
def world(make_client):
    sdb = make_client("SDB", "4111111111")
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    store = Site.objects.create(code="SDB-STORE", name="Store", kind="delivery_address", party=sdb, address="1 Rd")
    juice = Item.objects.create(client=sdb, sku="JUICE", description="Juice", lot_tracked=True, expiry_tracked=True)
    water = Item.objects.create(client=sdb, sku="WATER", description="Water")
    loc = {
        code: Location.objects.create(site=teak, code=code, kind=kind, pick_sequence=seq, pallet_capacity=5)
        for code, kind, seq in [
            ("DOCK-1", "dock", 0),
            ("A-01", "storage", 10),
            ("A-02", "storage", 20),
            ("A-03", "storage", 30),
            ("Q-01", "quarantine", 90),
        ]
    }
    supervisor = make_user("sup@example.com", Role.WAREHOUSE_SUPERVISOR, site_ids=[teak.pk], all_clients=True)
    return {"sdb": sdb, "teak": teak, "store": store, "juice": juice, "water": water, "loc": loc, "sup": supervisor}


def stock(world, code, item, qty, *, lot="", days=None, status="available"):
    key = ledger.StockKey(
        world["sdb"].pk,
        item.pk,
        world["loc"][code].pk,
        lot,
        TODAY + timedelta(days=days) if days is not None else None,
        status,
    )
    with transaction.atomic():
        return ledger.post(key, D(qty), kind="opening").balance


def order(world, *lines, ref=""):
    return oms.create_sales_order(
        client=world["sdb"],
        site=world["teak"],
        ship_to=world["store"],
        client_ref=ref,
        lines=[oms.OrderLineIn(sku=sku, qty=D(qty)) for sku, qty in lines],
    )


def deliver():
    for _ in range(10):
        published = outbox.publish_pending()
        stats = outbox.deliver_pending()
        if not published and not stats.delivered:
            return


def taken(order_obj):
    rows = Allocation.objects.filter(sales_order_id=order_obj.pk, status="reserved").order_by("created_at", "id")
    return [(a.balance.location.code, a.balance.lot, a.qty_allocated) for a in rows.select_related("balance__location")]


def test_fefo_skips_expired_dock_and_quarantine_stock(world):
    juice = world["juice"]
    stock(world, "A-03", juice, 50, lot="L-LATE", days=60)
    stock(world, "A-01", juice, 20, lot="L-SOON", days=30)
    stock(world, "A-02", juice, 99, lot="L-EXPIRED", days=-1)
    stock(world, "DOCK-1", juice, 99, lot="L-DOCK", days=1)
    stock(world, "Q-01", juice, 99, lot="L-Q", days=1, status="quarantine")
    so = order(world, ("JUICE", 25))
    deliver()

    assert taken(so) == [("A-01", "L-SOON", D("20.000")), ("A-03", "L-LATE", D("5.000"))]
    so.refresh_from_db()
    assert so.status == "allocated"
    assert InventoryBalance.objects.get(lot="L-SOON").qty_allocated == 20
    completed = OutboxEvent.objects.get(type="wms.allocation.completed")
    assert completed.payload["fully_allocated"] is True
    assert not allocation.drift().exists()


def test_fifo_by_arrival_when_expiry_is_equal(world):
    stock(world, "A-03", world["water"], 10)  # arrived first, further down the pick path
    stock(world, "A-01", world["water"], 10)
    so = order(world, ("WATER", 15))
    deliver()
    assert taken(so) == [("A-03", "", D("10.000")), ("A-01", "", D("5.000"))]


def test_short_order_is_backordered_then_topped_up(world):
    stock(world, "A-01", world["water"], 30)
    so = order(world, ("WATER", 100))
    deliver()
    so.refresh_from_db()
    assert so.status == "backordered"
    assert taken(so) == [("A-01", "", D("30.000"))]  # what exists stays reserved
    shorted = OutboxEvent.objects.get(type="wms.allocation.shorted")
    assert shorted.payload["qty_requested"] == "100.000" and shorted.payload["qty_allocated"] == "30.000"
    case = ExceptionCase.objects.get(kind="allocation.short")
    assert case.status == "open"

    stock(world, "A-02", world["water"], 100)
    response = api_for(world["sup"]).post("/allocations:run", {"site_id": str(world["teak"].pk)})
    assert response.status_code == 200, response.content
    assert response.json()[0]["fully_allocated"] is True
    deliver()
    so.refresh_from_db()
    assert so.status == "allocated"
    assert taken(so) == [("A-01", "", D("30.000")), ("A-02", "", D("70.000"))]
    case.refresh_from_db()
    assert case.status == "resolved"
    assert not allocation.drift().exists()


def test_cancel_releases_reservations(world):
    stock(world, "A-01", world["water"], 30)
    so = order(world, ("WATER", 10))
    deliver()
    oms.cancel_sales_order(so.pk, "Customer cancelled")
    deliver()
    assert not taken(so)
    assert Allocation.objects.get(sales_order_id=so.pk).status == "cancelled"
    assert InventoryBalance.objects.get().qty_allocated == 0
    assert not allocation.drift().exists()


def test_order_cancelled_before_allocation_runs_reserves_nothing(world):
    stock(world, "A-01", world["water"], 30)
    so = order(world, ("WATER", 10))
    oms.cancel_sales_order(so.pk, "Typo")
    deliver()
    assert not Allocation.objects.exists()
    so.refresh_from_db()
    assert so.status == "cancelled"  # a late allocation result never revives it


def test_reserved_stock_cannot_be_moved_or_corrected_away(world):
    balance = stock(world, "A-01", world["water"], 10)
    order(world, ("WATER", 8))
    deliver()
    with pytest.raises(Problem) as excinfo, transaction.atomic():
        ledger.post(ledger.StockKey.of(balance), D(-5), kind="adjust", reason="test")
    assert excinfo.value.status == 409


def test_rerun_is_for_staff_supervisors(world, bearer_api):
    body = {"site_id": str(world["teak"].pk)}
    assert bearer_api(world["sdb"]).post("/allocations:run", body).status_code == 403
    operator = make_user("op@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[world["teak"].pk], all_clients=True)
    assert api_for(operator).post("/allocations:run", body).status_code == 403


# --- Property test ------------------------------------------------------------------------------------------------

layouts = st.lists(
    st.tuples(st.sampled_from(["A-01", "A-02", "A-03"]), st.integers(1, 50), st.integers(-3, 90)),
    min_size=1,
    max_size=6,
)
requests = st.lists(st.integers(1, 120), min_size=1, max_size=4)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(layout=layouts, qtys=requests)
def test_allocation_invariants(world, layout, qtys):
    with transaction.atomic():
        for n, (code, qty, days) in enumerate(layout):
            stock(world, code, world["juice"], qty, lot=f"L{n}", days=days)
        usable = sum(qty for _, qty, days in layout if days >= 0)
        orders = [order(world, ("JUICE", q)) for q in qtys]
        for so in orders:
            allocation.allocate_order(so.pk)

        balances = list(InventoryBalance.objects.filter(item=world["juice"]))
        assert all(b.qty_allocated <= b.qty_on_hand for b in balances)
        assert not any(b.qty_allocated for b in balances if b.expiry < TODAY)  # never expired stock
        total = sum(b.qty_allocated for b in balances)
        assert total == min(sum(qtys), usable)  # greedy: nothing left unallocated while an order is short
        for so in orders:
            line = so.lines.get()
            held = sum(a.qty_allocated for a in Allocation.objects.filter(order_line_id=line.pk))
            assert held <= line.base_qty
        # FEFO: if any stock is still free, no order dipped into a later expiry than it.
        free = [b for b in balances if b.expiry >= TODAY and b.qty_on_hand > b.qty_allocated]
        used = [b for b in balances if b.qty_allocated > 0]
        if free and used:
            assert max(b.expiry for b in used) <= min(b.expiry for b in free)
        assert not allocation.drift().exists()
        transaction.set_rollback(True)
    assert not SalesOrder.objects.exists()
