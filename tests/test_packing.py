"""M2 packing: handling units with SSCCs, packing what was picked, closing, labels, order packed."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.masterdata.models import Item, ItemBarcode, Site
from apps.oms import services as oms
from apps.platform import outbox
from apps.platform.apiutils import Problem
from apps.platform.models import OutboxEvent, Role
from apps.wms import labels, ledger, sscc
from apps.wms.models import HandlingUnit, HandlingUnitContent, Location

from .conftest import api_for, make_user

pytestmark = pytest.mark.django_db

D = Decimal


# --- SSCC and labels ---------------------------------------------------------------------------------------------


def test_sscc_check_digit_matches_the_gs1_example():
    assert sscc.check_digit("00614141123456789") == "0"  # GS1 General Specifications example 006141411234567890
    assert sscc.is_valid("006141411234567890")
    assert not sscc.is_valid("006141411234567891")


def test_sscc_build_validates_its_parts():
    code = sscc.build("3", "6009876543", 42)
    assert len(code) == 18 and code.startswith("36009876543000042") and sscc.is_valid(code)
    with pytest.raises(ValueError):
        sscc.build("3", "12345", 1)  # prefix too short
    with pytest.raises(ValueError):
        sscc.build("3", "6009876543", 1_000_000)  # 6 serial digits left for a 10-digit prefix


def test_next_sscc_never_repeats_and_needs_a_prefix(settings):
    settings.GS1_COMPANY_PREFIX = "6009876"
    first, second = sscc.next_sscc(), sscc.next_sscc()
    assert first != second and sscc.is_valid(first) and sscc.is_valid(second)
    assert int(second[8:17]) == int(first[8:17]) + 1
    settings.GS1_COMPANY_PREFIX = ""
    with pytest.raises(Problem) as excinfo:
        sscc.next_sscc()
    assert excinfo.value.status == 503


def test_gs1_128_symbol_values():
    values = labels.gs1_128_values("00006141411234567890")
    assert values[:2] == [105, 102] and values[2:12] == [0, 0, 61, 41, 41, 12, 34, 56, 78, 90]
    expected = (105 + 102 * 1 + sum(v * i for i, v in enumerate(values[2:12], start=2))) % 103
    assert values[-1] == expected
    assert all(sum(map(int, p)) == 11 for p in labels.PATTERNS) and len(set(labels.PATTERNS)) == 106


LABEL = labels.LabelData("006141411234567890", "SDB", "ORD-1 / PO-(7)", "TEAK Teakwood", "Store", "1 line(s)", "9.5")


def test_zpl_label_encodes_gs1_128():
    text = labels.zpl(LABEL)
    assert text.startswith("^XA") and text.endswith("^XZ")
    assert "^FD>;>800006141411234567890^FS" in text
    assert "(00) 006141411234567890" in text


def test_pdf_label_is_well_formed():
    body = labels.pdf(LABEL)
    assert body.startswith(b"%PDF-1.4") and body.rstrip().endswith(b"%%EOF")
    xref_at = int(re.search(rb"startxref\n(\d+)", body).group(1))
    assert body[xref_at:].startswith(b"xref")
    offsets = [int(o) for o in re.findall(rb"(\d{10}) 00000 n", body)]
    assert [body[o:].split(b" ", 1)[0] for o in offsets] == [b"1", b"2", b"3", b"4", b"5"]
    assert b"PO-\\(7\\)" in body  # parentheses escaped in PDF strings


# --- Packing flow ------------------------------------------------------------------------------------------------


@pytest.fixture
def bench(make_client, settings):
    settings.GS1_COMPANY_PREFIX = "6009876"
    sdb = make_client("SDB", "4111111111")
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    store = Site.objects.create(
        code="SDB-STORE", name="Sandton Store", kind="delivery_address", party=sdb, address="1 Rd"
    )
    water = Item.objects.create(client=sdb, sku="WATER", description="Water")
    juice = Item.objects.create(client=sdb, sku="JUICE", description="Juice", lot_tracked=True, expiry_tracked=True)
    ItemBarcode.objects.create(item=water, client=sdb, barcode="6001234000017", uom="EA")
    bins = {
        code: Location.objects.create(site=teak, code=code, kind="storage", pick_sequence=seq, pallet_capacity=5)
        for code, seq in [("A-01", 10), ("A-02", 20), ("A-03", 30)]
    }
    sup = make_user("sup@example.com", Role.WAREHOUSE_SUPERVISOR, site_ids=[teak.pk], all_clients=True)
    op = make_user("op@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[teak.pk], all_clients=True)
    return {
        "sdb": sdb,
        "teak": teak,
        "store": store,
        "water": water,
        "juice": juice,
        "bins": bins,
        "sup": api_for(sup),
        "op": api_for(op),
    }


def put(bench, code, item, qty, lot="", days=None):
    expiry = timezone.localdate() + timedelta(days=days) if days else None
    key = ledger.StockKey(bench["sdb"].pk, item.pk, bench["bins"][code].pk, lot, expiry, "available")
    with transaction.atomic():
        ledger.post(key, D(qty), kind="opening")


def deliver():
    for _ in range(10):
        published = outbox.publish_pending()
        stats = outbox.deliver_pending()
        if not published and not stats.delivered:
            return


def picked_order(bench, *lines, pick=True):
    so = oms.create_sales_order(
        client=bench["sdb"],
        site=bench["teak"],
        ship_to=bench["store"],
        client_ref="PO-77",
        lines=[oms.OrderLineIn(sku=sku, qty=D(qty)) for sku, qty in lines],
    )
    deliver()
    wave = bench["sup"].post("/waves", {"site_id": str(bench["teak"].pk), "sales_order_ids": [str(so.pk)]}).json()
    assert bench["sup"].post(f"/waves/{wave['id']}/release").status_code == 200
    tasks = bench["op"].get("/pick-tasks", wave_id=wave["id"]).json()["items"]
    if pick:
        for t in tasks:
            body = {"location_code": t["from_location"], "item_code": t["sku"], "qty": t["qty"]}
            assert bench["op"].post(f"/pick-tasks/{t['id']}/confirm", body).status_code == 200
    so.refresh_from_db()
    return so, tasks


def open_unit(bench, so, kind="carton"):
    response = bench["op"].post("/handling-units", {"sales_order_id": str(so.pk), "kind": kind})
    assert response.status_code == 201, response.content
    return response.json()


def test_pack_close_label_and_order_packed(bench):
    put(bench, "A-01", bench["water"], 20)
    put(bench, "A-02", bench["juice"], 10, lot="J1", days=90)
    so, _ = picked_order(bench, ("WATER", 12), ("JUICE", 4))
    unit = open_unit(bench, so)
    assert sscc.is_valid(unit["sscc"]) and unit["status"] == "open"

    add = lambda body: bench["op"].post(f"/handling-units/{unit['id']}/contents", body)  # noqa: E731
    assert add({"item_code": "6001234000017", "qty": "5"}).status_code == 201
    assert add({"item_code": "WATER", "qty": "7"}).status_code == 201
    response = add({"item_code": "JUICE", "qty": "4"})
    assert response.status_code == 201
    contents = sorted((c["sku"], c["lot"], D(c["qty"])) for c in response.json()["contents"])
    assert contents == [("JUICE", "J1", D(4)), ("WATER", "", D(12))]  # two scans merged into one line

    bench_view = bench["op"].get("/pack-orders", site_id=str(bench["teak"].pk)).json()
    assert [o["number"] for o in bench_view] == [so.number]
    assert all(D(line["remaining"]) == 0 for line in bench_view[0]["lines"])

    label_before = bench["op"].get(f"/handling-units/{unit['id']}/label")
    assert label_before.status_code == 409
    closed = bench["op"].post(f"/handling-units/{unit['id']}/close", {"weight_kg": "9.250"})
    assert closed.status_code == 200, closed.content
    assert closed.json()["status"] == "closed" and closed.json()["cartons"] == 1

    event = OutboxEvent.objects.get(type="wms.handling_unit.closed")
    assert event.payload["sscc"] == unit["sscc"] and event.payload["order_fully_packed"] is True
    assert sorted(c["qty"] for c in event.payload["contents"]) == ["12.000", "4.000"]
    deliver()
    so.refresh_from_db()
    assert so.status == "packed"
    assert OutboxEvent.objects.filter(type="oms.sales_order.packed").count() == 1
    assert bench["op"].get("/pack-orders", site_id=str(bench["teak"].pk)).json() == []

    pdf = bench["op"].client.get(f"/api/v1/handling-units/{unit['id']}/label")
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert b"Sandton Store" in pdf.content and b"ORD-" in pdf.content and b"PO-77" in pdf.content
    zpl = bench["op"].client.get(f"/api/v1/handling-units/{unit['id']}/label?format=zpl")
    assert zpl.status_code == 200 and f">;>800{unit['sscc']}".encode() in zpl.content

    found = bench["op"].get("/handling-units", sscc=f"(00) {unit['sscc']}").json()["items"]
    assert [u["id"] for u in found] == [unit["id"]]
    found = bench["op"].get("/handling-units", sscc=f"00{unit['sscc']}").json()["items"]
    assert [u["id"] for u in found] == [unit["id"]]


def test_closed_units_are_immutable(bench):
    put(bench, "A-01", bench["water"], 10)
    so, _ = picked_order(bench, ("WATER", 4))
    unit = open_unit(bench, so)
    bench["op"].post(f"/handling-units/{unit['id']}/contents", {"item_code": "WATER", "qty": "2"})
    bench["op"].post(f"/handling-units/{unit['id']}/close", {"weight_kg": "3"})
    again = bench["op"].post(f"/handling-units/{unit['id']}/contents", {"item_code": "WATER", "qty": "1"})
    assert again.status_code == 409
    content = HandlingUnitContent.objects.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        HandlingUnitContent.objects.filter(pk=content.pk).update(qty=D(1))
    with pytest.raises(IntegrityError), transaction.atomic():
        HandlingUnit.objects.filter(pk=unit["id"]).update(weight_kg=D(1))
    with pytest.raises(IntegrityError), transaction.atomic():
        HandlingUnitContent.objects.create(
            handling_unit_id=unit["id"], order_line_id=content.order_line_id, item=bench["water"], lot="X", qty=D(1)
        )


def test_cannot_pack_more_than_was_picked(bench):
    put(bench, "A-01", bench["water"], 10)
    put(bench, "A-02", bench["juice"], 10, lot="J1", days=90)
    so, _ = picked_order(bench, ("WATER", 4))
    unit = open_unit(bench, so)
    url = f"/handling-units/{unit['id']}/contents"
    too_many = bench["op"].post(url, {"item_code": "WATER", "qty": "5"})
    assert too_many.status_code == 422 and too_many.json()["title"] == "Too many"
    not_picked = bench["op"].post(url, {"item_code": "JUICE", "qty": "1"})
    assert not_picked.status_code == 422 and not_picked.json()["title"] == "Nothing to pack"
    assert bench["op"].post(url, {"item_code": "WATER", "qty": "3"}).status_code == 201
    # A second unit for the same order only gets what's left.
    second = open_unit(bench, so)
    over = bench["op"].post(f"/handling-units/{second['id']}/contents", {"item_code": "WATER", "qty": "2"})
    assert over.status_code == 422 and "Only 1.000 left" in over.json()["detail"]


def test_two_lots_need_a_lot_scan(bench):
    put(bench, "A-01", bench["juice"], 3, lot="J1", days=30)
    put(bench, "A-02", bench["juice"], 10, lot="J2", days=60)
    so, _ = picked_order(bench, ("JUICE", 5))
    unit = open_unit(bench, so)
    url = f"/handling-units/{unit['id']}/contents"
    response = bench["op"].post(url, {"item_code": "JUICE", "qty": "5"})
    assert response.status_code == 422 and response.json()["title"] == "Lot required"
    assert bench["op"].post(url, {"item_code": "JUICE", "qty": "3", "lot": "J1"}).status_code == 201
    assert bench["op"].post(url, {"item_code": "JUICE", "qty": "2"}).status_code == 201  # only J2 left now
    lots = sorted((c.lot, c.qty) for c in HandlingUnitContent.objects.all())
    assert lots == [("J1", D(3)), ("J2", D(2))]


def test_order_is_packed_only_when_picking_is_done_and_everything_is_closed(bench):
    put(bench, "A-01", bench["water"], 10)
    put(bench, "A-03", bench["juice"], 10, lot="J1", days=90)
    so, tasks = picked_order(bench, ("WATER", 4), ("JUICE", 2), pick=False)
    water_task = next(t for t in tasks if t["sku"] == "WATER")
    body = {"location_code": "A-01", "item_code": "WATER", "qty": water_task["qty"]}
    bench["op"].post(f"/pick-tasks/{water_task['id']}/confirm", body)

    first = open_unit(bench, so)
    bench["op"].post(f"/handling-units/{first['id']}/contents", {"item_code": "WATER", "qty": "4"})
    bench["op"].post(f"/handling-units/{first['id']}/close", {"weight_kg": "4"})
    assert OutboxEvent.objects.get(type="wms.handling_unit.closed").payload["order_fully_packed"] is False

    juice_task = next(t for t in tasks if t["sku"] == "JUICE")
    body = {"location_code": "A-03", "item_code": "JUICE", "qty": juice_task["qty"]}
    bench["op"].post(f"/pick-tasks/{juice_task['id']}/confirm", body)
    pallet = open_unit(bench, so, kind="pallet")
    bench["op"].post(f"/handling-units/{pallet['id']}/contents", {"item_code": "JUICE", "qty": "2"})
    no_cartons = bench["op"].post(f"/handling-units/{pallet['id']}/close", {"weight_kg": "30"})
    assert no_cartons.status_code == 422 and no_cartons.json()["title"] == "Cartons required"
    assert (
        bench["op"].post(f"/handling-units/{pallet['id']}/close", {"weight_kg": "30", "cartons": 2}).status_code == 200
    )
    deliver()
    so.refresh_from_db()
    assert so.status == "packed"


def test_short_picked_order_packs_what_was_found(bench):
    put(bench, "A-01", bench["water"], 5)
    so, tasks = picked_order(bench, ("WATER", 5), pick=False)
    body = {"location_code": "A-01", "qty_found": "3", "reason": "Two damaged"}
    assert bench["op"].post(f"/pick-tasks/{tasks[0]['id']}/short", body).status_code == 200
    unit = open_unit(bench, so)
    assert (
        bench["op"].post(f"/handling-units/{unit['id']}/contents", {"item_code": "WATER", "qty": "3"}).status_code
        == 201
    )
    bench["op"].post(f"/handling-units/{unit['id']}/close", {"weight_kg": "3"})
    deliver()
    so.refresh_from_db()
    assert so.status == "packed"


def test_packing_needs_a_waved_order_and_the_right_roles(bench, bearer_api):
    put(bench, "A-01", bench["water"], 10)
    so = oms.create_sales_order(
        client=bench["sdb"],
        site=bench["teak"],
        ship_to=bench["store"],
        client_ref="",
        lines=[oms.OrderLineIn(sku="WATER", qty=D(2))],
    )
    deliver()
    response = bench["op"].post("/handling-units", {"sales_order_id": str(so.pk), "kind": "carton"})
    assert response.status_code == 409 and response.json()["title"] == "Not picked"
    client_api = bearer_api(bench["sdb"])
    assert client_api.post("/handling-units", {"sales_order_id": str(so.pk), "kind": "carton"}).status_code == 403
