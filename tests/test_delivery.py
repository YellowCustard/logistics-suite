"""M3 on the road: arrival, proof of delivery with evidence, failed delivery, trip completion, offline sync."""

import itertools
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.platform.models import Document, ExceptionCase, OutboxEvent, Role
from apps.tms.models import DeliveryAttempt, Shipment, ShipmentUnit, Stop, Trip

from .conftest import api_for, make_user
from .test_packing import deliver
from .test_trips import new_trip, order, own, pick_and_pack, put, shipment_of, stops, yard  # noqa: F401

pytestmark = pytest.mark.django_db

D = Decimal
_files = itertools.count(1)


@pytest.fixture
def road(yard, settings, tmp_path):  # noqa: F811
    """A dispatched two-stop trip: Sandton gets a two-carton order, Rosebank a one-carton order."""
    settings.MEDIA_ROOT = str(tmp_path)
    user = make_user("sipho@example.com", Role.DRIVER)
    yard["driver"].user_id = user.pk
    yard["driver"].save()
    put(yard, "A-01", 50)
    sand, rose = order(yard, "SDB-SAND", 6), order(yard, "SDB-ROSE", 4)
    sand_units, rose_units = pick_and_pack(yard, sand, units=2), pick_and_pack(yard, rose)
    trip = new_trip(yard)
    stops(yard, trip, (yard["stores"]["SDB-SAND"], [sand]), (yard["stores"]["SDB-ROSE"], [rose]))
    own(yard, trip)
    for code in [*sand_units, *rose_units]:
        yard["op"].post(f"/trips/{trip['id']}/load", {"sscc": code})
    assert yard["disp"].post(f"/trips/{trip['id']}/dispatch").status_code == 200
    deliver()
    stop_ids = [str(s.pk) for s in Stop.objects.filter(trip_id=trip["id"]).order_by("seq")]
    return yard | {
        "trip": trip,
        "trip_id": trip["id"],
        "stop_ids": stop_ids,
        "sand": sand,
        "rose": rose,
        "sand_units": sand_units,
        "rose_units": rose_units,
        "driver_api": api_for(user),
    }


