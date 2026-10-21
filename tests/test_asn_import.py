"""M1: ASNs uploaded as CSV, all or nothing."""

import pytest

from apps.masterdata.models import Item, ItemBarcode, ItemUom, Site
from apps.oms.models import InboundNotice
from apps.platform.models import Document, OutboxEvent, Role

from .conftest import api_for, make_user

pytestmark = pytest.mark.django_db

HEADER = "client_ref,site_code,expected_at,sku,barcode,qty,uom,lot,expiry\n"


@pytest.fixture
def setup(make_client):
    sdb = make_client("SDB", "4111111111")
    other = make_client("OTHER", "4222222222")
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    water = Item.objects.create(client=sdb, sku="SDB-001", description="Water 500ml")
    ItemUom.objects.create(item=water, uom="CS", factor=24)
    ItemBarcode.objects.create(item=water, client=sdb, barcode="6001234000017", uom="EA")
    Item.objects.create(client=sdb, sku="SDB-002", description="Juice 1l", lot_tracked=True)
    Item.objects.create(client=other, sku="OTH-001", description="Other client's item")
    return {"sdb": sdb, "other": other, "teak": teak}


def test_rows_sharing_a_reference_become_one_asn(setup, bearer_api):
    csv_text = HEADER + (
        "PO-1,teak,2026-11-02T08:00:00+02:00,SDB-001,,10,CS,,\n"
        "PO-1,TEAK,2026-11-02T08:00:00+02:00,,6001234000017,6,,,\n"
        "PO-2,TEAK,2026-11-03T08:00:00+02:00,SDB-002,,5,,L-77,2027-01-31\n"
    )
    response = bearer_api(setup["sdb"]).upload("/inbound-notices:import", csv_text, name="asns.csv")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["created"] == 2 and [n["client_ref"] for n in body["notices"]] == ["PO-1", "PO-2"]
    po1 = InboundNotice.objects.get(client_ref="PO-1")
    assert [(line.qty, line.uom) for line in po1.lines.order_by("line_no")] == [(10, "CS"), (6, "EA")]
    assert OutboxEvent.objects.filter(type="oms.inbound_notice.accepted").count() == 2
    assert Document.objects.get(pk=body["document_id"]).client_id == setup["sdb"].pk


def test_one_bad_row_rejects_the_whole_file(setup, bearer_api):
    csv_text = HEADER + (
        "PO-1,TEAK,2026-11-02T08:00:00+02:00,SDB-001,,10,CS,,\n"
        "PO-2,TEAK,2026-11-02T08:00:00+02:00,OTH-001,,1,,,\n"  # another client's item
        "PO-3,TEAK,2026-11-02T08:00:00+02:00,SDB-001,,1,,,\n"
        "PO-3,TEAK,2026-11-04T08:00:00+02:00,SDB-001,,1,,,\n"  # same ASN, different date
    )
    response = bearer_api(setup["sdb"]).upload("/inbound-notices:import", csv_text)
    assert response.status_code == 422
    assert [e["row"] for e in response.json()["errors"]] == [3, 4]
    assert not InboundNotice.objects.exists()
    assert not OutboxEvent.objects.filter(type__startswith="oms.").exists()

    bad_qty = HEADER + "PO-9,TEAK,2026-11-02T08:00:00+02:00,SDB-001,,0,,,\n"
    response = bearer_api(setup["sdb"]).upload("/inbound-notices:import", bad_qty)
    assert response.status_code == 422 and response.json()["errors"][0]["field"] == "qty"


def test_staff_upload_for_a_named_client(setup):
    sup = make_user("sup@example.com", Role.WAREHOUSE_SUPERVISOR, site_ids=[setup["teak"].pk], all_clients=True)
    csv_text = HEADER + "PO-1,TEAK,2026-11-02T08:00:00+02:00,SDB-001,,10,CS,,\n"
    api = api_for(sup)
    assert api.upload("/inbound-notices:import", csv_text).status_code == 422  # which client?
    response = api.upload(f"/inbound-notices:import?client_id={setup['sdb'].pk}", csv_text)
    assert response.status_code == 201, response.content
    operator = make_user("op@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[setup["teak"].pk], all_clients=True)
    assert api_for(operator).upload(f"/inbound-notices:import?client_id={setup['sdb'].pk}", csv_text).status_code == 403
