"""M2 outbound: sales orders (create, inline delivery addresses, CSV import, cancel)."""

import pytest
from django.db import connection

from apps.masterdata.models import Item, ItemUom, Site
from apps.oms.models import SalesOrder
from apps.platform.models import OutboxEvent, Role

from .conftest import api_for, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(make_client):
    sdb = make_client("SDB", "4111111111")
    other = make_client("OTHER", "4222222222")
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    Site.objects.create(code="DBN", name="Durban", kind="warehouse")
    store = Site.objects.create(
        code="SDB-STORE", name="SDB store", kind="delivery_address", party=sdb, address="1 Main Rd"
    )
    Site.objects.create(code="OTH-STORE", name="Other store", kind="delivery_address", party=other, address="2 Side Rd")
    water = Item.objects.create(client=sdb, sku="SDB-001", description="Water 500ml")
    ItemUom.objects.create(item=water, uom="CS", factor=24)
    Item.objects.create(client=other, sku="OTH-001", description="Other client's item")
    supervisor = make_user("sup@example.com", Role.WAREHOUSE_SUPERVISOR, site_ids=[teak.pk], all_clients=True)
    return {"sdb": sdb, "other": other, "teak": teak, "store": store, "water": water, "supervisor": supervisor}


def _order(api, **overrides):
    body = {
        "site_code": "TEAK",
        "client_ref": "SO-1",
        "ship_to_code": "SDB-STORE",
        "required_by": "2026-11-05",
        "lines": [{"sku": "SDB-001", "qty": "2", "uom": "cs"}, {"sku": "SDB-001", "qty": "5"}],
    }
    body.update(overrides)
    return api.post("/orders", body)


def test_client_system_places_an_order(world, bearer_api):
    response = _order(bearer_api(world["sdb"]), contact_name="Thandi", contact_phone="+27 82 000 0000")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["number"].startswith("ORD-") and body["status"] == "accepted"
    assert body["ship_to"]["code"] == "SDB-STORE"
    assert [(line["qty"], line["uom"], line["base_qty"]) for line in body["lines"]] == [
        ("2.000", "CS", "48.000"),
        ("5.000", "EA", "5.000"),
    ]
    event = OutboxEvent.objects.get(type="oms.sales_order.accepted")
    assert event.client_id == world["sdb"].pk and event.site_id == world["teak"].pk
    assert [line["base_qty"] for line in event.payload["lines"]] == ["48.000", "5.000"]

    # The phone number is personal information: encrypted at rest, readable through the API.
    with connection.cursor() as cursor:
        cursor.execute("SELECT contact_phone FROM oms_salesorder WHERE id = %s", [body["id"]])
        assert "+27" not in cursor.fetchone()[0]
    assert body["contact_phone"] == "+27 82 000 0000"


def test_inline_delivery_address_is_created_once_and_reused(world, bearer_api):
    api = bearer_api(world["sdb"])
    ship_to = {"name": "Corner  Cafe", "address": "5 Long St, Cape Town"}
    first = _order(api, client_ref="SO-1", ship_to_code="", ship_to=ship_to)
    second = _order(api, client_ref="SO-2", ship_to_code="", ship_to={**ship_to, "name": "corner cafe"})
    assert first.status_code == second.status_code == 201, second.content
    assert first.json()["ship_to"]["code"].startswith("DA-")
    assert first.json()["ship_to"]["id"] == second.json()["ship_to"]["id"]
    assert Site.objects.get(pk=first.json()["ship_to"]["id"]).party_id == world["sdb"].pk


def test_order_validation(world, bearer_api):
    api = bearer_api(world["sdb"])
    bad_lines = _order(api, lines=[{"sku": "SDB-001", "qty": "1", "uom": "PL"}, {"sku": "OTH-001", "qty": "1"}])
    assert bad_lines.status_code == 422
    assert [e["line"] for e in bad_lines.json()["errors"]] == [1, 2]
    assert _order(api, ship_to_code="OTH-STORE").status_code == 422  # another client's address
    assert _order(api, ship_to_code="").status_code == 422  # no address at all
    assert _order(api, site_code="SDB-STORE").status_code == 422  # not a warehouse
    assert _order(api).status_code == 201
    duplicate = _order(api)
    assert duplicate.status_code == 409 and duplicate.json()["title"] == "Duplicate reference"
    assert SalesOrder.objects.count() == 1


