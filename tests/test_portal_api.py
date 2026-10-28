"""The API behind the client portal's Home, Orders and Stock screens: order search, status counts and timeline,
stock summary, the sites and exception cases a client may see, and deliveries by day."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.platform import exceptions_queue
from apps.platform.models import Role

from .conftest import api_for, make_user
from .test_delivery import evidence, pod, road, upload  # noqa: F401
from .test_trips import new_trip, order, own, pick_and_pack, put, shipment_of, stops, yard  # noqa: F401

pytestmark = pytest.mark.django_db


def buyer_of(fixture):
    return api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[fixture["sdb"].pk]))


def test_orders_search_counts_timeline_and_cancel(road):  # noqa: F811
    buyer = buyer_of(road)
    _, sand_stop, _ = road["stop_ids"]
    signature = evidence(road, sand_stop)
    units = [{"sscc": code, "status": "delivered"} for code in road["sand_units"]]
    assert (
        pod(road, sand_stop, recipient_name="Lerato", signature_document_id=signature, units=units).status_code == 200
    )
    spare = order(road, "SDB-ROSE", 2)
    theirs = order(road, client="other", sku="SOAP")

    listed = buyer.get("/orders", newest_first="true").json()["items"]
    assert [o["number"] for o in listed] == [spare.number, road["rose"].number, road["sand"].number]
    assert listed[0]["ship_to_name"] == "Rosebank Store" and listed[0]["status"] == "allocated"
    assert [o["id"] for o in buyer.get("/orders", q=road["sand"].client_ref.lower()).json()["items"]] == [
        str(road["sand"].pk)
    ]
    assert len(buyer.get("/orders", q="PO-").json()["items"]) == 3
    assert buyer.get("/orders", q=theirs.number).json()["items"] == []

    detail = buyer.get(f"/orders/{spare.pk}").json()
    assert detail["can_cancel"] is True
    assert buyer.get(f"/orders/{road['sand'].pk}").json()["can_cancel"] is False
    cancelled = buyer.post(f"/orders/{spare.pk}/cancel", {"reason": "Ordered twice"})
    assert cancelled.status_code == 200 and cancelled.json()["can_cancel"] is False

    counts = buyer.get("/orders/status-counts").json()["counts"]
    assert counts["delivered"] == 1 and counts["dispatched"] == 1 and counts["cancelled"] == 1
    assert counts["accepted"] == 0 and set(counts) >= {"backordered", "picking", "packed"}

    sand_history = [e["status"] for e in buyer.get(f"/orders/{road['sand'].pk}/timeline").json()]
    assert sand_history == ["accepted", "allocated", "picking", "packed", "dispatched", "delivered"]
    spare_history = buyer.get(f"/orders/{spare.pk}/timeline").json()
    assert [e["status"] for e in spare_history] == ["accepted", "allocated", "cancelled"]
    assert spare_history[-1]["detail"] == "Ordered twice"
    assert buyer.get(f"/orders/{theirs.pk}/timeline").status_code == 404

    today = timezone.localdate()
    delivered = buyer.get("/shipments", delivered_on=today.isoformat()).json()["items"]
    assert [s["id"] for s in delivered] == [str(shipment_of(road["sand"]).pk)]
    assert buyer.get("/shipments", delivered_on=(today - timedelta(days=1)).isoformat()).json()["items"] == []


def test_order_counts_forget_old_finished_orders(yard):  # noqa: F811
    put(yard, "A-01", 10)
    old, kept = order(yard, qty=1), order(yard, qty=1)
    buyer = buyer_of(yard)
    for so in (old, kept):
        buyer.post(f"/orders/{so.pk}/cancel", {"reason": "Not needed"})
    type(old).objects.filter(pk=old.pk).update(updated_at=timezone.now() - timedelta(days=8))
    assert buyer.get("/orders/status-counts").json()["counts"]["cancelled"] == 1


def test_stock_summary_adds_up_bins_and_shows_allocations(yard):  # noqa: F811
    put(yard, "A-01", 10)
    put(yard, "A-02", 5)
    put(yard, "A-01", 7, item=yard["soap"])
    order(yard, qty=6)
    buyer = buyer_of(yard)

    page = buyer.get("/inventory/summary").json()
    assert page["truncated"] is False
    [row] = page["items"]
    assert (row["sku"], row["description"], row["site_code"], row["base_uom"]) == ("WATER", "Water", "TEAK", "EA")
    assert (row["qty_on_hand"], row["qty_allocated"], row["qty_available"]) == ("15.000", "6.000", "9.000")
    assert len(buyer.get("/inventory/summary", q="wat").json()["items"]) == 1
    assert buyer.get("/inventory/summary", q="soap").json()["items"] == []
    assert buyer.get("/inventory/summary", site_id=str(yard["north"].pk)).json()["items"] == []

    staff = yard["sup"].get("/inventory/summary").json()["items"]
    assert sorted(r["sku"] for r in staff) == ["SOAP", "WATER"]
    elsewhere = api_for(make_user("north@example.com", Role.WAREHOUSE_SUPERVISOR, site_ids=[yard["north"].pk]))
    assert elsewhere.get("/inventory/summary").json()["items"] == []


def test_clients_see_warehouses_and_their_own_addresses(yard):  # noqa: F811
    yard["north"].is_active = False
    yard["north"].save()
    buyer = buyer_of(yard)
    assert sorted(s["code"] for s in buyer.get("/sites").json()["items"]) == ["SDB-ROSE", "SDB-SAND", "TEAK"]
    assert [s["code"] for s in buyer.get("/sites", kind="warehouse").json()["items"]] == ["TEAK"]
    # Staff scoping is unchanged.
    assert [s["code"] for s in yard["sup"].get("/sites").json()["items"]] == ["TEAK"]


def test_clients_see_their_own_order_and_delivery_cases_only(yard):  # noqa: F811
    put(yard, "A-01", 2)
    short = order(yard, qty=6)  # backordered: opens an allocation.short case for SDB
    exceptions_queue.raise_case(kind="inventory.short_pick", summary="Count A-01", client_id=yard["sdb"].pk)
    exceptions_queue.raise_case(kind="delivery.failed", summary="Theirs", client_id=yard["other"].pk)
    done = exceptions_queue.raise_case(kind="receipt.variance", summary="Old", client_id=yard["sdb"].pk)
    exceptions_queue.resolve(done, "Checked with the client")
    buyer = buyer_of(yard)

    cases = buyer.get("/client/exceptions").json()["items"]
    assert [(c["kind"], c["entity_id"]) for c in cases] == [("allocation.short", str(short.pk))]
    assert "detail" not in cases[0] and "assigned_to_id" not in cases[0]
    everything = buyer.get("/client/exceptions", open_only="false").json()["items"]
    assert [c["kind"] for c in everything] == ["receipt.variance", "allocation.short"]
    assert yard["sup"].get("/client/exceptions").status_code == 403
