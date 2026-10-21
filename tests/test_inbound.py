"""M1 inbound: ASNs, receiving, the inventory ledger and putaway."""

import uuid
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction

from apps.masterdata.models import Item, ItemBarcode, ItemUom, Site
from apps.oms.models import InboundNotice
from apps.platform import outbox
from apps.platform.apiutils import Problem
from apps.platform.models import Document, ExceptionCase, OutboxEvent, Role
from apps.wms import ledger
from apps.wms.models import InventoryBalance, InventoryMovement, Location, PutawayTask

from .conftest import api_for, make_user

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def world(make_client):
    sdb = make_client("SDB", "4111111111")
    other = make_client("OTHER", "4222222222")
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    dbn = Site.objects.create(code="DBN", name="Durban", kind="warehouse")
    water = Item.objects.create(client=sdb, sku="SDB-001", description="Water 500ml", units_per_pallet=1440)
    ItemUom.objects.create(item=water, uom="EA", factor=1)
    ItemUom.objects.create(item=water, uom="CS", factor=24)
    ItemBarcode.objects.create(item=water, client=sdb, barcode="6001234000017", uom="EA")
    juice = Item.objects.create(
        client=sdb, sku="SDB-002", description="Juice 1l", lot_tracked=True, expiry_tracked=True
    )
    Item.objects.create(client=other, sku="OTH-001", description="Other client's item")
    locations = {
        code: Location.objects.create(site=teak, code=code, kind=kind, pick_sequence=seq, pallet_capacity=cap)
        for code, kind, seq, cap in [
            ("DOCK-1", "dock", 0, 0),
            ("A-01", "storage", 10, 2),
            ("A-02", "storage", 20, 1),
            ("A-03", "storage", 30, 1),
            ("Q-01", "quarantine", 90, 5),
        ]
    }
    Location.objects.create(site=dbn, code="DOCK-1", kind="dock", pallet_capacity=0)
    operator = make_user("op@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[teak.pk], all_clients=True)
    return {
        "sdb": sdb,
        "other": other,
        "teak": teak,
        "dbn": dbn,
        "water": water,
        "juice": juice,
        "loc": locations,
        "operator": operator,
    }


def _asn(api, *, ref="PO-1", lines=None, site="TEAK"):
    body = {
        "site_code": site,
        "expected_at": "2026-11-02T08:00:00+02:00",
        "client_ref": ref,
        "lines": lines or [{"sku": "SDB-001", "qty": "10", "uom": "CS"}],
    }
    return api.post("/inbound-notices", body)


def _deliver_events():
    outbox.publish_pending()
    return outbox.deliver_pending()


# --- Inbound notices ---------------------------------------------------------------------------------------


def test_client_system_submits_an_asn(world, bearer_api):
    api = bearer_api(world["sdb"])
    response = _asn(api, lines=[{"sku": "SDB-001", "qty": "10", "uom": "cs"}, {"barcode": "6001234000017", "qty": "6"}])
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["number"].startswith("ASN-") and body["status"] == "accepted"
    assert [(line["sku"], line["qty"], line["uom"]) for line in body["lines"]] == [
        ("SDB-001", "10.000", "CS"),
        ("SDB-001", "6.000", "EA"),
    ]
    event = OutboxEvent.objects.get(type="oms.inbound_notice.accepted")
    assert event.client_id == world["sdb"].pk and event.site_id == world["teak"].pk
    assert len(event.payload["lines"]) == 2

    assert _asn(api).status_code == 409  # same client_ref
    assert _asn(api, ref="PO-2", lines=[{"sku": "OTH-001", "qty": "1"}]).status_code == 422  # other client's item
    assert _asn(api, ref="PO-3", lines=[{"sku": "SDB-001", "qty": "1", "uom": "PALLET"}]).status_code == 422


def test_clients_only_see_their_own_notices(world, bearer_api):
    notice_id = _asn(bearer_api(world["sdb"])).json()["id"]
    other = bearer_api(world["other"])
    assert other.get(f"/inbound-notices/{notice_id}").status_code == 404
    assert other.get("/inbound-notices").json()["items"] == []


def test_cancel_is_allowed_only_before_receiving(world, bearer_api):
    api = bearer_api(world["sdb"])
    first = _asn(api, ref="PO-A").json()
    assert api.post(f"/inbound-notices/{first['id']}/cancel", {"reason": ""}).status_code == 422
    cancelled = api.post(f"/inbound-notices/{first['id']}/cancel", {"reason": "Supplier delay"})
    assert cancelled.json()["status"] == "cancelled"
    assert (
        api_for(world["operator"])
        .post("/receipts", {"inbound_notice_id": first["id"], "dock_code": "DOCK-1"})
        .status_code
        == 409
    )

    second = _asn(api, ref="PO-B").json()
    op = api_for(world["operator"])
    assert op.post("/receipts", {"inbound_notice_id": second["id"], "dock_code": "DOCK-1"}).status_code == 201
    response = api.post(f"/inbound-notices/{second['id']}/cancel", {"reason": "Too late"})
    assert response.status_code == 409


# --- Receiving -------------------------------------------------------------------------------------------------


def _receive(world, bearer_api, lines, asn_lines=None):
    notice = _asn(bearer_api(world["sdb"]), lines=asn_lines).json()
    op = api_for(world["operator"])
    receipt = op.post("/receipts", {"inbound_notice_id": notice["id"], "dock_code": "DOCK-1"})
    assert receipt.status_code == 201, receipt.content
    receipt_id = receipt.json()["id"]
    for line in lines:
        response = op.post(f"/receipts/{receipt_id}/lines", line)
        assert response.status_code == 201, response.content
    completed = op.post(f"/receipts/{receipt_id}/complete")
    assert completed.status_code == 200, completed.content
    return notice, completed.json(), op


def test_full_receipt_posts_stock_creates_putaway_and_closes_the_asn(world, bearer_api):
    notice, receipt, _ = _receive(
        world, bearer_api, [{"sku": "SDB-001", "qty": "6", "uom": "CS"}, {"barcode": "6001234000017", "qty": "96"}]
    )
    assert receipt["status"] == "completed"
    dock = InventoryBalance.objects.get(location=world["loc"]["DOCK-1"], item=world["water"])
    assert dock.qty_on_hand == D("240")  # 6 × 24 + 96 = 10 cases
    assert InventoryMovement.objects.filter(kind="receive").count() == 2
    assert PutawayTask.objects.filter(status="open").count() == 2
    assert not ExceptionCase.objects.filter(kind="receipt.variance").exists()

    event = OutboxEvent.objects.get(type="wms.receipt.completed")
    assert event.payload["variance"] == [] and len(event.payload["lines"]) == 2
    assert _deliver_events().delivered == 1
    closed = InboundNotice.objects.get(pk=notice["id"])
    assert closed.status == "closed"
    assert closed.lines.get().qty_received == D("10")


def test_short_and_unexpected_lines_raise_a_variance_case(world, bearer_api):
    notice, _, _ = _receive(
        world,
        bearer_api,
        [
            {"sku": "SDB-001", "qty": "8", "uom": "CS"},
            {"sku": "SDB-002", "qty": "5", "lot": "L1", "expiry": "2027-06-30"},
        ],
    )
    case = ExceptionCase.objects.get(kind="receipt.variance")
    variance = case.detail["variance"]
    assert {(v["expected_base_qty"], v["received_base_qty"]) for v in variance} == {
        ("240.000", "192.000"),
        ("0", "5.000"),
    }
    _deliver_events()
    assert InboundNotice.objects.get(pk=notice["id"]).lines.get().qty_received == D("8")


def test_receiving_rules(world, bearer_api):
    notice = _asn(bearer_api(world["sdb"])).json()
    op = api_for(world["operator"])
    receipt_id = op.post("/receipts", {"inbound_notice_id": notice["id"], "dock_code": "DOCK-1"}).json()["id"]

    def add(**line):
        return op.post(f"/receipts/{receipt_id}/lines", line)

    assert add(sku="SDB-002", qty="1").status_code == 422  # lot and expiry required
    assert add(sku="SDB-001", qty="1", condition="damaged").status_code == 422  # photo required
    assert add(sku="OTH-001", qty="1").status_code == 422  # not this client's item
    assert add(sku="SDB-001", qty="1", uom="PALLET").status_code == 422

    command_id = str(uuid.uuid4())
    first = add(sku="SDB-001", qty="1", command_id=command_id)
    replay = add(sku="SDB-001", qty="1", command_id=command_id)
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]

    assert op.post(f"/receipts/{receipt_id}/complete").status_code == 200
    assert add(sku="SDB-001", qty="1").status_code == 409  # completed receipts are closed
    again = op.post(f"/receipts/{receipt_id}/complete")
    assert again.status_code == 200
    assert InventoryMovement.objects.filter(kind="receive").count() == 1  # completing twice posts once


