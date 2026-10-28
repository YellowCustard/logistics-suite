"""The API behind the client portal's Items and Documents screens: editing items, pack sizes and barcodes, and
searching documents by the order, shipment, notice or receipt they are evidence of."""

import pytest

from apps.masterdata.models import Item, ItemBarcode
from apps.platform import gs1
from apps.platform.models import AuditEntry, Document, Role

from .conftest import api_for, make_user
from .test_delivery import evidence, pod, road, upload  # noqa: F401
from .test_inbound import _deliver_events, _receive, world  # noqa: F401
from .test_portal_inbound_api import _photo
from .test_trips import new_trip, order, own, pick_and_pack, put, shipment_of, stops, yard  # noqa: F401

pytestmark = pytest.mark.django_db


def test_gtin_check_digits():
    assert gs1.gtin_valid("6001234000013") and gs1.gtin_valid("96385074")  # EAN-13 and EAN-8
    assert gs1.gtin_valid("00012345600012") and gs1.gtin_valid("036000291452")  # GTIN-14 and UPC-A
    assert not gs1.gtin_valid("6001234000017")
    assert not gs1.looks_like_gtin("ABC-123") and not gs1.looks_like_gtin("12345")


def test_client_admin_edits_items_pack_sizes_and_barcodes(world):  # noqa: F811
    boss = api_for(make_user("boss@sdb.example", Role.CLIENT_ADMIN, client_ids=[world["sdb"].pk]))
    buyer = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[world["sdb"].pk]))
    water = Item.objects.get(sku="SDB-001")

    listed = {i["sku"]: i for i in buyer.get("/items").json()["items"]}
    assert set(listed) == {"SDB-001", "SDB-002"}
    assert listed["SDB-001"]["pack_sizes"] == [{"uom": "EA", "factor": 1}, {"uom": "CS", "factor": 24}]
    assert [b["barcode"] for b in listed["SDB-001"]["barcodes"]] == ["6001234000017"]
    version = listed["SDB-001"]["version"]

    # Edit with optimistic locking; a stale version is refused.
    changed = boss.patch(
        f"/items/{water.pk}", {"description": "Water 500ml still", "weight_kg": "0.52"}, {"If-Match": str(version)}
    )
    assert changed.status_code == 200, changed.content
    assert changed.json()["description"] == "Water 500ml still" and changed.json()["version"] == version + 1
    assert boss.patch(f"/items/{water.pk}", {"description": "X"}, {"If-Match": str(version)}).status_code == 412
    assert boss.patch(f"/items/{water.pk}", {"description": None}, {"If-Match": str(version + 1)}).status_code == 422
    # SKU and tracking are not editable: unknown fields are ignored, not applied.
    boss.patch(f"/items/{water.pk}", {"sku": "NEW", "lot_tracked": True}, {"If-Match": str(version + 1)})
    water.refresh_from_db()
    assert (water.sku, water.lot_tracked) == ("SDB-001", False)

    # Pack sizes, then barcodes for them.
    added = boss.post(f"/items/{water.pk}/pack-sizes", {"uom": "plt", "factor": 1440})
    assert added.status_code == 201 and {"uom": "PLT", "factor": 1440} in added.json()["pack_sizes"]
    assert boss.post(f"/items/{water.pk}/pack-sizes", {"uom": "CS", "factor": 12}).status_code == 409
    assert boss.post(f"/items/{water.pk}/pack-sizes", {"uom": "EA", "factor": 1}).status_code == 422

    case = boss.post(f"/items/{water.pk}/barcodes", {"barcode": " 16001234000010 ", "uom": "cs"})
    assert case.status_code == 201, case.content
    assert {"barcode": "16001234000010", "uom": "CS"} in [
        {k: b[k] for k in ("barcode", "uom")} for b in case.json()["barcodes"]
    ]
    typo = boss.post(f"/items/{water.pk}/barcodes", {"barcode": "6001234000014"})
    assert typo.status_code == 422 and "check digit" in typo.json()["detail"]
    assert boss.post(f"/items/{water.pk}/barcodes", {"barcode": "SDB 001"}).status_code == 422
    assert boss.post(f"/items/{water.pk}/barcodes", {"barcode": "X1", "uom": "BOX"}).status_code == 422
    juice = Item.objects.get(sku="SDB-002")
    taken = boss.post(f"/items/{juice.pk}/barcodes", {"barcode": "16001234000010"})
    assert taken.status_code == 409 and "SDB-001" in taken.json()["detail"]
    # Another client may use the same code for its own item.
    other = Item.objects.get(sku="OTH-001")
    other_admin = api_for(make_user("a@other.example", Role.CLIENT_ADMIN, client_ids=[world["other"].pk]))
    assert other_admin.post(f"/items/{other.pk}/barcodes", {"barcode": "16001234000010"}).status_code == 201

    barcode = ItemBarcode.objects.get(client=world["sdb"], barcode="16001234000010")
    removed = boss.delete(f"/items/{water.pk}/barcodes/{barcode.pk}")
    assert removed.status_code == 200 and "16001234000010" not in [b["barcode"] for b in removed.json()["barcodes"]]
    assert AuditEntry.objects.filter(entity_type="masterdata.itembarcode", action="delete").exists()

    # Deactivating keeps the item listed but stops new orders and notices from using it.
    version = Item.objects.get(pk=water.pk).version
    assert (
        boss.patch(f"/items/{water.pk}", {"is_active": False}, {"If-Match": str(version)}).json()["is_active"] is False
    )

    # Users without the admin role read only; other clients and site staff cannot touch the item.
    assert buyer.patch(f"/items/{juice.pk}", {"description": "X"}, {"If-Match": "0"}).status_code == 403
    assert buyer.post(f"/items/{juice.pk}/barcodes", {"barcode": "J1"}).status_code == 403
    assert other_admin.post(f"/items/{juice.pk}/barcodes", {"barcode": "J1"}).status_code == 404
    ops = api_for(make_user("ops@example.com", Role.WAREHOUSE_SUPERVISOR, all_clients=True, all_sites=True))
    assert ops.post(f"/items/{juice.pk}/pack-sizes", {"uom": "CS", "factor": 6}).status_code == 403


