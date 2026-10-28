"""The API behind the client portal's Inbound screens: notice search and cancellation flag, and receipts a client
may read with SKUs and photos of damaged stock."""

import pytest

from apps.platform.models import Document, Role

from .conftest import api_for, make_user
from .test_inbound import _asn, _deliver_events, _receive, world  # noqa: F401

pytestmark = pytest.mark.django_db


def _photo(world):  # noqa: F811
    return Document.objects.create(
        client_id=world["sdb"].pk,
        kind="receipt_photo",
        storage_key="k/1.jpg",
        mime="image/jpeg",
        size_bytes=10,
        sha256="0" * 64,
        captured_at="2026-11-02T08:00:00Z",
    )


def test_notices_search_newest_first_and_can_cancel(world, bearer_api):  # noqa: F811
    api = bearer_api(world["sdb"])
    first, second = _asn(api, ref="PO-100").json(), _asn(api, ref="PX-200").json()
    assert first["can_cancel"] is True

    listed = api.get("/inbound-notices", newest_first="true").json()["items"]
    assert [n["id"] for n in listed] == [second["id"], first["id"]]
    assert [n["id"] for n in api.get("/inbound-notices", q="po-1").json()["items"]] == [first["id"]]
    assert [n["id"] for n in api.get("/inbound-notices", q=second["number"]).json()["items"]] == [second["id"]]

    cancelled = api.post(f"/inbound-notices/{first['id']}/cancel", {"reason": "Supplier delay"}).json()
    assert cancelled["status"] == "cancelled" and cancelled["can_cancel"] is False


def test_clients_read_their_receipts_with_skus_and_damage_photos(world, bearer_api):  # noqa: F811
    photo = _photo(world)
    notice, receipt, op = _receive(
        world,
        bearer_api,
        [
            {"sku": "SDB-001", "qty": "8", "uom": "CS"},
            {"sku": "SDB-001", "qty": "2", "uom": "CS", "condition": "damaged", "photo_document_ids": [str(photo.pk)]},
        ],
    )
    _deliver_events()
    buyer = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[world["sdb"].pk]))

    [mine] = buyer.get("/receipts", inbound_notice_id=notice["id"]).json()["items"]
    assert mine["id"] == receipt["id"] and mine["status"] == "completed"
    good, damaged = mine["lines"]
    assert (good["sku"], good["condition"], good["photos"]) == ("SDB-001", "good", [])
    assert damaged["condition"] == "damaged"
    assert damaged["photos"] == [{"id": str(photo.pk), "mime": "image/jpeg"}]
    assert buyer.get(f"/receipts/{receipt['id']}").json()["lines"][1]["photos"][0]["id"] == str(photo.pk)
    assert buyer.get(f"/documents/{photo.pk}").status_code == 200

    # The notice shows what was expected against what arrived.
    [line] = buyer.get(f"/inbound-notices/{notice['id']}").json()["lines"]
    assert (line["qty"], line["qty_received"]) == ("10.000", "10.000")

    # Other clients see nothing; staff keep their site scoping.
    stranger = api_for(make_user("x@other.example", Role.CLIENT_USER, client_ids=[world["other"].pk]))
    assert stranger.get("/receipts").json()["items"] == []
    assert stranger.get(f"/receipts/{receipt['id']}").status_code == 404
    durban = api_for(make_user("dbn@example.com", Role.WAREHOUSE_OPERATOR, site_ids=[world["dbn"].pk]))
    assert durban.get(f"/receipts/{receipt['id']}").status_code == 404
    assert op.get(f"/receipts/{receipt['id']}").status_code == 200
    # Reading is all a client may do with a receipt.
    assert buyer.post(f"/receipts/{receipt['id']}/complete").status_code == 403
