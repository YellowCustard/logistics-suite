"""The staging commands behind the M3 exit test: seed_exit_test and exit_test_trip."""

import re
import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.masterdata.models import Person, Site
from apps.platform.models import Role, User
from apps.tms import trips
from apps.tms.models import Shipment, ShipmentUnit, Stop, Trip

from .conftest import api_for

pytestmark = pytest.mark.django_db

STOPS = """code,name,address,latitude,longitude
EXT-1,Corner shop,1 Main Rd,-26.101,28.231
EXT-2,Filling station,2 Main Rd,-26.102,28.232
EXT-3,Spaza,3 Main Rd,-26.103,28.233
EXT-4,Pharmacy,4 Main Rd,-26.104,28.234
EXT-5,Bakery,5 Main Rd,-26.105,28.235
"""


@pytest.fixture
def seeded(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.GS1_COMPANY_PREFIX = "0000000"
    call_command("seed_demo", "--not-production", stdout=StringIO())
    stops = tmp_path / "stops.csv"
    stops.write_text(STOPS)
    out = StringIO()
    args = ["--stops", str(stops), "--driver-email", "Sipho@Example.com", "--driver-name", "Sipho Dlamini"]
    call_command("seed_exit_test", "--not-production", *args, "--vehicle", "nd 123-456", stdout=out)
    text = out.getvalue()
    return {"batch": re.search(r"Batch (EXIT-\d{10})", text).group(1), "out": text, "stops": stops}


def _run(*args) -> str:
    out = StringIO()
    call_command("exit_test_trip", *args, stdout=out)
    return out.getvalue()


def test_seed_refuses_without_confirmation_or_demo_data(tmp_path):
    stops = tmp_path / "stops.csv"
    stops.write_text(STOPS)
    args = ["--stops", str(stops), "--driver-email", "d@example.com", "--driver-name", "A B", "--vehicle", "X"]
    with pytest.raises(CommandError, match="--not-production"):
        call_command("seed_exit_test", *args)
    with pytest.raises(CommandError, match="seed_demo"):
        call_command("seed_exit_test", "--not-production", *args)
    stops.write_text("code,name\nA,B\n")
    with pytest.raises(CommandError, match="missing column"):
        call_command("seed_exit_test", "--not-production", *args)


def test_seed_packs_one_carton_per_stop_and_creates_the_driver(seeded):
    batch = seeded["batch"]
    shipments = Shipment.objects.filter(client_ref__startswith=f"{batch}-").select_related("ship_to")
    assert sorted(s.ship_to.code for s in shipments) == ["EXT-1", "EXT-2", "EXT-3", "EXT-4", "EXT-5"]
    assert {s.status for s in shipments} == {"ready"} and all(s.units.count() == 1 for s in shipments)
    assert Site.objects.get(code="EXT-3").latitude is not None

    user = User.objects.get(email="sipho@example.com")
    assert [g.role for g in user.role_grants.all()] == [Role.DRIVER]
    assert Person.objects.get(user_id=user.pk).is_driver
    assert "sipho@example.com  " in seeded["out"]  # the one-time password follows the email
    assert f"exit_test_trip plan --not-production --batch {batch}" in seeded["out"]


def test_seed_refuses_a_staff_login_as_the_driver(seeded):
    User.objects.create_user(email="boss@example.com", password="x" * 12, is_staff=True)
    args = ["--stops", str(seeded["stops"]), "--driver-email", "boss@example.com", "--driver-name", "A B"]
    with pytest.raises(CommandError, match="not a driver-only login"):
        call_command("seed_exit_test", "--not-production", *args, "--vehicle", "X", stdout=StringIO())


def _signature(driver, trip, stop) -> str:
    file = SimpleUploadedFile("signature.png", f"sig-{stop.pk}".encode(), content_type="image/png")
    response = driver.client.post(
        f"/api/v1/trips/{trip.pk}/stops/{stop.pk}/evidence",
        {"file": file, "kind": "pod_signature"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.content
    return response.json()["id"]


def test_plan_then_report_a_trip_driven_with_a_dropped_signal(seeded):
    batch = seeded["batch"]
    plan = ["plan", "--batch", batch, "--vehicle", "ND 123-456", "--driver-email", "sipho@example.com"]
    with pytest.raises(CommandError, match="--not-production"):
        _run(*plan)
    with pytest.raises(CommandError, match="EXIT-1028193005"):
        _run("plan", "--not-production", "--batch", "10281930", "--vehicle", "X", "--driver-email", "x@example.com")
    planned = _run(*plan[:2], f"{batch}-3", *plan[3:], "--not-production")  # an order reference names its batch
    trip = Trip.objects.get()
    assert f"{trip.number} ({trip.pk}) is planning" in planned
    with pytest.raises(CommandError, match="not scanned onto the trip"):
        _run("dispatch", "--not-production", trip.number)
    for unit in ShipmentUnit.objects.filter(shipment__client_ref__startswith=f"{batch}-"):
        assert unit.sscc in planned
        trips.load(trip.pk, unit.sscc, user_id=None)  # the scanner's Load tile
    assert "dispatched" in _run("dispatch", "--not-production", trip.number)
    drops = list(Stop.objects.filter(trip=trip, seq__gt=1).select_related("site").order_by("seq"))
    assert [s.site.code for s in drops] == ["EXT-1", "EXT-2", "EXT-3", "EXT-4", "EXT-5"]
    with pytest.raises(CommandError, match="must be ready"):
        _run(*plan, "--not-production")  # the batch is on a trip now

    # Left an hour ago; stops 3 and 4 were signed 20 minutes before the phone had signal again.
    Trip.objects.filter(pk=trip.pk).update(dispatched_at=timezone.now() - timedelta(hours=1))
    driver = api_for(User.objects.get(email="sipho@example.com"))
    offline_at = (timezone.now() - timedelta(minutes=20)).isoformat()
    for n, stop in enumerate(drops, start=1):
        base = f"/trips/{trip.pk}/stops/{stop.pk}"
        if n == 5:
            response = driver.post(f"{base}/failed", {"reason": "closed"})
        else:
            body = {
                "recipient_name": f"Recipient {n}",
                "signature_document_id": _signature(driver, trip, stop),
                "latitude": str(stop.site.latitude),
                "longitude": str(stop.site.longitude),
            }
            if n in (3, 4):
                body["signed_at"] = offline_at
            response = driver.post(f"{base}/pod", body)
        assert response.status_code == 200, response.content

    report = StringIO()
    with pytest.raises(CommandError, match="does not meet"):
        call_command("exit_test_trip", "report", trip.number, stdout=report)
    assert "is dispatched, not completed" in report.getvalue()

    assert driver.post(f"/trips/{trip.pk}/complete").status_code == 200
    passed = _run("report", trip.number)
    assert "PASS (server side): 5 stops done, 2 POD(s) captured offline" in passed
    assert passed.count("synced late") == 2 and "FAIL" not in passed


def test_warns_when_the_batch_is_not_five_stops(seeded, monkeypatch):
    # A different batch reference from the fixture's, even if both runs fall in the same second.
    monkeypatch.setattr("apps.wms.management.commands.seed_exit_test.new_batch", lambda: "EXIT-0101000000")
    two = seeded["stops"].with_name("two.csv")
    two.write_text("\n".join(STOPS.splitlines()[:3]) + "\n")
    out = StringIO()
    args = ["--stops", str(two), "--driver-email", "sipho@example.com", "--driver-name", "Sipho Dlamini"]
    call_command("seed_exit_test", "--not-production", *args, "--vehicle", "ND 123-456", stdout=out)
    assert "needs 5 stops; this batch has 2" in out.getvalue()
    assert "needs 5 stops" not in seeded["out"]


def test_stops_can_be_piped_in(monkeypatch):
    from apps.wms.management.commands.seed_exit_test import read_stops

    monkeypatch.setattr("sys.stdin", StringIO("﻿" + STOPS))
    rows = read_stops("-")
    assert [r.code for r in rows] == ["EXT-1", "EXT-2", "EXT-3", "EXT-4", "EXT-5"]
    assert str(rows[0].latitude) == "-26.101000"


def test_a_rehearsal_loads_and_dispatches_in_one_go(seeded):
    out = _run(
        "plan", "--not-production", "--batch", seeded["batch"], "--vehicle", "ND 123-456",
        "--driver-email", "sipho@example.com", "--load-and-dispatch",
    )  # fmt: skip
    trip = Trip.objects.get()
    assert trip.status == "dispatched" and "Load tile" not in out
    assert ShipmentUnit.objects.filter(loaded_trip=trip).count() == 5
