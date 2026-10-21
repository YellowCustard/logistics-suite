"""M1 inventory control: blind cycle counts with approval, direct corrections and bin moves."""

import uuid
from decimal import Decimal

import pytest
from django.db import transaction

from apps.masterdata.models import Item, ItemBarcode, ItemUom, Site
from apps.platform import outbox
from apps.platform.models import ExceptionCase, OutboxEvent, Role
from apps.wms import ledger
from apps.wms.models import InventoryBalance, InventoryMovement, Location

from .conftest import api_for, make_user

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def wh(make_client):
    sdb = make_client("SDB", "4111111111")
    other = make_client("OTHER", "4222222222")
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    water = Item.objects.create(client=sdb, sku="SDB-001", description="Water 500ml")
    ItemUom.objects.create(item=water, uom="CS", factor=24)
    ItemBarcode.objects.create(item=water, client=sdb, barcode="6001234000017", uom="EA")
    Item.objects.create(client=other, sku="SDB-001", description="Same SKU, other client")
    bins = {
        code: Location.objects.create(site=teak, code=code, kind=kind, pick_sequence=n, pallet_capacity=2)
        for n, (code, kind) in enumerate(
            [("A-01", "storage"), ("A-02", "storage"), ("A-03", "storage"), ("Q-01", "quarantine")]
        )
    }
    with transaction.atomic():
        for code, qty in [("A-01", "240"), ("A-02", "100")]:
            key = ledger.StockKey(sdb.pk, water.pk, bins[code].pk, "", None, "available")
            ledger.post(key, D(qty), kind="opening")
    grant = {"site_ids": [teak.pk], "all_clients": True}
    people = {
        name: make_user(f"{name}@example.com", role, **grant)
        for name, role in [
            ("op", Role.WAREHOUSE_OPERATOR),
            ("op2", Role.WAREHOUSE_OPERATOR),
            ("sup", Role.WAREHOUSE_SUPERVISOR),
            ("mgr", Role.SITE_MANAGER),
            ("mgr2", Role.SITE_MANAGER),
        ]
    }
    return {"sdb": sdb, "other": other, "teak": teak, "water": water, "bins": bins, **people}


def stock(wh, code, status="available"):
    balance = InventoryBalance.objects.filter(location=wh["bins"][code], item=wh["water"], status=status).first()
    return balance.qty_on_hand if balance else D(0)


def _count(wh, codes=("A-01", "A-02")):
    response = api_for(wh["sup"]).post("/cycle-counts", {"site_id": str(wh["teak"].pk), "location_codes": list(codes)})
    assert response.status_code == 201, response.content
    return response.json()["id"]


def _line(wh, who, count_id, **line):
    body = {"client_id": str(wh["sdb"].pk), "sku": "SDB-001", **line}
    return api_for(wh[who]).post(f"/cycle-counts/{count_id}/lines", body)


def _deliver():
    outbox.publish_pending()
    return outbox.deliver_pending()


# --- Cycle counts --------------------------------------------------------------------------------------------


def test_counts_are_blind_for_operators(wh):
    count_id = _count(wh)
    assert _line(wh, "op", count_id, location_code="A-01", qty="10", uom="CS").status_code == 201
    assert _line(wh, "op2", count_id, location_code="A-02", qty="100").status_code == 201

    blind = api_for(wh["op"]).get(f"/cycle-counts/{count_id}").json()
    assert [(line["location_code"], line["counted_qty"]) for line in blind["lines"]] == [("A-01", "240.000")]
    assert "system_qty" not in blind["lines"][0]

    full = api_for(wh["sup"]).get(f"/cycle-counts/{count_id}").json()
    assert {(line["location_code"], line["system_qty"], line["variance"]) for line in full["lines"]} == {
        ("A-01", "240.000", "0.000"),
        ("A-02", "100.000", "0.000"),
    }