def upload(api, road, stop_id, kind="pod_signature", content=None, **extra):
    content = content or f"image-{next(_files)}".encode()
    file = SimpleUploadedFile("evidence.png", content, content_type="image/png")
    return api.client.post(
        f"/api/v1/trips/{road['trip_id']}/stops/{stop_id}/evidence",
        {"file": file, "kind": kind, **extra},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


def evidence(road, stop_id, kind="pod_signature"):
    response = upload(road["driver_api"], road, stop_id, kind)
    assert response.status_code == 201, response.content
    return response.json()["id"]


def pod(road, stop_id, api=None, **body):
    return (api or road["driver_api"]).post(f"/trips/{road['trip_id']}/stops/{stop_id}/pod", body)


def test_driver_delivers_one_stop_fails_another_and_completes(road):
    drv, trip_id = road["driver_api"], road["trip_id"]
    _, sand_stop, rose_stop = road["stop_ids"]

    mine = drv.get("/driver/trips").json()
    assert [t["number"] for t in mine] == [road["trip"]["number"]]
    view = mine[0]
    assert view["status"] == "dispatched" and view["vehicle_registration"] == "ND 123-456"
    assert [s["status"] for s in view["stops"]] == ["done", "pending", "pending"]  # the pickup is done at dispatch
    assert sorted(u["sscc"] for u in view["stops"][1]["shipments"][0]["units"]) == sorted(road["sand_units"])

    arrived = drv.post(f"/trips/{trip_id}/stops/{sand_stop}/arrive", {"latitude": "-26.107", "longitude": "28.056"})
    assert arrived.status_code == 200 and arrived.json()["stops"][1]["status"] == "arrived"
    assert OutboxEvent.objects.filter(type="tms.stop.arrived").count() == 1
    drv.post(f"/trips/{trip_id}/stops/{sand_stop}/arrive", {})
    assert OutboxEvent.objects.filter(type="tms.stop.arrived").count() == 1  # arriving twice changes nothing

    signature = evidence(road, sand_stop)
    photo = upload(drv, road, sand_stop, "pod_photo", b"same-bytes").json()["id"]
    assert upload(drv, road, sand_stop, "pod_photo", b"same-bytes").json()["id"] == photo  # a retried upload
    doc = Document.objects.get(pk=signature)
    assert doc.client_id == road["sdb"].pk and doc.linked_type == "tms.stop" and len(doc.sha256) == 64

    units = [{"sscc": code, "status": "delivered"} for code in road["sand_units"]]
    signed = pod(
        road,
        sand_stop,
        recipient_name="Lerato",
        signature_document_id=signature,
        photo_document_ids=[photo],
        units=units,
        latitude="-26.107",
        longitude="28.056",
    )
    assert signed.status_code == 200, signed.content
    assert signed.json()["stops"][1]["outcome"] == "delivered"
    sand_shipment = shipment_of(road["sand"])
    assert sand_shipment.status == "delivered" and sand_shipment.delivered_at is not None
    attempt = DeliveryAttempt.objects.get(stop_id=sand_stop)
    assert attempt.recipient_name == "Lerato" and attempt.signature_id == uuid.UUID(signature)
    assert [p.document_id for p in attempt.photos.all()] == [uuid.UUID(photo)]

    failure_photo = evidence(road, rose_stop, "pod_photo")
    failed = drv.post(
        f"/trips/{trip_id}/stops/{rose_stop}/failed", {"reason": "closed", "photo_document_ids": [failure_photo]}
    )
    assert failed.status_code == 200, failed.content
    assert shipment_of(road["rose"]).status == "failed"
    case = ExceptionCase.objects.get(kind="delivery.failed")
    assert case.client_id == road["sdb"].pk and case.detail["reason"] == "closed"

    deliver()
    road["sand"].refresh_from_db()
    road["rose"].refresh_from_db()
    assert road["sand"].status == "delivered" and road["rose"].status == "dispatched"
    told = OutboxEvent.objects.get(type="oms.sales_order.delivered")
    assert told.client_id == road["sdb"].pk and told.payload["recipient_name"] == "Lerato"
    assert told.payload["lines"] == [
        {"order_line_id": str(road["sand"].lines.get().pk), "item_id": str(road["water"].pk), "qty_delivered": "6.000"}
    ]
    assert OutboxEvent.objects.get(type="tms.shipment.delivery_failed").payload["reason"] == "closed"

    done = drv.post(f"/trips/{trip_id}/complete", {})
    assert done.status_code == 200 and done.json()["status"] == "completed"
    returned = shipment_of(road["rose"])
    assert returned.status == "ready"  # back at the warehouse, ready for another trip
    assert not ShipmentUnit.objects.filter(shipment=returned, loaded_trip__isnull=False).exists()
    completed = OutboxEvent.objects.get(type="tms.trip.completed").payload
    assert completed["stops_delivered"] == 1 and completed["stops_failed"] == 1
    assert completed["returned_shipment_ids"] == [str(returned.pk)]
    assert drv.post(f"/trips/{trip_id}/complete", {}).status_code == 200  # completing twice is harmless

    again = new_trip(road)
    replanned = stops(road, again, (road["stores"]["SDB-ROSE"], [road["rose"]]))
    assert replanned.status_code == 200, replanned.content


def test_pod_exceptions_and_a_fully_refused_shipment(road):
    _, sand_stop, rose_stop = road["stop_ids"]
    first, second = road["sand_units"]
    response = pod(
        road,
        sand_stop,
        recipient_name="Lerato",
        signature_document_id=evidence(road, sand_stop),
        units=[{"sscc": f"(00) {first}", "status": "damaged"}],
    )
    assert response.status_code == 200, response.content
    statuses = dict(ShipmentUnit.objects.filter(sscc__in=road["sand_units"]).values_list("sscc", "delivery_status"))
    assert statuses == {first: "damaged", second: "missing"}  # the unit not scanned is missing
    assert shipment_of(road["sand"]).status == "delivered"
    case = ExceptionCase.objects.get(kind="delivery.exception")
    assert sorted(u["status"] for u in case.detail["units"]) == ["damaged", "missing"]
    delivered = OutboxEvent.objects.get(type="tms.shipment.delivered").payload
    sizes = {code: D(c["qty"]) for code in road["sand_units"] for c in ShipmentUnit.objects.get(sscc=code).contents}
    assert D(delivered["lines"][0]["qty_delivered"]) == sizes[first]

    (rose_unit,) = road["rose_units"]
    refused = pod(
        road,
        rose_stop,
        recipient_name="Guard",
        photo_document_ids=[evidence(road, rose_stop, "pod_photo")],
        units=[{"sscc": rose_unit, "status": "refused"}],
    )
    assert refused.status_code == 200
    assert shipment_of(road["rose"]).status == "failed"
    assert OutboxEvent.objects.get(type="tms.shipment.delivery_failed").payload["reason"] == "refused"


def test_pod_rules(road):
    drv, trip_id = road["driver_api"], road["trip_id"]
    pickup, sand_stop, rose_stop = road["stop_ids"]
    no_evidence = pod(road, sand_stop, recipient_name="Lerato")
    assert no_evidence.status_code == 422 and no_evidence.json()["title"] == "Evidence required"
    elsewhere = pod(road, sand_stop, recipient_name="Lerato", signature_document_id=evidence(road, rose_stop))
    assert elsewhere.status_code == 422 and elsewhere.json()["title"] == "Unknown evidence"
    signature = evidence(road, sand_stop)
    stranger = pod(
        road,
        sand_stop,
        recipient_name="L",
        signature_document_id=signature,
        units=[{"sscc": road["rose_units"][0], "status": "delivered"}],
    )
    assert stranger.status_code == 422 and stranger.json()["title"] == "Not for this stop"
    future = (timezone.now() + timedelta(hours=1)).isoformat()
    late = pod(road, sand_stop, recipient_name="L", signature_document_id=signature, signed_at=future)
    assert late.status_code == 422 and late.json()["title"] == "Implausible time"
    assert pod(road, pickup, recipient_name="L", signature_document_id=signature).status_code == 409
    wrong_kind = upload(drv, road, sand_stop, "invoice")
    assert wrong_kind.status_code == 422

    assert pod(road, sand_stop, recipient_name="L", signature_document_id=signature).status_code == 200
    twice = pod(road, sand_stop, recipient_name="L", signature_document_id=signature)
    assert twice.status_code == 409 and twice.json()["title"] == "Already recorded"
    assert set(ShipmentUnit.objects.filter(sscc__in=road["sand_units"]).values_list("delivery_status", flat=True)) == {
        "delivered"
    }

    open_stop = drv.post(f"/trips/{trip_id}/complete", {})
    assert open_stop.status_code == 409 and "3" in open_stop.json()["detail"]
    other = drv.post(f"/trips/{trip_id}/stops/{rose_stop}/failed", {"reason": "other"})
    assert other.status_code == 422 and other.json()["title"] == "Note required"


def test_database_keeps_attempts_consistent(road):
    _, sand_stop, _ = road["stop_ids"]
    with pytest.raises(IntegrityError), transaction.atomic():
        DeliveryAttempt.objects.create(
            stop_id=sand_stop,
            trip_id=road["trip_id"],
            client=road["sdb"],
            outcome="delivered",
            captured_at=timezone.now(),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        DeliveryAttempt.objects.create(
            stop_id=sand_stop, trip_id=road["trip_id"], client=road["sdb"], outcome="failed", captured_at=timezone.now()
        )


def test_who_can_act_on_the_road(road):
    trip_id = road["trip_id"]
    _, sand_stop, _ = road["stop_ids"]
    other_driver = api_for(make_user("other@example.com", Role.DRIVER))
    assert other_driver.get("/driver/trips").json() == []
    assert other_driver.post(f"/trips/{trip_id}/stops/{sand_stop}/arrive", {}).status_code == 404
    assert upload(other_driver, road, sand_stop).status_code == 404
    assert road["op"].get("/driver/trips").status_code == 403
    assert road["op"].post(f"/trips/{trip_id}/stops/{sand_stop}/arrive", {}).status_code == 404
    client = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[road["sdb"].pk]))
    assert client.post(f"/trips/{trip_id}/complete", {}).status_code == 404

    # A dispatcher keys in a POD from a photo of the paper slip, for every unit at once.
    photo = upload(road["disp"], road, sand_stop, "pod_photo")
    assert photo.status_code == 201
    keyed = pod(road, sand_stop, api=road["disp"], recipient_name="From slip", photo_document_ids=[photo.json()["id"]])
    assert keyed.status_code == 200
    assert set(ShipmentUnit.objects.filter(sscc__in=road["sand_units"]).values_list("delivery_status", flat=True)) == {
        "delivered"
    }


