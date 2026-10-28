"""M3 tracking for clients (status, timeline, POD) and invite links for subcontracted drivers."""

import re
import uuid
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.platform.models import Role, User
from apps.tms.models import CarrierLink, Trip

from .conftest import Api, api_for, make_user
from .test_delivery import evidence, pod, road  # noqa: F401
from .test_trips import new_trip, order, pick_and_pack, put, stops, yard  # noqa: F401

pytestmark = pytest.mark.django_db


def test_client_tracks_a_shipment_to_its_pod(road):  # noqa: F811
    _, sand_stop, rose_stop = road["stop_ids"]
    client = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[road["sdb"].pk]))
    sand = client.get("/shipments", sales_order_id=str(road["sand"].pk)).json()["items"][0]
    rose = client.get("/shipments", sales_order_id=str(road["rose"].pk)).json()["items"][0]

    on_the_way = client.get(f"/shipments/{rose['id']}/tracking").json()
    assert on_the_way["status"] == "in_transit" and on_the_way["trip_number"] == road["trip"]["number"]
    assert on_the_way["stops_before"] == 1 and on_the_way["pod"] is None and on_the_way["eta"] is None
    assert [e["what"] for e in on_the_way["events"]] == ["order_received", "packed", "dispatched"]
    assert client.get(f"/shipments/{rose['id']}/pod").status_code == 404

    road["driver_api"].post(f"/trips/{road['trip_id']}/stops/{sand_stop}/arrive", {})
    signature, photo = evidence(road, sand_stop), evidence(road, sand_stop, "pod_photo")
    pod(road, sand_stop, recipient_name="Lerato", signature_document_id=signature, photo_document_ids=[photo])
    assert client.get(f"/shipments/{rose['id']}/tracking").json()["stops_before"] == 0

    done = client.get(f"/shipments/{sand['id']}/tracking").json()
    assert done["status"] == "delivered"
    assert [e["what"] for e in done["events"]][-2:] == ["arrived", "delivered"]
    proof = client.get(f"/shipments/{sand['id']}/pod").json()
    assert proof["recipient_name"] == "Lerato" and proof["outcome"] == "delivered"
    assert proof["signature"]["id"] == signature and [p["id"] for p in proof["photos"]] == [photo]
    assert {u["delivery_status"] for u in proof["units"]} == {"delivered"}
    download = client.client.get(f"/api/v1/documents/{signature}/download")
    assert download.status_code == 200 and download["Content-Type"] == "image/png"

    road["driver_api"].post(
        f"/trips/{road['trip_id']}/stops/{rose_stop}/failed", {"reason": "no_access", "note": "Gate locked"}
    )
    failed = client.get(f"/shipments/{rose['id']}/pod").json()
    assert failed["outcome"] == "failed" and failed["reason"] == "no_access" and failed["units"] == []

    stranger = api_for(make_user("x@oth.example", Role.CLIENT_USER, client_ids=[road["other"].pk]))
    assert stranger.get(f"/shipments/{sand['id']}/tracking").status_code == 404
    assert stranger.get(f"/shipments/{sand['id']}/pod").status_code == 404
    assert stranger.client.get(f"/api/v1/documents/{signature}/download").status_code == 404


def _token(url: str) -> str:
    return re.search(r"#link/(.+)$", url).group(1)


def redeem(token: str) -> tuple[Client, object]:
    browser = Client(enforce_csrf_checks=True)
    browser.get("/scanner/")  # sets the CSRF cookie, as the PWA shell does
    csrf = browser.cookies["csrftoken"].value
    response = browser.post(
        "/api/v1/carrier-links/redeem",
        {"token": token},
        content_type="application/json",
        headers={"X-CSRFToken": csrf, "Idempotency-Key": str(uuid.uuid4())},
    )
    return browser, response


@pytest.fixture
def carrier_trip(yard, settings, tmp_path):  # noqa: F811
    settings.MEDIA_ROOT = str(tmp_path)
    put(yard, "A-01", 20)
    so = order(yard)
    (code,) = pick_and_pack(yard, so)
    trip = new_trip(yard)
    stops(yard, trip, (yard["stores"]["SDB-SAND"], [so]))
    yard["disp"].post(f"/trips/{trip['id']}/assign", {"carrier_id": str(yard["carrier"].pk), "agreed_cost": "900"})
    yard["op"].post(f"/trips/{trip['id']}/load", {"sscc": code})
    return yard | {"trip": trip, "so": so, "code": code}


