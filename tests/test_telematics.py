"""M3 telematics: position ingest (poll and webhook), geofence arrival, ETA, vehicle matching and the simulator."""

import json
import math
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.integrations.ports import Position, TelematicsVehicle
from apps.integrations.telematics import adapter
from apps.integrations.telematics.fake import SIGNATURE_HEADER, sign
from apps.masterdata.models import Vehicle
from apps.platform.models import ExceptionCase, OutboxEvent, Role
from apps.tms import telematics
from apps.tms.models import Stop, StopStatus, TelematicsCursor, Trip, VehiclePosition

from .conftest import api_for, make_user
from .test_delivery import evidence, pod, road  # noqa: F401
from .test_trips import new_trip, order, pick_and_pack, put, stops, yard  # noqa: F401

pytestmark = pytest.mark.django_db

FAKE = "apps.integrations.telematics.fake.FakeTelematics"
TEAK = (-26.0473, 28.0118)
SAND = (-26.1076, 28.0567)
ROSE = (-26.1452, 28.0410)
WEBHOOK = "/api/v1/integrations/telematics/webhook"


@pytest.fixture
def fake(settings):
    settings.TELEMATICS_ADAPTER = FAKE
    settings.TELEMATICS_WEBHOOK_SECRET = "test-webhook-secret"
    port = adapter()
    port.reset()
    yield port
    port.reset()


@pytest.fixture
def mapped(road):  # noqa: F811
    """The road trip with coordinates on its sites and the truck linked to telematics unit U1."""
    for site, (lat, lon) in (
        (road["teak"], TEAK),
        (road["stores"]["SDB-SAND"], SAND),
        (road["stores"]["SDB-ROSE"], ROSE),
    ):
        site.latitude, site.longitude = Decimal(str(lat)), Decimal(str(lon))
        site.save()
    road["truck"].telematics_id = "U1"
    road["truck"].save()
    Trip.objects.filter(pk=road["trip_id"]).update(dispatched_at=timezone.now() - timedelta(hours=1))
    return road


def fix(unit="U1", at=None, lat=TEAK[0], lon=TEAK[1], speed=0.0, **extra) -> Position:
    return Position(unit, at or timezone.now(), lat, lon, speed, extra.get("heading"), extra.get("ignition", True))


def test_distance_is_haversine():
    assert telematics.distance_m(*SAND, *SAND) == 0
    # One degree of latitude is about 111.2 km on the mean-radius sphere.
    assert math.isclose(telematics.distance_m(0, 0, 1, 0), 111_195, rel_tol=1e-3)


def test_ingest_stores_once_rejects_junk_and_flags_unknown_units(mapped):
    now = timezone.now()
    good = fix(at=now - timedelta(minutes=2), speed=50)
    batch = [
        good,
        good,  # repeated in the same batch
        fix(at=now - timedelta(minutes=1), lat=float("nan")),
        fix(at=now + timedelta(hours=1)),  # from the future
        fix(at=now - timedelta(minutes=1), lat=0.0, lon=0.0),  # no GPS lock
        fix(at=now - timedelta(minutes=1), lat=91.0),
        fix("U9", at=now),
    ]
    stats = telematics.ingest(batch)
    assert (stats.received, stats.accepted, stats.rejected, stats.unknown) == (7, 2, 4, 1)
    again = telematics.ingest([good, fix("U9", at=now)])  # a re-polled window
    assert again.unknown == 1
    stored = VehiclePosition.objects.get()
    assert stored.vehicle_id == mapped["truck"].pk and stored.speed_kmh == 50
    case = ExceptionCase.objects.get(kind="telematics.unknown_vehicle")
    assert case.entity_id == "U9"  # one case per unit, however often it reports