def test_idempotent_replay_returns_the_same_order(world, bearer_api):
    api = bearer_api(world["sdb"])
    headers = {"Idempotency-Key": "order-retry-1"}
    first = api.post("/orders", _body(), headers=headers)
    again = api.post("/orders", _body(), headers=headers)
    assert first.status_code == again.status_code == 201
    assert again.headers["Idempotent-Replayed"] == "true"
    assert first.json()["id"] == again.json()["id"]
    assert SalesOrder.objects.count() == 1


def _body(**overrides):
    body = {
        "site_code": "TEAK",
        "client_ref": "SO-R",
        "ship_to_code": "SDB-STORE",
        "lines": [{"sku": "SDB-001", "qty": "1"}],
    }
    body.update(overrides)
    return body


def test_scoping_and_roles(world, bearer_api):
    sdb_api = bearer_api(world["sdb"])
    order = _order(sdb_api).json()
    assert bearer_api(world["other"]).get(f"/orders/{order['id']}").status_code == 404
    assert [o["id"] for o in sdb_api.get("/orders").json()["items"]] == [order["id"]]

    supervisor = api_for(world["supervisor"])
    assert _order(supervisor, client_ref="SO-2").status_code == 422  # staff must name the client
    staff_order = _order(supervisor, client_ref="SO-2", client_id=str(world["sdb"].pk))
    assert staff_order.status_code == 201, staff_order.content
    operator = make_user("op@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[world["teak"].pk], all_clients=True)
    assert _order(api_for(operator), client_ref="SO-3", client_id=str(world["sdb"].pk)).status_code == 403


def test_cancel_until_picking_starts(world, bearer_api):
    api = bearer_api(world["sdb"])
    order = _order(api).json()
    assert api.post(f"/orders/{order['id']}/cancel", {"reason": " "}).status_code == 422
    cancelled = api.post(f"/orders/{order['id']}/cancel", {"reason": "Customer changed mind"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert {line["status"] for line in cancelled.json()["lines"]} == {"cancelled"}
    assert OutboxEvent.objects.filter(type="oms.sales_order.cancelled").count() == 1
    assert api.post(f"/orders/{order['id']}/cancel", {"reason": "again"}).status_code == 200  # no-op
    assert OutboxEvent.objects.filter(type="oms.sales_order.cancelled").count() == 1

    picking = _order(api, client_ref="SO-2").json()
    SalesOrder.objects.filter(pk=picking["id"]).update(status="picking")
    refused = api.post(f"/orders/{picking['id']}/cancel", {"reason": "too late"})
    assert refused.status_code == 409 and refused.json()["title"] == "Picking has started"


CSV_HEADER = "client_ref,site_code,ship_to_code,ship_to_name,ship_to_address,required_by,sku,qty,uom\n"


def test_csv_import_groups_rows_into_orders(world, bearer_api):
    api = bearer_api(world["sdb"])
    csv_text = CSV_HEADER + (
        "SO-10,TEAK,SDB-STORE,,,2026-11-05,SDB-001,2,CS\n"
        "SO-10,TEAK,SDB-STORE,,,2026-11-05,SDB-001,3,EA\n"
        'SO-11,TEAK,,Spaza 7,"7 Vilakazi St, Soweto",,SDB-001,1,CS\n'
    )
    response = api.upload("/orders:import", csv_text)
    assert response.status_code == 201, response.content
    assert [o["client_ref"] for o in response.json()["orders"]] == ["SO-10", "SO-11"]
    assert SalesOrder.objects.get(client_ref="SO-10").lines.count() == 2
    assert SalesOrder.objects.get(client_ref="SO-11").ship_to.code.startswith("DA-")


def test_csv_import_is_all_or_nothing(world, bearer_api):
    api = bearer_api(world["sdb"])
    csv_text = CSV_HEADER + (
        "SO-20,TEAK,SDB-STORE,,,2026-11-05,SDB-001,2,CS\n"
        "SO-21,TEAK,SDB-STORE,,,2026-11-05,SDB-001,1,CS\n"
        "SO-21,TEAK,SDB-STORE,,,2026-11-06,SDB-001,1,CS\n"  # disagrees on required_by
        "SO-22,TEAK,SDB-STORE,,,,NOPE,1,EA\n"
    )
    response = api.upload("/orders:import", csv_text)
    assert response.status_code == 422
    assert [e["row"] for e in response.json()["errors"]] == [3, 5]
    assert "NOPE" in response.json()["errors"][1]["message"]
    assert not SalesOrder.objects.exists()
