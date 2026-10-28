"""M3 trips: shipments from orders and packed units, stops, own or carrier assignment, load scan, dispatch."""

import itertools
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.masterdata import services as masterdata
from apps.masterdata.models import Item, PartyRoleType, Person, Site, Vehicle
from apps.oms import services as oms
from apps.platform.models import OutboxEvent, Role
from apps.tms.models import Shipment, ShipmentUnit, Stop, StopShipment, Trip
from apps.wms import ledger
from apps.wms.models import Location

from .conftest import api_for, make_user
from .test_packing import deliver

pytestmark = pytest.mark.django_db

D = Decimal
_refs = itertools.count(1)


@pytest.fixture
def yard(make_client, settings):
    settings.GS1_COMPANY_PREFIX = "6009876"
    sdb = make_client("SDB", "4111111111")
    other = make_client("OTH", "4222222222")
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    north = Site.objects.create(code="NORTH", name="Northgate", kind="warehouse")
    stores = {
        code: Site.objects.create(code=code, name=name, kind="delivery_address", party=sdb, address="1 Rd")
        for code, name in [("SDB-SAND", "Sandton Store"), ("SDB-ROSE", "Rosebank Store")]
    }
    oth_store = Site.objects.create(code="OTH-1", name="Other Store", kind="delivery_address", party=other)
    water = Item.objects.create(client=sdb, sku="WATER", description="Water")
    soap = Item.objects.create(client=other, sku="SOAP", description="Soap")
    bins = {
        code: Location.objects.create(site=teak, code=code, kind="storage", pick_sequence=seq, pallet_capacity=50)
        for code, seq in [("A-01", 10), ("A-02", 20)]
    }
    truck = Vehicle.objects.create(
        registration="ND 123-456", roadworthy_expiry=timezone.localdate() + timedelta(days=90)
    )
    spare = Vehicle.objects.create(registration="ND 999-000")
    driver = Person.objects.create(first_name="Sipho", last_name="Dlamini", is_driver=True)
    relief = Person.objects.create(first_name="Thandi", last_name="Nkosi", is_driver=True)
    carrier = masterdata.create_party(code="FASTCO", legal_name="Fast Co", roles=[PartyRoleType.CARRIER])
    every = {"site_ids": [teak.pk], "all_clients": True}
    return {
        "sdb": sdb,
        "other": other,
        "teak": teak,
        "north": north,
        "stores": stores,
        "oth_store": oth_store,
        "water": water,
        "soap": soap,
        "bins": bins,
        "truck": truck,
        "spare": spare,
        "driver": driver,
        "relief": relief,
        "carrier": carrier,
        "disp": api_for(make_user("disp@example.com", Role.DISPATCHER, **every)),
        "sup": api_for(make_user("sup@example.com", Role.WAREHOUSE_SUPERVISOR, **every)),
        "op": api_for(make_user("op@example.com", Role.WAREHOUSE_OPERATOR, **every)),
    }


def put(yard, code, qty, item=None):
    item = item or yard["water"]
    key = ledger.StockKey(item.client_id, item.pk, yard["bins"][code].pk, "", None, "available")
    with transaction.atomic():
        ledger.post(key, D(qty), kind="opening")


def order(yard, store="SDB-SAND", qty=6, client="sdb", sku="WATER"):
    ship_to = yard["oth_store"] if client == "other" else yard["stores"][store]
    so = oms.create_sales_order(
        client=yard[client],
        site=yard["teak"],
        ship_to=ship_to,
        client_ref=f"PO-{next(_refs)}",
        lines=[oms.OrderLineIn(sku=sku, qty=D(qty))],
    )
    deliver()
    return so