def test_variance_within_tolerance_posts_on_submit(wh):
    count_id = _count(wh)
    _line(wh, "op", count_id, location_code="A-01", qty="239")
    _line(wh, "op", count_id, location_code="A-02", qty="100")
    submitted = api_for(wh["sup"]).post(f"/cycle-counts/{count_id}/submit").json()
    assert submitted["status"] == "posted"
    assert stock(wh, "A-01") == D("239")
    movement = InventoryMovement.objects.get(kind="adjust")
    assert movement.qty_delta == D("-1") and movement.source_type == "wms.cyclecount"
    event = OutboxEvent.objects.get(type="wms.inventory.adjusted")
    assert event.payload["approved_by"] is None and event.payload["qty_before"] == "240.000"
    _deliver()
    assert not ExceptionCase.objects.filter(kind="inventory.adjustment_review").exists()


def test_large_variance_and_uncounted_stock_need_an_independent_manager(wh):
    count_id = _count(wh)
    _line(wh, "op", count_id, location_code="A-01", qty="200")  # A-02 is never counted: counts as zero
    _line(wh, "mgr", count_id, location_code="A-01", qty="200")  # the manager recounts, so can't approve
    sup = api_for(wh["sup"])
    submitted = sup.post(f"/cycle-counts/{count_id}/submit").json()
    assert submitted["status"] == "awaiting_approval"
    flagged = {(line["location_code"], line["variance"], line["needs_approval"]) for line in submitted["lines"]}
    assert flagged == {("A-01", "-40.000", True), ("A-02", "-100.000", True)}
    assert stock(wh, "A-01") == D("240")  # nothing posted yet

    assert sup.post(f"/cycle-counts/{count_id}/approve", {}).status_code == 403  # not a site manager
    assert api_for(wh["mgr"]).post(f"/cycle-counts/{count_id}/approve", {}).status_code == 403  # counted it
    approved = api_for(wh["mgr2"]).post(f"/cycle-counts/{count_id}/approve", {"note": "Recounted twice"})
    assert approved.json()["status"] == "posted"
    assert (stock(wh, "A-01"), stock(wh, "A-02")) == (D("200"), D("0"))
    events = OutboxEvent.objects.filter(type="wms.inventory.adjusted")
    assert {e.payload["approved_by"] for e in events} == {str(wh["mgr2"].pk)}
    _deliver()
    assert not ExceptionCase.objects.filter(kind="inventory.adjustment_review").exists()


def test_rejection_needs_a_reason_and_posts_nothing(wh):
    count_id = _count(wh)
    _line(wh, "op", count_id, location_code="A-01", qty="1")
    api_for(wh["sup"]).post(f"/cycle-counts/{count_id}/submit")
    mgr = api_for(wh["mgr"])
    assert mgr.post(f"/cycle-counts/{count_id}/reject", {"note": ""}).status_code == 422
    assert mgr.post(f"/cycle-counts/{count_id}/reject", {"note": "Wrong bin"}).json()["status"] == "rejected"
    assert stock(wh, "A-01") == D("240") and not InventoryMovement.objects.filter(kind="adjust").exists()
    assert _count(wh)  # the bins are free for a new count


def test_overlapping_counts_recounts_and_replays(wh):
    count_id = _count(wh, ["A-01"])
    response = api_for(wh["sup"]).post("/cycle-counts", {"site_id": str(wh["teak"].pk), "location_codes": ["A-01"]})
    assert response.status_code == 409

    command = str(uuid.uuid4())
    first = _line(wh, "op", count_id, location_code="A-01", qty="5", command_id=command).json()
    replay = _line(wh, "op", count_id, location_code="A-01", qty="5", command_id=command).json()
    assert first["id"] == replay["id"]
    recount = _line(wh, "op", count_id, location_code="A-01", qty="240").json()
    assert recount["id"] == first["id"] and recount["counted_qty"] == "240.000"
    late = _line(wh, "op", count_id, location_code="A-01", qty="5", command_id=command).json()
    assert late["counted_qty"] == "240.000"
    assert _line(wh, "op", count_id, location_code="A-02", qty="1").status_code == 422  # not in this count