def test_a_finished_trip_is_not_on_the_road(road):
    trip = Trip.objects.get(pk=road["trip_id"])
    Trip.objects.filter(pk=trip.pk).update(status="completed", completed_at=timezone.now())
    _, sand_stop, _ = road["stop_ids"]
    response = road["driver_api"].post(f"/trips/{trip.pk}/stops/{sand_stop}/arrive", {})
    assert response.status_code == 409 and response.json()["title"] == "Trip not on the road"


def test_offline_driver_pushes_queued_commands(road):
    drv, trip_id = road["driver_api"], road["trip_id"]
    _, sand_stop, rose_stop = road["stop_ids"]
    Trip.objects.filter(pk=trip_id).update(dispatched_at=timezone.now() - timedelta(hours=2))
    signature = evidence(road, sand_stop)  # uploaded first when signal returns
    signed_at = (timezone.now() - timedelta(minutes=30)).isoformat()
    commands = [
        {"command_id": str(uuid.uuid4()), "type": "trip.arrive", "payload": {"trip_id": trip_id, "stop_id": sand_stop}},
        {
            "command_id": str(uuid.uuid4()),
            "type": "trip.pod",
            "payload": {
                "trip_id": trip_id,
                "stop_id": sand_stop,
                "recipient_name": "Lerato",
                "signature_document_id": signature,
                "signed_at": signed_at,
            },
        },
        {
            "command_id": str(uuid.uuid4()),
            "type": "trip.failed",
            "payload": {"trip_id": trip_id, "stop_id": rose_stop, "reason": "no_access"},
        },
        {"command_id": str(uuid.uuid4()), "type": "trip.complete", "payload": {"trip_id": trip_id}},
    ]
    pushed = drv.post("/sync/push", {"device_id": "phone-1", "commands": commands})
    assert pushed.status_code == 200, pushed.content
    assert [r["status"] for r in pushed.json()["results"]] == ["applied"] * 4
    assert pushed.json()["results"][-1]["result"]["status"] == "completed"
    attempt = DeliveryAttempt.objects.get(stop_id=sand_stop)
    assert abs((attempt.captured_at - timezone.now()) + timedelta(minutes=30)) < timedelta(seconds=5)

    again = drv.post("/sync/push", {"device_id": "phone-1", "commands": commands[1:2]})
    assert again.json()["results"][0]["replayed"] is True
    assert DeliveryAttempt.objects.count() == 2


def test_overweight_load_warns(yard, settings):  # noqa: F811
    yard["truck"].capacity_kg = 5
    yard["truck"].save()
    put(yard, "A-01", 20)
    so = order(yard)
    first, second = pick_and_pack(yard, so, units=2)  # 4.5 kg each
    trip = new_trip(yard)
    stops(yard, trip, (yard["stores"]["SDB-SAND"], [so]))
    own(yard, trip)
    light = yard["op"].post(f"/trips/{trip['id']}/load", {"sscc": first}).json()
    assert light["warning"] is None and D(light["loaded_weight_kg"]) == D("4.5")
    heavy = yard["op"].post(f"/trips/{trip['id']}/load", {"sscc": second}).json()
    assert heavy["warning"] == "Loaded 9.000 kg is over the vehicle's 5 kg."
    assert Shipment.objects.get().status == "planned"
