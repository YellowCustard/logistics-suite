"""M0 exit criterion: a client, a site, its bins, the client's items and a published rate card are set
up entirely through CSV imports and the API, and every step is visible in the audit log."""

import pytest

from apps.masterdata.models import Item, Party, RateCard, Site
from apps.platform.models import AuditEntry, Document, OutboxEvent
from apps.wms.models import Location

pytestmark = pytest.mark.django_db

CLIENTS = (
    "code,legal_name,vat_number,sage_customer_code,billing_email\n"
    "SDB,SDB Beverages (Pty) Ltd,4123456789,SDB001,accounts@sdb.example\n"
)
SITES = (
    "code,name,kind,address,latitude,longitude,pallet_capacity\nTEAK,Teakwood,warehouse,Teakwood Rd,-26.1,28.2,4000\n"
)
BINS = (
    "code,zone,kind,pallet_capacity,pick_sequence\n"
    "A-01-01-A,a,storage,1,10\n"
    "A-01-01-B,a,storage,1,20\n"
    "DOCK-1,,dock,0,0\n"
)
ITEMS = "sku,description,base_uom,case_uom,case_factor,units_per_pallet\nSDB-001,Sparkling 500ml,EA,CS,24,1440\n"
LINES = (
    "charge_type,uom,unit_price,site_code\n"
    "STORAGE_PALLET_DAY,PALLET_DAY,12.5000,\n"
    "HANDLING_IN_PALLET,PALLET,85.0000,TEAK\n"
    "PICK_CARTON,CARTON,3.2000,\n"
)


def test_onboard_pilot_client_through_csv_and_api(admin, admin_api, bearer_api):
    def ok(response, status=201):
        assert response.status_code == status, response.content
        return response.json()

    ok(admin_api.upload("/clients:import", CLIENTS, name="clients.csv"))
    sdb = Party.objects.get(code="SDB")
    ok(admin_api.upload("/sites:import", SITES, name="sites.csv"))
    teak = Site.objects.get(code="TEAK")
    ok(admin_api.upload(f"/sites/{teak.pk}/locations:import", BINS, name="bins.csv"))
    ok(admin_api.upload(f"/items:import?client_id={sdb.pk}", ITEMS, name="items.csv"))
    contract = ok(
        admin_api.post("/contracts", {"client_id": str(sdb.pk), "reference": "SDB-WH-2026", "valid_from": "2026-11-01"})
    )
    card = ok(admin_api.post(f"/contracts/{contract['id']}/rate-cards", {"valid_from": "2026-11-01"}))
    ok(admin_api.upload(f"/rate-cards/{card['id']}/lines:import", LINES, name="rates.csv"))
    published = ok(admin_api.post(f"/rate-cards/{card['id']}/publish"), status=200)

    assert published["status"] == "published" and len(published["lines"]) == 3
    assert Location.objects.filter(site=teak).count() == 3
    assert Item.objects.get(client=sdb, sku="SDB-001").uoms.count() == 2
    assert Document.objects.filter(kind="import_file").count() == 5

    # Every record is in the audit log, attributed to the admin and tied to a request.
    card_id = card["id"]
    expected = {
        ("masterdata.party", str(sdb.pk), "create"),
        ("masterdata.site", str(teak.pk), "create"),
        ("masterdata.item", str(Item.objects.get(sku="SDB-001").pk), "create"),
        ("masterdata.contract", contract["id"], "create"),
        ("masterdata.ratecard", card_id, "create"),
        ("masterdata.ratecard", card_id, "rate_card.publish"),
    }
    expected |= {("wms.location", str(pk), "create") for pk in Location.objects.values_list("pk", flat=True)}
    expected |= {
        ("masterdata.rateline", str(pk), "create")
        for pk in RateCard.objects.get(pk=card_id).lines.values_list("pk", flat=True)
    }
    entries = AuditEntry.objects.filter(actor_id=admin.pk)
    assert expected <= set(entries.values_list("entity_type", "entity_id", "action"))
    assert not entries.filter(request_id__isnull=True).exists()

    assert set(OutboxEvent.objects.values_list("type", flat=True)) == {
        "masterdata.client.created",
        "masterdata.rate_card.published",
    }

    # The client's own system can now read its catalogue and its published rates, and nothing else.
    client_api = bearer_api(sdb)
    assert [i["sku"] for i in ok(client_api.get("/items"), 200)["items"]] == ["SDB-001"]
    assert ok(client_api.get(f"/rate-cards/{card_id}"), 200)["number"] == 1
    assert client_api.get("/sites").status_code == 403