def test_carrier_driver_signs_in_with_a_link_and_delivers(carrier_trip):
    y, trip = carrier_trip, carrier_trip["trip"]
    made = y["disp"].post(f"/trips/{trip['id']}/carrier-links", {"driver_name": "Bongani", "valid_hours": 12})
    assert made.status_code == 201, made.content
    token = _token(made.json()["url"])
    link = CarrierLink.objects.get()
    assert link.token_sha256 != token and len(link.token_sha256) == 64  # only the hash is kept

    browser, signed_in = redeem(token)
    assert signed_in.status_code == 200, signed_in.content
    assert signed_in.json()["roles"] == ["carrier_driver"]
    assert browser.session.get_expiry_date() <= link.expires_at + timedelta(seconds=1)
    driver = Api(browser, {"X-CSRFToken": browser.cookies["csrftoken"].value})
    mine = driver.get("/driver/trips").json()
    assert [t["id"] for t in mine] == [trip["id"]]
    assert (
        driver.get("/trips").json()["items"] == [] and driver.get("/shipments").json()["items"] == []
    )  # no site access

    y["disp"].post(f"/trips/{trip['id']}/dispatch")
    stop_id = mine[0]["stops"][1]["id"]
    photo = browser.post(
        f"/api/v1/trips/{trip['id']}/stops/{stop_id}/evidence",
        {"file": SimpleUploadedFile("slip.png", b"slip", content_type="image/png"), "kind": "pod_photo"},
        headers={"X-CSRFToken": browser.cookies["csrftoken"].value, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert photo.status_code == 201, photo.content
    body = {"recipient_name": "Store manager", "photo_document_ids": [photo.json()["id"]]}
    assert driver.post(f"/trips/{trip['id']}/stops/{stop_id}/pod", body).status_code == 200
    assert driver.post(f"/trips/{trip['id']}/complete", {}).status_code == 200

    _, again = redeem(token)  # the trip is finished, so the link no longer works
    assert again.status_code == 401


def test_carrier_links_expire_are_replaced_and_revoked(carrier_trip):
    y, trip = carrier_trip, carrier_trip["trip"]
    first = _token(y["disp"].post(f"/trips/{trip['id']}/carrier-links", {}).json()["url"])
    browser, ok = redeem(first)
    assert ok.status_code == 200
    second = _token(y["disp"].post(f"/trips/{trip['id']}/carrier-links", {}).json()["url"])
    assert redeem(first)[1].status_code == 401  # replaced
    assert browser.get("/api/v1/driver/trips").status_code == 401  # and its driver is signed out
    assert User.objects.get(email__startswith="carrier-link-").is_active is False

    CarrierLink.objects.filter(revoked_at__isnull=True).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert redeem(second)[1].status_code == 401  # expired

    third = _token(y["disp"].post(f"/trips/{trip['id']}/carrier-links", {}).json()["url"])
    assert y["disp"].delete(f"/trips/{trip['id']}/carrier-links").status_code == 204
    assert redeem(third)[1].status_code == 401
    assert redeem("not-a-real-token-but-long-enough")[1].status_code == 401


def test_carrier_link_rules(carrier_trip):
    y, trip = carrier_trip, carrier_trip["trip"]
    own = new_trip(y)
    assert y["disp"].post(f"/trips/{own['id']}/carrier-links", {}).status_code == 409  # no carrier
    assert y["op"].post(f"/trips/{trip['id']}/carrier-links", {}).status_code == 403
    assert y["disp"].post(f"/trips/{trip['id']}/carrier-links", {"valid_hours": 500}).status_code == 422
    browser, _ = redeem(_token(y["disp"].post(f"/trips/{trip['id']}/carrier-links", {}).json()["url"]))
    driver = Api(browser, {"X-CSRFToken": browser.cookies["csrftoken"].value})
    other = Trip.objects.get(pk=own["id"])
    assert driver.post(f"/trips/{other.pk}/complete", {}).status_code == 404  # only its own trip

    no_csrf = Client(enforce_csrf_checks=True).post(
        "/api/v1/carrier-links/redeem",
        {"token": "x" * 30},
        content_type="application/json",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert no_csrf.status_code == 403