def test_damaged_stock_needs_a_photo_and_is_quarantined(world, bearer_api):
    photo = Document.objects.create(
        client_id=world["sdb"].pk,
        kind="receipt_photo",
        storage_key="k/1.jpg",
        mime="image/jpeg",
        size_bytes=10,
        sha256="0" * 64,
        captured_at="2026-11-02T08:00:00Z",
    )
    _, _, op = _receive(
        world,
        bearer_api,
        [{"sku": "SDB-001", "qty": "10", "uom": "CS", "condition": "damaged", "photo_document_ids": [str(photo.pk)]}],
    )
    photo.refresh_from_db()
    assert photo.linked_type == "wms.receiptline"
    task = PutawayTask.objects.get()
    assert task.from_balance.status == "damaged" and task.suggested_location.code == "Q-01"
    response = op.post(f"/putaway-tasks/{task.pk}/confirm", {"to_location_code": "A-01", "reason": "full"})
    assert response.status_code == 422  # damaged stock only into quarantine
    assert op.post(f"/putaway-tasks/{task.pk}/confirm", {"to_location_code": "Q-01"}).status_code == 200


def test_operators_cannot_receive_at_other_sites(world, bearer_api):
    notice = _asn(bearer_api(world["sdb"]), site="DBN").json()
    response = api_for(world["operator"]).post("/receipts", {"inbound_notice_id": notice["id"], "dock_code": "DOCK-1"})
    assert response.status_code == 404