def test_item_import_rejects_mistyped_gtins(admin_api, world):  # noqa: F811
    csv_text = "sku,description,barcode\nSDB-009,Soda,6001234000014\n"
    response = admin_api.upload(f"/items:import?client_id={world['sdb'].pk}", csv_text)
    assert response.status_code == 422 and "check digit" in str(response.json()["errors"])


def test_documents_search_finds_receipt_photos_by_notice_and_receipt(world, bearer_api):  # noqa: F811
    photo = _photo(world)
    notice, receipt, _ = _receive(
        world,
        bearer_api,
        [
            {"sku": "SDB-001", "qty": "8", "uom": "CS"},
            {"sku": "SDB-001", "qty": "2", "uom": "CS", "condition": "damaged", "photo_document_ids": [str(photo.pk)]},
        ],
    )
    _deliver_events()
    upload_file = Document.objects.create(
        client_id=world["sdb"].pk,
        kind="import_file",
        storage_key="k/orders.csv",
        original_name="orders-november.csv",
        mime="text/csv",
        size_bytes=10,
        sha256="1" * 64,
        captured_at="2026-11-05T10:00:00+02:00",
    )
    buyer = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[world["sdb"].pk]))

    everything = buyer.get("/documents").json()["items"]
    assert [d["id"] for d in everything] == [str(upload_file.pk), str(photo.pk)]  # newest first
    label = everything[1]["reference"]
    assert label == f"{receipt['number']} · {notice['number']} ({notice['client_ref']}) · SDB-001"
    assert everything[0]["reference"] == ""

    by = lambda **p: [d["id"] for d in buyer.get("/documents", **p).json()["items"]]  # noqa: E731
    assert by(q=notice["client_ref"].lower()) == [str(photo.pk)]
    assert by(q=notice["number"]) == [str(photo.pk)]
    assert by(q=receipt["number"]) == [str(photo.pk)]
    assert by(q="orders-nov") == [str(upload_file.pk)]
    assert by(q="nothing-like-this") == []
    assert by(kind="receipt_photo") == [str(photo.pk)]
    assert by(kind=["receipt_photo", "import_file"]) == [str(upload_file.pk), str(photo.pk)]
    assert by(captured_from="2026-11-03") == [str(upload_file.pk)]  # the photo was captured on 2026-11-02
    assert by(captured_from="2026-11-02", captured_to="2026-11-02") == [str(photo.pk)]

    stranger = api_for(make_user("x@other.example", Role.CLIENT_USER, client_ids=[world["other"].pk]))
    assert stranger.get("/documents").json()["items"] == []
    assert stranger.get("/documents", q=notice["number"]).json()["items"] == []


def test_documents_search_finds_pods_by_order_shipment_and_reference(road):  # noqa: F811
    _, sand_stop, _ = road["stop_ids"]
    signature = evidence(road, sand_stop)
    photo = evidence(road, sand_stop, kind="pod_photo")
    units = [{"sscc": code, "status": "delivered"} for code in road["sand_units"]]
    assert (
        pod(road, sand_stop, recipient_name="Lerato", signature_document_id=signature, units=units).status_code == 200
    )
    buyer = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[road["sdb"].pk]))
    sand = road["sand"]
    shipment = shipment_of(sand)

    pods = buyer.get("/documents", kind=["pod_signature", "pod_photo"]).json()["items"]
    assert [d["id"] for d in pods] == [photo, signature]
    assert {d["reference"] for d in pods} == {f"{shipment.number} · {sand.number} · {sand.client_ref}"}
    for q in (sand.number, shipment.number, sand.client_ref.lower()):
        assert {d["id"] for d in buyer.get("/documents", q=q).json()["items"]} == {photo, signature}
    assert buyer.get("/documents", q=road["rose"].number).json()["items"] == []

    other = api_for(make_user("x@other.example", Role.CLIENT_USER, client_ids=[road["other"].pk]))
    assert other.get("/documents", q=sand.number).json()["items"] == []
