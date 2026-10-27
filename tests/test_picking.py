"""M2 waves and picking: plan, release, scan-confirm, short picks with re-allocation, offline replay."""

import uuid
from decimal import Decimal

import pytest
from django.db import transaction

from apps.masterdata.models import Item, ItemBarcode, Site
from apps.oms import services as oms
from apps.platform import outbox
from apps.platform.models import ExceptionCase, OutboxEvent, Role
from apps.wms import allocation, ledger
from apps.wms.models import Allocation, InventoryBalance, InventoryMovement, Location, PickTask, PickWave

from .conftest import api_for, make_user

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def floor(make_client):
    sdb = make_client("SDB", "4111111111")
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    store = Site.objects.create(code="SDB-STORE", name="Store", kind="delivery_address", party=sdb, address="1 Rd")
    water = Item.objects.create(client=sdb, sku="WATER", description="Water")
    juice = Item.objects.create(client=sdb, sku="JUICE", description="Juice")
    ItemBarcode.objects.create(item=water, client=sdb, barcode="6001234000017", uom="EA")
    bins = {
        code: Location.objects.create(site=teak, code=code, kind="storage", pick_sequence=seq, pallet_capacity=5)
        for code, seq in [("A-01", 10), ("A-02", 20), ("B-01", 30)]
    }
    sup = make_user("sup@example.com", Role.WAREHOUSE_SUPERVISOR, site_ids=[teak.pk], all_clients=True)
    op = make_user("op@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[teak.pk], all_clients=True)
    op2 = make_user("op2@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[teak.pk], all_clients=True)
    return {
        "sdb": sdb,
        "teak": teak,
        "store": store,
        "water": water,
        "juice": juice,
        "bins": bins,
        "sup": api_for(sup),
        "op": api_for(op),
        "op2": api_for(op2),
    }


def put(floor, code, item, qty):
    key = ledger.StockKey(floor["sdb"].pk, item.pk, floor["bins"][code].pk, "", None, "available")
    with transaction.atomic():
        ledger.post(key, D(qty), kind="opening")


def deliver():
    for _ in range(10):
        published = outbox.publish_pending()
        stats = outbox.deliver_pending()
        if not published and not stats.delivered:
            return


def order(floor, *lines):
    so = oms.create_sales_order(
        client=floor["sdb"],
        site=floor["teak"],
        ship_to=floor["store"],
        client_ref="",
        lines=[oms.OrderLineIn(sku=sku, qty=D(qty)) for sku, qty in lines],
    )
    deliver()
    so.refresh_from_db()
    return so


def released_wave(floor, *orders):
    body = {"site_id": str(floor["teak"].pk), "sales_order_ids": [str(o.pk) for o in orders]}
    response = floor["sup"].post("/waves", body)
    assert response.status_code == 201, response.content
    wave = response.json()
    assert wave["status"] == "planned"
    response = floor["sup"].post(f"/waves/{wave['id']}/release")
    assert response.status_code == 200, response.content
    return response.json()


def tasks(floor, wave_id):
    response = floor["op"].get("/pick-tasks", wave_id=wave_id)
    assert response.status_code == 200, response.content
    return sorted(response.json()["items"], key=lambda t: t["pick_sequence"])


def on_hand(floor, code, item):
    b = InventoryBalance.objects.filter(location=floor["bins"][code], item=item).first()
    return (b.qty_on_hand, b.qty_allocated) if b else (D(0), D(0))


def test_release_and_pick_a_wave_end_to_end(floor):
    put(floor, "A-01", floor["water"], 10)
    put(floor, "B-01", floor["juice"], 10)
    so1 = order(floor, ("WATER", 6), ("JUICE", 4))
    so2 = order(floor, ("WATER", 4))
    wave = released_wave(floor, so1, so2)
    assert wave["tasks_total"] == 3 and wave["tasks_open"] == 3
    so1.refresh_from_db()
    assert so1.status == "picking"
    # Orders in picking can't be cancelled any more.
    assert floor["sup"].post(f"/orders/{so1.pk}/cancel", {"reason": "late"}).status_code == 409

    for task in tasks(floor, wave["id"]):
        assert floor["op"].post(f"/tasks/{task['id']}/claim").json()["assigned_to"]
        code = "6001234000017" if task["sku"] == "WATER" else "JUICE"  # barcode or SKU both work
        body = {"location_code": task["from_location"].lower(), "item_code": code, "qty": task["qty"]}
        response = floor["op"].post(f"/pick-tasks/{task['id']}/confirm", body)
        assert response.status_code == 200, response.content
        assert response.json()["status"] == "done"

    assert on_hand(floor, "A-01", floor["water"]) == (D(0), D(0))
    assert on_hand(floor, "B-01", floor["juice"]) == (D(6), D(0))
    assert InventoryMovement.objects.filter(kind="pick").count() == 3
    assert set(Allocation.objects.values_list("status", flat=True)) == {"picked"}
    done = floor["sup"].get(f"/waves/{wave['id']}").json()
    assert done["status"] == "completed" and done["tasks_open"] == 0
    event = OutboxEvent.objects.get(type="wms.pick_wave.completed")
    assert event.payload["tasks_total"] == 3 and event.payload["tasks_short"] == 0
    assert sorted(event.payload["sales_order_ids"]) == sorted([str(so1.pk), str(so2.pk)])
    assert not allocation.drift().exists() and not ledger.drift().exists()


def test_scans_must_match_the_task(floor):
    put(floor, "A-01", floor["water"], 10)
    put(floor, "A-02", floor["juice"], 10)
    wave = released_wave(floor, order(floor, ("WATER", 5)))
    task = tasks(floor, wave["id"])[0]
    url = f"/pick-tasks/{task['id']}/confirm"
    wrong_bin = floor["op"].post(url, {"location_code": "A-02", "item_code": "WATER", "qty": "5"})
    assert wrong_bin.status_code == 422 and wrong_bin.json()["title"] == "Wrong bin"
    wrong_item = floor["op"].post(url, {"location_code": "A-01", "item_code": "JUICE", "qty": "5"})
    assert wrong_item.status_code == 422 and wrong_item.json()["title"] == "Wrong item"
    wrong_qty = floor["op"].post(url, {"location_code": "A-01", "item_code": "WATER", "qty": "4"})
    assert wrong_qty.status_code == 422 and wrong_qty.json()["title"] == "Quantity differs"
    assert on_hand(floor, "A-01", floor["water"]) == (D(10), D(5))


def test_claimed_task_belongs_to_its_operator(floor):
    put(floor, "A-01", floor["water"], 10)
    wave = released_wave(floor, order(floor, ("WATER", 5)))
    task = tasks(floor, wave["id"])[0]
    assert floor["op"].post(f"/tasks/{task['id']}/claim").status_code == 200
    assert floor["op2"].post(f"/tasks/{task['id']}/claim").status_code == 409
    body = {"location_code": "A-01", "item_code": "WATER", "qty": "5"}
    assert floor["op2"].post(f"/pick-tasks/{task['id']}/confirm", body).status_code == 409


def test_short_pick_reallocates_from_another_bin(floor):
    put(floor, "A-01", floor["water"], 5)
    put(floor, "A-02", floor["water"], 10)
    other = order(floor, ("WATER", 3))  # FIFO: takes 3 of A-01's 5
    so = order(floor, ("WATER", 6))  # 2 from A-01, 4 from A-02
    wave = released_wave(floor, so)
    first = next(t for t in tasks(floor, wave["id"]) if t["from_location"] == "A-01")
    assert D(first["qty"]) == 2
    body = {"location_code": "A-01", "qty_found": "1", "reason": "Only one left in the bin"}
    response = floor["op"].post(f"/pick-tasks/{first['id']}/short", body)
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "short" and D(response.json()["qty_picked"]) == 1

    remaining = tasks(floor, wave["id"])
    assert sorted((t["from_location"], D(t["qty"])) for t in remaining) == [("A-02", D(1)), ("A-02", D(4))]
    assert on_hand(floor, "A-01", floor["water"]) == (D(4), D(3))  # `other` still holds its 3
    case = ExceptionCase.objects.get(kind="inventory.short_pick")
    assert case.detail["location"] == "A-01" and case.detail["found"] == "1.000"
    assert not ExceptionCase.objects.filter(kind="pick.short").exists()

    for task in remaining:
        body = {"location_code": "A-02", "item_code": "WATER", "qty": task["qty"]}
        assert floor["op"].post(f"/pick-tasks/{task['id']}/confirm", body).status_code == 200
    completed = OutboxEvent.objects.get(type="wms.pick_wave.completed")
    assert completed.payload["tasks_total"] == 3 and completed.payload["tasks_short"] == 1
    picked = sum(-m.qty_delta for m in InventoryMovement.objects.filter(kind="pick"))
    assert picked == 6  # 1 + 4 + 1: the order is complete despite the short
    other.refresh_from_db()
    assert other.status == "allocated"
    assert not allocation.drift().exists() and not ledger.drift().exists()


def test_short_pick_with_no_stock_left_is_an_exception(floor):
    put(floor, "A-01", floor["water"], 5)
    so = order(floor, ("WATER", 5))
    wave = released_wave(floor, so)
    task = tasks(floor, wave["id"])[0]
    body = {"location_code": "A-01", "qty_found": "0", "reason": "Bin empty"}
    assert floor["op"].post(f"/pick-tasks/{task['id']}/short", body).status_code == 200
    case = ExceptionCase.objects.get(kind="pick.short")
    assert case.severity == "high" and case.detail["qty_covered"] == "0.000"
    shorted = OutboxEvent.objects.get(type="wms.allocation.shorted")
    assert shorted.payload["qty_requested"] == "5.000"
    assert PickWave.objects.get().status == "completed"
    deliver()
    so.refresh_from_db()
    assert so.status == "picking"  # a late allocation event never moves an order out of picking
    assert on_hand(floor, "A-01", floor["water"]) == (D(5), D(0))  # stock untouched, the bin awaits a count
    assert not allocation.drift().exists()


def test_release_drops_orders_that_changed(floor):
    put(floor, "A-01", floor["water"], 10)
    keep = order(floor, ("WATER", 2))
    drop = order(floor, ("WATER", 2))
    body = {"site_id": str(floor["teak"].pk), "sales_order_ids": [str(keep.pk), str(drop.pk)]}
    wave = floor["sup"].post("/waves", body).json()
    oms.cancel_sales_order(drop.pk, "Customer cancelled")
    deliver()
    released = floor["sup"].post(f"/waves/{wave['id']}/release").json()
    assert [o["number"] for o in released["orders"]] == [keep.number]
    assert released["tasks_total"] == 1
    assert floor["sup"].post(f"/waves/{wave['id']}/release").status_code == 409


def test_only_allocated_orders_can_be_waved(floor):
    put(floor, "A-01", floor["water"], 3)
    short = order(floor, ("WATER", 10))
    assert short.status == "backordered"
    response = floor["sup"].post("/waves", {"site_id": str(floor["teak"].pk), "sales_order_ids": [str(short.pk)]})
    assert response.status_code == 422 and "not allocated" in response.json()["detail"]
    assert (
        floor["op"].post("/waves", {"site_id": str(floor["teak"].pk), "sales_order_ids": [str(short.pk)]}).status_code
        == 403
    )


def test_one_wave_per_order(floor):
    put(floor, "A-01", floor["water"], 10)
    so = order(floor, ("WATER", 2))
    body = {"site_id": str(floor["teak"].pk), "sales_order_ids": [str(so.pk)]}
    assert floor["sup"].post("/waves", body).status_code == 201
    second = floor["sup"].post("/waves", body)
    assert second.status_code == 422 and "already in a wave" in second.json()["detail"]


def _cmd(type_, **payload):
    return {
        "command_id": str(uuid.uuid4()),
        "type": type_,
        "payload": payload,
        "recorded_at": "2026-10-27T10:00:00+02:00",
    }


def push(api, *commands):
    response = api.client.post(
        "/api/v1/sync/push", {"device_id": "TC52-01", "commands": list(commands)}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    return response.json()["results"]


def test_offline_pick_commands_apply_once(floor):
    put(floor, "A-01", floor["water"], 10)
    wave = released_wave(floor, order(floor, ("WATER", 4)))
    pull = floor["op"].get("/sync/pull", site_id=str(floor["teak"].pk)).json()
    assert [t["sku"] for t in pull["pick_tasks"]] == ["WATER"]
    task_id = pull["pick_tasks"][0]["id"]
    claim = _cmd("pick.claim", task_id=task_id)
    confirm = _cmd("pick.confirm", task_id=task_id, location_code="A-01", item_code="6001234000017", qty="4")
    results = push(floor["op"], claim, confirm)
    assert [r["status"] for r in results] == ["applied", "applied"]
    assert [r["status"] for r in push(floor["op"], confirm)] == ["applied"]  # replay returns the stored outcome
    assert InventoryMovement.objects.filter(kind="pick").count() == 1
    assert on_hand(floor, "A-01", floor["water"]) == (D(6), D(0))
    late = push(floor["op"], _cmd("pick.short", task_id=task_id, location_code="A-01", qty_found="0", reason="x"))
    assert late[0]["status"] == "rejected" and late[0]["problem"]["title"] == "Task already finished"
    assert PickTask.objects.get().status == "done"
    assert floor["sup"].get(f"/waves/{wave['id']}").json()["status"] == "completed"