# --- Putaway ----------------------------------------------------------------------------------------------------


def test_putaway_suggests_same_sku_first_then_the_first_empty_bin(world, bearer_api):
    loc = world["loc"]
    _, _, op = _receive(
        world,
        bearer_api,
        [{"sku": "SDB-001", "qty": "5", "uom": "CS"}, {"sku": "SDB-001", "qty": "5", "uom": "CS"}],
    )
    first, second = PutawayTask.objects.order_by("created_at", "id")
    assert first.suggested_location == loc["A-01"]
    assert second.suggested_location == loc["A-02"]  # A-01 is reserved by the first task (one empty bin each)

    assert op.post(f"/tasks/{first.pk}/claim").status_code == 200
    mine = op.get("/tasks", mine="true").json()["items"]
    assert [t["id"] for t in mine] == [str(first.pk)]
    assert op.post(f"/putaway-tasks/{first.pk}/confirm", {"to_location_code": "A-01"}).status_code == 200

    # Override without a reason is refused; with one it is recorded.
    assert op.post(f"/putaway-tasks/{second.pk}/confirm", {"to_location_code": "A-03"}).status_code == 422
    response = op.post(f"/putaway-tasks/{second.pk}/confirm", {"to_location_code": "A-03", "reason": "A-02 blocked"})
    assert response.status_code == 200
    assert PutawayTask.objects.get(pk=second.pk).override_reason == "A-02 blocked"

    stock = {b.location.code: b.qty_on_hand for b in InventoryBalance.objects.filter(qty_on_hand__gt=0)}
    assert stock == {"A-01": D("120"), "A-03": D("120")}

    # A new receipt of the same item now goes to A-01 first: it holds the SKU and has a second slot.
    notice = _asn(bearer_api(world["sdb"]), ref="PO-9").json()
    receipt_id = op.post("/receipts", {"inbound_notice_id": notice["id"], "dock_code": "DOCK-1"}).json()["id"]
    op.post(f"/receipts/{receipt_id}/lines", {"sku": "SDB-001", "qty": "10", "uom": "CS"})
    op.post(f"/receipts/{receipt_id}/complete")
    assert PutawayTask.objects.filter(status="open").get().suggested_location == loc["A-01"]