def test_webhook_needs_a_valid_signature(mapped, fake, settings):
    vendor = Client(enforce_csrf_checks=True)  # a vendor has no session or CSRF token
    body = json.dumps(
        {"positions": [{"unit_id": "U1", "time": timezone.now().isoformat(), "lat": SAND[0], "lon": SAND[1]}]}
    ).encode()

    def push(payload: bytes, signature: str | None):
        headers = {SIGNATURE_HEADER: signature} if signature else {}
        return vendor.post(WEBHOOK, payload, content_type="application/json", headers=headers)

    assert push(body, None).status_code == 401
    assert push(body, sign(body, "someone-else")).status_code == 401
    assert push(b'{"positions": [{"lat": 1}]}', sign(b'{"positions": [{"lat": 1}]}')).status_code == 400
    assert push(b"not json", sign(b"not json")).status_code == 400
    accepted = push(body, sign(body))
    assert accepted.status_code == 202, accepted.content
    assert accepted.json() == {"received": 1, "accepted": 1, "rejected": 0, "unknown": 0, "arrivals": 1}
    assert push(body, sign(body)).status_code == 202  # a retried push
    assert VehiclePosition.objects.count() == 1

    settings.TELEMATICS_WEBHOOK_SECRET = ""
    assert push(body, sign(body, "anything")).status_code == 401  # no secret: nothing verifies
    settings.TELEMATICS_ADAPTER = ""
    assert push(body, sign(body, "anything")).status_code == 404


def test_geofence_marks_the_stop_arrived(mapped):
    _, sand_stop, rose_stop = mapped["stop_ids"]
    now = timezone.now()
    trip = Trip.objects.get(pk=mapped["trip_id"])
    early = fix(at=trip.dispatched_at - timedelta(minutes=5), lat=SAND[0], lon=SAND[1])
    passing = fix(at=now - timedelta(minutes=10), lat=ROSE[0], lon=ROSE[1], speed=60)
    outside = fix(at=now - timedelta(minutes=6), lat=SAND[0] + 0.01, lon=SAND[1], speed=5)  # ~1.1 km away
    parked = fix(at=now - timedelta(minutes=5), lat=SAND[0] + 0.0005, lon=SAND[1], speed=2)  # ~55 m away
    assert telematics.ingest([early, passing, outside]).arrivals == 0
    assert Stop.objects.get(pk=rose_stop).status == StopStatus.PENDING  # driving past isn't arriving

    assert telematics.ingest([parked]).arrivals == 1
    stop = Stop.objects.get(pk=sand_stop)
    assert (stop.status, stop.arrival_source, stop.arrived_at) == ("arrived", "geofence", parked.recorded_at)
    event = OutboxEvent.objects.get(type="tms.stop.arrived")
    assert event.payload["source"] == "geofence" and event.payload["stop_id"] == sand_stop
    assert telematics.ingest([fix(lat=SAND[0], lon=SAND[1])]).arrivals == 0  # once only

    # The driver's POD still works after an automatic arrival.
    pod(mapped, sand_stop, recipient_name="Lerato", signature_document_id=evidence(mapped, sand_stop))
    assert Stop.objects.get(pk=sand_stop).status == StopStatus.DONE


def test_geofence_arrival_can_be_switched_off(mapped, settings):
    settings.GEOFENCE_AUTO_ARRIVE = False
    assert telematics.ingest([fix(lat=SAND[0], lon=SAND[1])]).arrivals == 0
    assert VehiclePosition.objects.count() == 1
    assert Stop.objects.filter(status=StopStatus.ARRIVED).count() == 0


def test_eta_for_clients_and_position_for_dispatchers(mapped, settings):
    settings.ETA_AVERAGE_KMH, settings.ETA_ROAD_FACTOR, settings.ETA_MINUTES_PER_STOP = 60, 1.0, 10
    _, sand_stop, rose_stop = mapped["stop_ids"]
    buyer = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[mapped["sdb"].pk]))
    rose = buyer.get("/shipments", sales_order_id=str(mapped["rose"].pk)).json()["items"][0]
    assert buyer.get(f"/shipments/{rose['id']}/tracking").json()["eta"] is None  # no fix yet

    telematics.ingest([fix(speed=40, heading=170)])
    before = timezone.now()
    eta = buyer.get(f"/shipments/{rose['id']}/tracking").json()["eta"]
    to_sand = telematics.distance_m(*TEAK, *SAND) / 1000  # km at 60 km/h is minutes
    to_rose = to_sand + 10 + telematics.distance_m(*SAND, *ROSE) / 1000
    expected = before + timedelta(minutes=to_rose)
    assert abs((datetime.fromisoformat(eta) - expected).total_seconds()) < 5

    trip = mapped["disp"].get(f"/trips/{mapped['trip_id']}").json()
    assert trip["last_position"]["speed_kmh"] == 40 and trip["last_position"]["heading"] == 170
    etas = {s["id"]: s["eta"] for s in trip["stops"]}
    assert etas[sand_stop] < etas[rose_stop] and etas[mapped["stop_ids"][0]] is None

    VehiclePosition.objects.update(recorded_at=timezone.now() - timedelta(minutes=30))
    assert buyer.get(f"/shipments/{rose['id']}/tracking").json()["eta"] is None  # stale fix
    assert all(s["eta"] is None for s in mapped["disp"].get(f"/trips/{mapped['trip_id']}").json()["stops"])


