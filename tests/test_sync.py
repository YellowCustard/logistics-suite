"""M1 offline scanner: session sign-in, /sync/push (ordered, exactly-once commands) and /sync/pull."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.db import transaction
from django.test import Client

from apps.masterdata.models import Item, ItemBarcode, ItemUom, Site
from apps.platform.models import Role, SyncCommand
from apps.wms import ledger
from apps.wms.models import InventoryBalance, InventoryMovement, Location, PutawayTask, ReceiptLine

from .conftest import api_for, make_user

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def floor(make_client, bearer_api):
    sdb = make_client("SDB", "4111111111")
    other = make_client("OTHER", "4222222222")
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    dbn = Site.objects.create(code="DBN", name="Durban", kind="warehouse")
    water = Item.objects.create(client=sdb, sku="SDB-001", description="Water 500ml")
    ItemUom.objects.create(item=water, uom="CS", factor=24)
    ItemBarcode.objects.create(item=water, client=sdb, barcode="6001234000017", uom="EA")
    Item.objects.create(client=other, sku="OTH-001", description="Other client's item")
    bins = {
        code: Location.objects.create(site=teak, code=code, kind=kind, pick_sequence=n, pallet_capacity=2)
        for n, (code, kind) in enumerate([("DOCK-1", "dock"), ("A-01", "storage"), ("A-02", "storage")])
    }
    Location.objects.create(site=dbn, code="A-01", kind="storage")
    with transaction.atomic():
        ledger.post(ledger.StockKey(sdb.pk, water.pk, bins["A-01"].pk, "", None, "available"), D(48), kind="opening")
    notice = (
        bearer_api(sdb)
        .post(
            "/inbound-notices",
            {
                "site_code": "TEAK",
                "expected_at": "2026-11-02T08:00:00+02:00",
                "client_ref": "PO-1",
                "lines": [{"sku": "SDB-001", "qty": "2", "uom": "CS"}],
            },
        )
        .json()
    )
    op = make_user("op@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[teak.pk], all_clients=True)
    return {"sdb": sdb, "teak": teak, "dbn": dbn, "water": water, "bins": bins, "notice": notice, "op": op}


def _cmd(type_, **payload):
    return {
        "command_id": str(uuid.uuid4()),
        "type": type_,
        "payload": payload,
        "recorded_at": "2026-10-21T10:00:00+02:00",
    }


def push(api, *commands, device="TC52-01"):
    response = api.client.post(
        "/api/v1/sync/push", {"device_id": device, "commands": list(commands)}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    return response.json()["results"]


def stock(floor, code):
    balance = InventoryBalance.objects.filter(location=floor["bins"][code], item=floor["water"]).first()
    return balance.qty_on_hand if balance else D(0)


# --- Push ----------------------------------------------------------------------------------------------------


def test_a_shift_of_offline_scans_applies_in_order(floor):
    op = api_for(floor["op"])
    notice_id = floor["notice"]["id"]
    line = _cmd("receipt.line", inbound_notice_id=notice_id, dock_code="DOCK-1", sku="SDB-001", qty="2", uom="CS")
    complete = _cmd("receipt.complete", inbound_notice_id=notice_id)
    results = push(op, line, complete)
    assert [r["status"] for r in results] == ["applied", "applied"], results
    assert results[1]["result"]["status"] == "completed"
    assert ReceiptLine.objects.get().command_id == uuid.UUID(line["command_id"])  # the envelope id reaches the service

    task = PutawayTask.objects.get()
    claim = _cmd("putaway.claim", task_id=str(task.pk))
    confirm = _cmd("putaway.confirm", task_id=str(task.pk), to_location_code="A-02", reason="Closer")
    move = _cmd(
        "inventory.move",
        site_id=str(floor["teak"].pk),
        location_code="A-01",
        to_location_code="A-02",
        client_id=str(floor["sdb"].pk),
        sku="SDB-001",
        qty="1",
        uom="CS",
    )
    results = push(op, claim, confirm, move)
    assert [r["status"] for r in results] == ["applied"] * 3, results
    assert (stock(floor, "A-01"), stock(floor, "A-02")) == (D(24), D(72))
    assert SyncCommand.objects.filter(device_id="TC52-01", status="applied").count() == 5


def test_retries_are_replayed_not_reapplied(floor):
    op = api_for(floor["op"])
    move = _cmd(
        "inventory.move",
        site_id=str(floor["teak"].pk),
        location_code="A-01",
        to_location_code="A-02",
        client_id=str(floor["sdb"].pk),
        sku="SDB-001",
        qty="10",
    )
    first = push(op, move)[0]
    again = push(op, move, move)  # lost response, then a duplicate inside one batch
    assert first["status"] == "applied" and not first["replayed"]
    assert all(r["status"] == "applied" and r["replayed"] and r["result"] == first["result"] for r in again)
    assert stock(floor, "A-02") == D(10)
    assert InventoryMovement.objects.filter(kind="move").count() == 2  # one transfer: out and in


def test_rejections_are_final_and_do_not_block_the_queue(floor):
    op = api_for(floor["op"])
    too_much = _cmd(
        "inventory.move",
        site_id=str(floor["teak"].pk),
        location_code="A-01",
        to_location_code="A-02",
        client_id=str(floor["sdb"].pk),
        sku="SDB-001",
        qty="1000",
    )
    unknown = _cmd("pallet.teleport")
    invalid = _cmd("putaway.confirm", task_id="not-a-uuid")
    fine = _cmd(
        "inventory.move",
        site_id=str(floor["teak"].pk),
        location_code="A-01",
        to_location_code="A-02",
        client_id=str(floor["sdb"].pk),
        sku="SDB-001",
        qty="1",
    )
    results = push(op, too_much, unknown, invalid, fine)
    assert [r["status"] for r in results] == ["rejected", "rejected", "rejected", "applied"]
    assert results[0]["problem"]["title"] == "Insufficient stock"
    assert results[1]["problem"]["title"] == "Unknown command"
    assert "task_id" in {e["field"] for e in results[2]["problem"]["errors"]}
    replay = push(op, too_much)[0]
    assert replay["status"] == "rejected" and replay["replayed"] and replay["problem"] == results[0]["problem"]
    assert stock(floor, "A-02") == D(1)


def test_offline_commands_get_the_same_checks_as_the_api(floor):
    elsewhere = make_user("dbn@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[floor["dbn"].pk], all_clients=True)
    move = _cmd(
        "inventory.move",
        site_id=str(floor["teak"].pk),
        location_code="A-01",
        to_location_code="A-02",
        client_id=str(floor["sdb"].pk),
        sku="SDB-001",
        qty="1",
    )
    assert push(api_for(elsewhere), move)[0]["problem"]["status"] == 404  # not their site

    # Someone else's command id is never replayed to them.
    assert push(api_for(floor["op"]), move)[0]["problem"]["title"] == "Command id in use"

    client_user = make_user("sdb@example.com", Role.CLIENT_USER, client_ids=[floor["sdb"].pk])
    response = api_for(client_user).client.post(
        "/api/v1/sync/push", {"device_id": "x", "commands": []}, content_type="application/json"
    )
    assert response.status_code == 403


def test_push_needs_csrf_with_a_session():
    user = make_user("op2@example.com", Role.WAREHOUSE_OPERATOR, all_sites=True, all_clients=True)
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    response = client.post("/api/v1/sync/push", {"device_id": "x", "commands": []}, content_type="application/json")
    assert response.status_code in (401, 403)


# --- Pull ----------------------------------------------------------------------------------------------------


def test_full_then_incremental_pull(floor):
    op = api_for(floor["op"])
    full = op.get("/sync/pull", site_id=str(floor["teak"].pk)).json()
    assert full["full"] is True
    assert {loc["code"] for loc in full["locations"]} == {"DOCK-1", "A-01", "A-02"}
    water = next(item for item in full["items"] if item["sku"] == "SDB-001")
    assert water["barcodes"] == [{"barcode": "6001234000017", "uom": "EA"}]
    assert [n["client_ref"] for n in full["notices"]] == ["PO-1"]
    assert full["tasks"] == [] and full["counts"] == []

    cursor = full["cursor"]
    count = (
        api_for(make_user("sup@example.com", Role.WAREHOUSE_SUPERVISOR, site_ids=[floor["teak"].pk], all_clients=True))
        .post("/cycle-counts", {"site_id": str(floor["teak"].pk), "location_codes": ["A-01"]})
        .json()
    )
    later = op.get("/sync/pull", site_id=str(floor["teak"].pk), cursor=cursor).json()
    assert later["full"] is False
    assert [c["id"] for c in later["counts"]] == [count["id"]]
    assert "lines" not in later["counts"][0]  # blind: bins only

    # A cursor far in the past behaves like "everything changed since then", never an error.
    old = (datetime.fromisoformat(cursor) - timedelta(days=30)).isoformat()
    assert op.get("/sync/pull", site_id=str(floor["teak"].pk), cursor=old).status_code == 200


def test_pull_is_scoped_to_the_operators_site(floor):
    assert api_for(floor["op"]).get("/sync/pull", site_id=str(floor["dbn"].pk)).status_code == 404


# --- Session sign-in -------------------------------------------------------------------------------------------


def _csrf_client():
    client = Client(enforce_csrf_checks=True)
    assert client.get("/scanner/").status_code == 200  # sets the CSRF cookie
    return client, client.cookies["csrftoken"].value


def test_scanner_sign_in_and_out():
    user = user_with_password("op3@example.com", "correct horse battery staple")
    client, token = _csrf_client()
    login = {"email": "op3@example.com", "password": "correct horse battery staple"}

    assert client.post("/api/v1/auth/session", login, content_type="application/json").status_code == 403  # no token
    wrong = client.post(
        "/api/v1/auth/session",
        login | {"password": "nope"},
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )
    assert wrong.status_code == 401
    ok = client.post("/api/v1/auth/session", login, content_type="application/json", headers={"X-CSRFToken": token})
    assert ok.status_code == 200 and ok.json()["roles"] == ["warehouse_operator"]
    assert client.get("/api/v1/auth/session").json()["user_id"] == str(user.pk)

    token = client.cookies["csrftoken"].value  # rotated at sign-in
    assert client.delete("/api/v1/auth/session", headers={"X-CSRFToken": token}).status_code == 204
    assert client.get("/api/v1/auth/session").status_code == 401


def test_client_users_cannot_use_the_scanner(make_client):
    sdb = make_client("SDB", "4111111111")
    user_with_password("client@example.com", "pw-123456789", role=Role.CLIENT_USER, client_ids=[sdb.pk])
    client, token = _csrf_client()
    response = client.post(
        "/api/v1/auth/session",
        {"email": "client@example.com", "password": "pw-123456789"},
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 403
    assert client.get("/api/v1/auth/session").status_code == 401


def user_with_password(email, password, role=Role.WAREHOUSE_OPERATOR, **grant):
    user = make_user(email, role, **(grant or {"all_sites": True, "all_clients": True}))
    user.set_password(password)
    user.save()
    return user


# --- Shell --------------------------------------------------------------------------------------------------


def test_scanner_shell_is_served_with_a_strict_policy():
    client = Client()
    page = client.get("/scanner/")
    assert page.status_code == 200
    assert "script-src 'self'" in page["Content-Security-Policy"]
    assert b"scanner/app.js" in page.content
    worker = client.get("/scanner/sw.js")
    assert worker["Content-Type"].startswith("text/javascript") and b"scanner-" in worker.content
    assert client.get("/scanner/manifest.webmanifest").json()["scope"] == "/scanner/"