def test_putaway_confirm_is_idempotent_by_command_id(world, bearer_api):
    _, _, op = _receive(world, bearer_api, [{"sku": "SDB-001", "qty": "10", "uom": "CS"}])
    task = PutawayTask.objects.get()
    command = {"to_location_code": "A-01", "command_id": str(uuid.uuid4())}
    assert op.post(f"/putaway-tasks/{task.pk}/confirm", command).status_code == 200
    assert op.post(f"/putaway-tasks/{task.pk}/confirm", command).status_code == 200
    assert InventoryMovement.objects.filter(kind="putaway").count() == 2  # one transfer: out of dock, into A-01
    other_command = {"to_location_code": "A-01", "command_id": str(uuid.uuid4())}
    assert op.post(f"/putaway-tasks/{task.pk}/confirm", other_command).status_code == 409


def test_another_operators_claimed_task_is_off_limits(world, bearer_api):
    _receive(world, bearer_api, [{"sku": "SDB-001", "qty": "10", "uom": "CS"}])
    task = PutawayTask.objects.get()
    colleague = api_for(
        make_user("op2@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[world["teak"].pk], all_clients=True)
    )
    assert api_for(world["operator"]).post(f"/tasks/{task.pk}/claim").status_code == 200
    assert colleague.post(f"/tasks/{task.pk}/claim").status_code == 409
    assert colleague.post(f"/putaway-tasks/{task.pk}/confirm", {"to_location_code": "A-01"}).status_code == 409


# --- Ledger and inventory query ---------------------------------------------------------------------------------


def test_ledger_refuses_negative_stock_and_is_append_only(world, monkeypatch):
    key = ledger.StockKey(world["sdb"].pk, world["water"].pk, world["loc"]["A-01"].pk, "", None, "available")
    with transaction.atomic():
        movement = ledger.post(key, D("5"), kind="opening")
    with pytest.raises(Problem, match="Insufficient stock"), transaction.atomic():
        ledger.post(key, D("-6"), kind="adjust")
    with pytest.raises(DatabaseError), transaction.atomic():
        InventoryMovement.objects.filter(pk=movement.pk).update(qty_delta=D("50"))
    assert list(ledger.drift()) == []
    monkeypatch.setattr(connection, "in_atomic_block", False)
    with pytest.raises(RuntimeError, match="inside transaction"):
        ledger.post(key, D("1"), kind="opening")


def test_reconciliation_flags_a_balance_changed_outside_the_ledger(world):
    key = ledger.StockKey(world["sdb"].pk, world["water"].pk, world["loc"]["A-01"].pk, "", None, "available")
    with transaction.atomic():
        ledger.post(key, D("5"), kind="opening")
    InventoryBalance.objects.update(qty_on_hand=D("7"))  # a bypass the ledger would never do
    call_command("reconcile_inventory")
    case = ExceptionCase.objects.get(kind="inventory.ledger_drift")
    assert case.detail["on_hand"] == "7.000" and case.detail["ledger"] == "5.000"


def test_inventory_query_is_scoped(world, bearer_api):
    _receive(world, bearer_api, [{"sku": "SDB-001", "qty": "10", "uom": "CS"}])
    rows = bearer_api(world["sdb"]).get("/inventory").json()["items"]
    assert [(r["sku"], r["location_code"], r["qty_on_hand"], r["qty_available"]) for r in rows] == [
        ("SDB-001", "DOCK-1", "240.000", "240.000")
    ]
    assert bearer_api(world["other"]).get("/inventory").json()["items"] == []
    durban_op = api_for(
        make_user("dbn@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[world["dbn"].pk], all_clients=True)
    )
    assert durban_op.get("/inventory").json()["items"] == []