def test_poll_advances_a_cursor_and_survives_vendor_failure(mapped, fake):
    now = timezone.now()
    fake.feed([fix(at=now - timedelta(minutes=m), speed=30) for m in (50, 30, 10)])
    first = telematics.poll(fake, now=now)
    assert first.stats.accepted == 3 and TelematicsCursor.objects.get().polled_until == now
    fake.feed([fix(at=now + timedelta(seconds=30), speed=30)])
    second = telematics.poll(fake, now=now + timedelta(minutes=1))
    assert second.since == now - telematics.POLL_OVERLAP  # re-reads late fixes
    assert VehiclePosition.objects.count() == 4

    class Down:
        def fetch_positions(self, since, until):
            raise ConnectionError("vendor down")

    with pytest.raises(ConnectionError):
        telematics.poll(Down(), now=now + timedelta(minutes=2))
    assert TelematicsCursor.objects.get().polled_until == now + timedelta(minutes=1)


def test_poll_catches_up_in_bounded_windows(mapped, fake):
    now = timezone.now()
    TelematicsCursor.objects.create(name=telematics.CURSOR, polled_until=now - timedelta(hours=5))
    result = telematics.poll(fake, now=now)
    assert result.until == now - timedelta(hours=4)


def test_run_telematics_once(mapped, fake, capsys):
    fake.feed([fix(at=timezone.now() - timedelta(seconds=20))])
    call_command("run_telematics", "--once")
    assert "accepted=1" in capsys.readouterr().out


def test_sync_vehicles_links_by_registration(yard, fake):  # noqa: F811
    yard["spare"].telematics_id = "U7"
    yard["spare"].save()
    fake.set_vehicles(
        [
            TelematicsVehicle("U1", "nd123456"),  # spacing and case differ from "ND 123-456"
            TelematicsVehicle("U2", "GP 000-111"),
            TelematicsVehicle("U3", "ND 999-000"),  # already linked to U7
        ]
    )
    call_command("sync_telematics_vehicles")
    assert Vehicle.objects.get(pk=yard["truck"].pk).telematics_id == "U1"
    assert Vehicle.objects.get(pk=yard["spare"].pk).telematics_id == "U7"
    assert ExceptionCase.objects.get(kind="telematics.unknown_vehicle").entity_id == "U2"
    result = telematics.sync_vehicles(fake)
    assert (result.linked, result.unchanged, result.conflicts) == ([], 1, ["ND 999-000"])


def test_positions_past_retention_are_purged(mapped, settings):
    settings.TELEMATICS_RETENTION_DAYS = 30
    now = timezone.now()
    telematics.ingest([fix(at=now - timedelta(minutes=5))])
    VehiclePosition.objects.bulk_create(
        [
            VehiclePosition(
                vehicle=mapped["truck"], recorded_at=now - timedelta(days=31), latitude=TEAK[0], longitude=TEAK[1]
            )
        ]
    )
    assert telematics.purge_positions() == 1 and VehiclePosition.objects.count() == 1


def test_simulator_drives_to_the_next_stop(mapped):
    _, sand_stop, _ = mapped["stop_ids"]
    Vehicle.objects.filter(pk=mapped["truck"].pk).update(telematics_id=None)
    call_command("simulate_fleet", "--once")
    assert VehiclePosition.objects.count() == 0  # unlinked vehicles are left alone without --link

    call_command("simulate_fleet", "--once", "--link", "--interval", "60", "--speed-kmh", "60")
    assert Vehicle.objects.get(pk=mapped["truck"].pk).telematics_id == "sim-ND 123-456"
    first = VehiclePosition.objects.get()
    assert math.isclose(telematics.distance_m(*TEAK, first.latitude, first.longitude), 1000, rel_tol=0.01)
    assert first.speed_kmh == 60

    VehiclePosition.objects.update(recorded_at=timezone.now() - timedelta(minutes=1))
    call_command("simulate_fleet", "--once", "--interval", "3600", "--speed-kmh", "60")
    stop = Stop.objects.get(pk=sand_stop)
    assert (stop.status, stop.arrival_source) == ("arrived", "geofence")