def test_stock_moved_after_counting_does_not_create_a_false_variance(wh):
    count_id = _count(wh, ["A-01"])
    _line(wh, "op", count_id, location_code="A-01", qty="240")
    move = {
        "site_id": str(wh["teak"].pk),
        "location_code": "A-01",
        "to_location_code": "A-03",
        "client_id": str(wh["sdb"].pk),
        "sku": "SDB-001",
        "qty": "40",
    }
    assert api_for(wh["sup"]).post("/inventory/moves", move).status_code == 201
    assert api_for(wh["sup"]).post(f"/cycle-counts/{count_id}/submit").json()["status"] == "posted"
    assert (stock(wh, "A-01"), stock(wh, "A-03")) == (D("200"), D("40"))
    assert not InventoryMovement.objects.filter(kind="adjust").exists()


# --- Direct corrections and moves -------------------------------------------------------------------------------


def _adjust(wh, who="sup", **fields):
    body = {
        "site_id": str(wh["teak"].pk),
        "location_code": "A-01",
        "client_id": str(wh["sdb"].pk),
        "sku": "SDB-001",
        **fields,
    }
    return api_for(wh[who]).post("/inventory/adjustments", body)


def test_direct_corrections_post_at_once_and_large_ones_are_reviewed(wh):
    assert _adjust(wh, qty_delta="-1", reason="Broken bottle").status_code == 201
    assert _adjust(wh, qty_delta="-2", uom="CS", reason="Damaged in aisle").status_code == 201
    assert stock(wh, "A-01") == D("191")  # 240 - 1 - 48
    _deliver()
    cases = ExceptionCase.objects.filter(kind="inventory.adjustment_review")
    assert cases.count() == 1 and cases.get().detail["qty_delta"] == "-48.000"

    assert _adjust(wh, qty_delta="-1", reason="").status_code == 422
    assert _adjust(wh, qty_delta="-1000", reason="x").status_code == 409  # below zero
    assert _adjust(wh, who="op", qty_delta="1", reason="x").status_code == 403


def test_codes_shared_by_two_clients_need_the_client_named(wh):
    response = _adjust(wh, client_id=None, qty_delta="1", reason="Found")
    assert response.status_code == 422 and response.json()["title"] == "Ambiguous item"
    by_barcode = _adjust(wh, client_id=None, sku=None, barcode="6001234000017", qty_delta="1", reason="Found")
    assert by_barcode.status_code == 201


def test_moves(wh):
    sup = api_for(wh["op"])  # operators move stock; only corrections need a supervisor
    move = {
        "site_id": str(wh["teak"].pk),
        "location_code": "A-01",
        "client_id": str(wh["sdb"].pk),
        "sku": "SDB-001",
        "to_location_code": "A-02",
        "qty": "1",
        "uom": "CS",
        "command_id": str(uuid.uuid4()),
    }
    assert sup.post("/inventory/moves", move).status_code == 201
    assert sup.post("/inventory/moves", move).status_code == 201  # replay
    assert (stock(wh, "A-01"), stock(wh, "A-02")) == (D("216"), D("124"))
    assert InventoryMovement.objects.filter(kind="move").count() == 2
    assert sup.post("/inventory/moves", move | {"command_id": None, "to_location_code": "A-01"}).status_code == 422
    damaged = move | {"command_id": None, "status": "damaged"}
    response = sup.post("/inventory/moves", damaged)
    assert response.status_code == 422 and response.json()["title"] == "Quarantine bin required"
    assert sup.post("/inventory/moves", damaged | {"to_location_code": "Q-01"}).status_code == 409  # none damaged
    assert sup.post("/inventory/moves", move | {"command_id": None, "qty": "300"}).status_code == 409
    assert list(ledger.drift()) == []