def pick_and_pack(yard, so, units=1):
    """Wave, pick everything, and pack it into `units` cartons split as evenly as the quantity allows."""
    wave = yard["sup"].post("/waves", {"site_id": str(yard["teak"].pk), "sales_order_ids": [str(so.pk)]}).json()
    assert yard["sup"].post(f"/waves/{wave['id']}/release").status_code == 200
    for t in yard["op"].get("/pick-tasks", wave_id=wave["id"]).json()["items"]:
        body = {"location_code": t["from_location"], "item_code": t["sku"], "qty": t["qty"]}
        assert yard["op"].post(f"/pick-tasks/{t['id']}/confirm", body).status_code == 200
    line = so.lines.get()
    sizes = [line.base_qty // units] * units
    sizes[-1] += line.base_qty - sum(sizes)
    ssccs = []
    for size in sizes:
        unit = yard["op"].post("/handling-units", {"sales_order_id": str(so.pk), "kind": "carton"}).json()
        added = yard["op"].post(
            f"/handling-units/{unit['id']}/contents", {"item_code": line.item.sku, "qty": str(size)}
        )
        assert added.status_code == 201, added.content
        closed = yard["op"].post(f"/handling-units/{unit['id']}/close", {"weight_kg": "4.5"})
        assert closed.status_code == 200, closed.content
        ssccs.append(unit["sscc"])
    deliver()
    return ssccs


def shipment_of(so) -> Shipment:
    return Shipment.objects.get(sales_order_id=so.pk)


def new_trip(yard, api=None, site=None):
    start = (timezone.now() + timedelta(hours=2)).isoformat()
    response = (api or yard["disp"]).post("/trips", {"site_id": str((site or yard["teak"]).pk), "planned_start": start})
    assert response.status_code == 201, response.content
    return response.json()


def stops(yard, trip, *drops, api=None):
    body = {
        "stops": [
            {"site_id": str(site.pk), "shipment_ids": [str(shipment_of(so).pk) for so in orders]}
            for site, orders in drops
        ]
    }
    return (api or yard["disp"]).client.put(
        f"/api/v1/trips/{trip['id']}/stops",
        body,
        content_type="application/json",
        headers={"Idempotency-Key": f"stops-{next(_refs)}"},
    )


def own(yard, trip, truck=None, driver=None):
    body = {"vehicle_id": str((truck or yard["truck"]).pk), "driver_id": str((driver or yard["driver"]).pk)}
    return yard["disp"].post(f"/trips/{trip['id']}/assign", body)


# --- Shipments follow orders and packing ---------------------------------------------------------------------------


def test_order_becomes_a_shipment_that_is_ready_once_packed(yard):
    put(yard, "A-01", 20)
    so = order(yard, qty=6)
    shipment = shipment_of(so)
    assert shipment.status == "draft" and shipment.number.startswith("SHP-")
    assert shipment.ship_to == yard["stores"]["SDB-SAND"] and shipment.order_number == so.number
    assert [(line.qty_ordered, line.qty_packed) for line in shipment.lines.all()] == [(D(6), D(0))]

    ssccs = pick_and_pack(yard, so, units=2)
    shipment.refresh_from_db()
    assert shipment.status == "ready" and shipment.packed_at is not None
    assert shipment.weight_kg == D(9) and shipment.lines.get().qty_packed == D(6)
    assert sorted(shipment.units.values_list("sscc", flat=True)) == sorted(ssccs)

    view = yard["disp"].get(f"/shipments/{shipment.pk}").json()
    assert view["status"] == "ready" and view["ship_to_name"] == "Sandton Store" and len(view["units"]) == 2
    board = yard["disp"].get("/shipments", site_id=str(yard["teak"].pk), status="ready").json()["items"]
    assert [s["number"] for s in board] == [shipment.number]


def test_shipment_events_are_idempotent(yard):
    from apps.tms import shipments

    put(yard, "A-01", 20)
    so = order(yard, qty=4)
    pick_and_pack(yard, so)
    event = OutboxEvent.objects.get(type="wms.handling_unit.closed")
    shipments.attach_unit(event.payload)  # a redelivery
    accepted = OutboxEvent.objects.get(type="oms.sales_order.accepted")
    shipments.create_from_order(client_id=accepted.client_id, site_id=accepted.site_id, payload=accepted.payload)
    shipment = shipment_of(so)
    assert Shipment.objects.count() == 1 and shipment.units.count() == 1
    assert shipment.lines.get().qty_packed == D(4) and shipment.weight_kg == D("4.5")


# --- The happy path ------------------------------------------------------------------------------------------------


def test_plan_assign_load_and_dispatch_a_two_stop_trip(yard):
    put(yard, "A-01", 50)
    sand, rose = order(yard, "SDB-SAND", 6), order(yard, "SDB-ROSE", 4)
    sand_ssccs, rose_ssccs = pick_and_pack(yard, sand, units=2), pick_and_pack(yard, rose)
    trip = new_trip(yard)
    assert trip["status"] == "planning" and trip["number"].startswith("TRP-") and trip["stops"] == []

    planned = stops(yard, trip, (yard["stores"]["SDB-SAND"], [sand]), (yard["stores"]["SDB-ROSE"], [rose]))
    assert planned.status_code == 200, planned.content
    body = planned.json()
    assert [(s["seq"], s["site_code"]) for s in body["stops"]] == [(1, "TEAK"), (2, "SDB-SAND"), (3, "SDB-ROSE")]
    assert sorted(x["action"] for x in body["stops"][0]["shipments"]) == ["pickup", "pickup"]
    assert body["units_total"] == 3 and body["units_loaded"] == 0
    assert set(Shipment.objects.values_list("status", flat=True)) == {"planned"}

    assigned = own(yard, trip)
    assert assigned.status_code == 200, assigned.content
    assert assigned.json()["vehicle_registration"] == "ND 123-456" and assigned.json()["driver_name"] == "Sipho Dlamini"

    not_yet = yard["disp"].post(f"/trips/{trip['id']}/dispatch")
    assert not_yet.status_code == 409 and not_yet.json()["title"] == "Not loaded"

    first = yard["op"].post(f"/trips/{trip['id']}/load", {"sscc": f"(00) {sand_ssccs[0]}"})
    assert first.status_code == 200, first.content
    assert first.json()["units_loaded"] == 1
    again = yard["op"].post(f"/trips/{trip['id']}/load", {"sscc": f"00{sand_ssccs[0]}"})
    assert again.status_code == 200 and again.json()["units_loaded"] == 1  # a repeated scan
    for code in [sand_ssccs[1], *rose_ssccs]:
        assert yard["op"].post(f"/trips/{trip['id']}/load", {"sscc": code}).status_code == 200

    assert yard["op"].post(f"/trips/{trip['id']}/dispatch").status_code == 403  # loaders don't dispatch
    dispatched = yard["disp"].post(f"/trips/{trip['id']}/dispatch")
    assert dispatched.status_code == 200, dispatched.content
    assert dispatched.json()["status"] == "dispatched" and dispatched.json()["units_loaded"] == 3
    assert set(Shipment.objects.values_list("status", flat=True)) == {"in_transit"}

    event = OutboxEvent.objects.get(type="tms.trip.dispatched")
    assert event.payload["number"] == trip["number"] and event.payload["vehicle_id"] == str(yard["truck"].pk)
    assert [len(s["shipment_ids"]) for s in event.payload["stops"]] == [0, 1, 1]
    deliver()
    sand.refresh_from_db()
    rose.refresh_from_db()
    assert sand.status == rose.status == "dispatched"
    told = OutboxEvent.objects.filter(type="oms.sales_order.dispatched").order_by("occurred_at")
    assert [(e.client_id, e.payload["trip_number"]) for e in told] == [(yard["sdb"].pk, trip["number"])] * 2

    frozen = stops(yard, trip, (yard["stores"]["SDB-SAND"], [sand]))
    assert frozen.status_code == 409
    assert yard["op"].delete(f"/trips/{trip['id']}/load/{sand_ssccs[0]}").status_code == 409


def test_carrier_trip(yard):
    put(yard, "A-01", 20)
    so = order(yard)
    (code,) = pick_and_pack(yard, so)
    trip = new_trip(yard)
    stops(yard, trip, (yard["stores"]["SDB-SAND"], [so]))
    assert own(yard, trip).status_code == 200
    body = {"carrier_id": str(yard["carrier"].pk), "agreed_cost": "1850.00"}
    switched = yard["disp"].post(f"/trips/{trip['id']}/assign", body)
    assert switched.status_code == 200, switched.content
    out = switched.json()
    assert out["carrier_code"] == "FASTCO" and out["vehicle_id"] is None and D(out["agreed_cost"]) == D("1850.00")
    yard["op"].post(f"/trips/{trip['id']}/load", {"sscc": code})
    assert yard["disp"].post(f"/trips/{trip['id']}/dispatch").status_code == 200
    assert OutboxEvent.objects.get(type="tms.trip.dispatched").payload["carrier_id"] == str(yard["carrier"].pk)


# --- Guards --------------------------------------------------------------------------------------------------------


def test_assignment_is_validated(yard):
    trip = new_trip(yard)
    url = f"/trips/{trip['id']}/assign"
    both = {"vehicle_id": str(yard["truck"].pk), "carrier_id": str(yard["carrier"].pk), "agreed_cost": "10"}
    assert yard["disp"].post(url, both).json()["title"] == "Choose one"
    assert yard["disp"].post(url, {"vehicle_id": str(yard["truck"].pk)}).json()["title"] == "Vehicle and driver"
    assert yard["disp"].post(url, {"carrier_id": str(yard["carrier"].pk)}).json()["title"] == "Carrier and cost"
    not_carrier = {"carrier_id": str(yard["sdb"].pk), "agreed_cost": "10"}
    assert yard["disp"].post(url, not_carrier).json()["title"] == "Unknown carrier"

    yard["truck"].licence_disc_expiry = timezone.localdate() - timedelta(days=1)
    yard["truck"].save()
    expired = own(yard, trip)
    assert expired.status_code == 422 and "licence disc" in expired.json()["detail"]

    yard["driver"].prdp_expiry = timezone.localdate() - timedelta(days=1)
    yard["driver"].save()
    assert "prdp" in own(yard, trip, truck=yard["spare"]).json()["detail"]

    hired = Vehicle.objects.create(registration="CA 1", carrier=yard["carrier"])
    assert own(yard, trip, truck=hired, driver=yard["relief"]).json()["title"] == "Not own fleet"
    not_driver = Person.objects.create(first_name="Office", last_name="Clerk")
    assert own(yard, trip, truck=yard["spare"], driver=not_driver).json()["title"] == "Unknown driver"
    assert own(yard, trip, truck=yard["spare"], driver=yard["relief"]).status_code == 200


def test_database_refuses_a_trip_with_two_kinds_of_resource(yard):
    trip = Trip.objects.get(pk=new_trip(yard)["id"])
    with pytest.raises(IntegrityError), transaction.atomic():
        Trip.objects.filter(pk=trip.pk).update(vehicle=yard["truck"], driver=yard["driver"], carrier=yard["carrier"])
    with pytest.raises(IntegrityError), transaction.atomic():
        Trip.objects.filter(pk=trip.pk).update(vehicle=yard["truck"])
    with pytest.raises(IntegrityError), transaction.atomic():
        Trip.objects.filter(pk=trip.pk).update(status="dispatched")


def test_stops_are_validated(yard):
    put(yard, "A-01", 20)
    so = order(yard)
    trip = new_trip(yard)
    wrong_stop = stops(yard, trip, (yard["stores"]["SDB-ROSE"], [so]))
    assert wrong_stop.status_code == 422 and wrong_stop.json()["title"] == "Wrong stop"
    elsewhere = new_trip(yard, site=yard["teak"])
    Trip.objects.filter(pk=elsewhere["id"]).update(site=yard["north"])
    admin = api_for(make_user("boss@example.com", Role.SYSTEM_ADMIN, all_sites=True, all_clients=True))
    wrong_site = stops(yard, elsewhere, (yard["stores"]["SDB-SAND"], [so]), api=admin)
    assert wrong_site.status_code == 422 and wrong_site.json()["title"] == "Wrong warehouse"
    twice = stops(yard, trip, (yard["stores"]["SDB-SAND"], [so, so]))
    assert twice.status_code == 422 and twice.json()["title"] == "Shipment on two stops"


def test_a_shipment_rides_on_one_trip_and_its_units_only_on_that_trip(yard):
    put(yard, "A-01", 20)
    so = order(yard)
    (code,) = pick_and_pack(yard, so)
    first, second = new_trip(yard), new_trip(yard)
    assert stops(yard, first, (yard["stores"]["SDB-SAND"], [so])).status_code == 200
    taken = stops(yard, second, (yard["stores"]["SDB-SAND"], [so]))
    assert taken.status_code == 409 and taken.json()["title"] == "Shipment not available"

    stray = yard["op"].post(f"/trips/{second['id']}/load", {"sscc": code})
    assert stray.status_code == 409 and stray.json()["title"] == "Not on this trip"
    assert yard["op"].post(f"/trips/{first['id']}/load", {"sscc": code}).status_code == 200
    unknown = yard["op"].post(f"/trips/{first['id']}/load", {"sscc": "006009876000000000"})
    assert unknown.status_code == 404

    # Taking the shipment off needs its units unloaded first; then it is free for the other trip.
    blocked = stops(yard, first)
    assert blocked.status_code == 409 and blocked.json()["title"] == "Unload first"
    unloaded = yard["op"].delete(f"/trips/{first['id']}/load/{code}")
    assert unloaded.status_code == 200 and unloaded.json()["units_loaded"] == 0
    cleared = stops(yard, first)
    assert cleared.status_code == 200 and cleared.json()["stops"] == []
    assert shipment_of(so).status == "ready"
    assert stops(yard, second, (yard["stores"]["SDB-SAND"], [so])).status_code == 200
    yard["op"].post(f"/trips/{second['id']}/load", {"sscc": code})
    loaded_elsewhere = yard["op"].post(f"/trips/{first['id']}/load", {"sscc": code})
    assert loaded_elsewhere.status_code == 409  # the first trip no longer carries it


def test_dispatch_needs_packed_shipments_and_a_free_resource(yard):
    put(yard, "A-01", 50)
    unpacked = order(yard, "SDB-SAND")
    trip = new_trip(yard)
    stops(yard, trip, (yard["stores"]["SDB-SAND"], [unpacked]))  # draft shipments can be planned early
    assert shipment_of(unpacked).status == "planned"
    nobody = yard["disp"].post(f"/trips/{trip['id']}/dispatch")
    assert nobody.status_code == 409 and nobody.json()["title"] == "No resource"
    own(yard, trip)
    early = yard["disp"].post(f"/trips/{trip['id']}/dispatch")
    assert early.status_code == 409 and early.json()["title"] == "Not packed"

    (code,) = pick_and_pack(yard, unpacked)
    assert shipment_of(unpacked).status == "planned"  # packing doesn't take it off the trip
    yard["op"].post(f"/trips/{trip['id']}/load", {"sscc": code})
    assert yard["disp"].post(f"/trips/{trip['id']}/dispatch").status_code == 200

    other = order(yard, "SDB-ROSE")
    (code2,) = pick_and_pack(yard, other)
    later = new_trip(yard)
    stops(yard, later, (yard["stores"]["SDB-ROSE"], [other]))
    own(yard, later, driver=yard["relief"])  # same truck, different driver
    yard["op"].post(f"/trips/{later['id']}/load", {"sscc": code2})
    busy = yard["disp"].post(f"/trips/{later['id']}/dispatch")
    assert busy.status_code == 409 and busy.json()["title"] == "Resource busy"

    empty = new_trip(yard)
    assert yard["disp"].post(f"/trips/{empty['id']}/dispatch").json()["title"] == "No stops"


def test_cancelled_order_leaves_the_trip_and_the_stops_close_up(yard):
    put(yard, "A-01", 50)
    sand, rose = order(yard, "SDB-SAND"), order(yard, "SDB-ROSE")
    trip = new_trip(yard)
    stops(yard, trip, (yard["stores"]["SDB-SAND"], [sand]), (yard["stores"]["SDB-ROSE"], [rose]))
    oms.cancel_sales_order(sand.pk, "Client changed their mind")
    deliver()
    assert shipment_of(sand).status == "cancelled"
    view = yard["disp"].get(f"/trips/{trip['id']}").json()
    assert [(s["seq"], s["site_code"]) for s in view["stops"]] == [(1, "TEAK"), (2, "SDB-ROSE")]
    assert [len(s["shipments"]) for s in view["stops"]] == [1, 1]

    oms.cancel_sales_order(rose.pk, "Also cancelled")
    deliver()
    assert yard["disp"].get(f"/trips/{trip['id']}").json()["stops"] == []


def test_cancelling_a_trip_frees_its_shipments(yard):
    put(yard, "A-01", 20)
    so = order(yard)
    (code,) = pick_and_pack(yard, so)
    trip = new_trip(yard)
    stops(yard, trip, (yard["stores"]["SDB-SAND"], [so]))
    yard["op"].post(f"/trips/{trip['id']}/load", {"sscc": code})
    blocked = yard["disp"].post(f"/trips/{trip['id']}/cancel")
    assert blocked.status_code == 409 and blocked.json()["title"] == "Unload first"
    yard["op"].delete(f"/trips/{trip['id']}/load/{code}")
    cancelled = yard["disp"].post(f"/trips/{trip['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert shipment_of(so).status == "ready"
    assert not StopShipment.objects.exists() and not Stop.objects.exists()
    assert ShipmentUnit.objects.get().loaded_trip is None


# --- Who sees what -------------------------------------------------------------------------------------------------


def test_scoping(yard):
    put(yard, "A-01", 20)
    put(yard, "A-02", 20, item=yard["soap"])
    mine, theirs = order(yard), order(yard, client="other", sku="SOAP")
    trip = new_trip(yard)
    stops(yard, trip, (yard["stores"]["SDB-SAND"], [mine]), (yard["oth_store"], [theirs]))

    client = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[yard["sdb"].pk]))
    listed = client.get("/shipments").json()["items"]
    assert [s["number"] for s in listed] == [shipment_of(mine).number]
    assert client.get(f"/shipments/{shipment_of(theirs).pk}").status_code == 404
    assert client.get("/trips").status_code == 403
    assert (
        client.post(
            "/trips", {"site_id": str(yard["teak"].pk), "planned_start": timezone.now().isoformat()}
        ).status_code
        == 403
    )

    sdb_only = api_for(
        make_user("disp2@example.com", Role.DISPATCHER, site_ids=[yard["teak"].pk], client_ids=[yard["sdb"].pk])
    )
    view = sdb_only.get(f"/trips/{trip['id']}").json()
    assert [len(s["shipments"]) for s in view["stops"]] == [1, 1, 0]  # the other client's shipment is hidden

    north = api_for(make_user("n@example.com", Role.DISPATCHER, site_ids=[yard["north"].pk], all_clients=True))
    assert north.get(f"/trips/{trip['id']}").status_code == 404
    assert north.get(f"/shipments/{shipment_of(mine).pk}").status_code == 404
    assert (
        north.post("/trips", {"site_id": str(yard["teak"].pk), "planned_start": timezone.now().isoformat()}).status_code
        == 404
    )
    assert (
        yard["op"]
        .post("/trips", {"site_id": str(yard["teak"].pk), "planned_start": timezone.now().isoformat()})
        .status_code
        == 403
    )

    day = timezone.localtime(Trip.objects.get(pk=trip["id"]).planned_start).date()
    board = yard["disp"].get("/trips", site_id=str(yard["teak"].pk), on=day.isoformat()).json()["items"]
    assert [t["id"] for t in board] == [trip["id"]]
    assert yard["disp"].get("/trips", on=(day + timedelta(days=1)).isoformat()).json()["items"] == []


def test_backfill_builds_shipments_from_past_events(yard):
    from django.core.management import call_command

    put(yard, "A-01", 30)
    packed, cancelled = order(yard), order(yard, "SDB-ROSE")
    (code,) = pick_and_pack(yard, packed)
    oms.cancel_sales_order(cancelled.pk, "No longer needed")
    deliver()
    Shipment.objects.all().delete()  # as if transport had been installed after these events
    call_command("backfill_shipments")
    call_command("backfill_shipments")  # and again: nothing changes
    rebuilt = shipment_of(packed)
    assert rebuilt.status == "ready" and list(rebuilt.units.values_list("sscc", flat=True)) == [code]
    assert rebuilt.lines.get().qty_packed == D(6)
    assert shipment_of(cancelled).status == "cancelled" and Shipment.objects.count() == 2
