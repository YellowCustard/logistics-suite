import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.masterdata.models import Item, ItemBarcode, ItemUom, Person
from apps.platform.models import Document

pytestmark = pytest.mark.django_db


@pytest.fixture
def sdb(make_client):
    return make_client()


def test_item_import_creates_items_uoms_barcodes_and_keeps_the_file(admin_api, sdb):
    csv_text = (
        "sku,description,base_uom,case_uom,case_factor,barcode\n"
        "SDB-001,Sparkling water 500ml,ea,cs,24,6001234000013\n"
        "SDB-002,Still water 1.5l,EA,,,\n"
    )
    response = admin_api.upload(f"/items:import?client_id={sdb.pk}", csv_text, name="items.csv")
    assert response.status_code == 201, response.content
    assert response.json()["created"] == 2
    assert set(ItemUom.objects.filter(item__sku="SDB-001").values_list("uom", "factor")) == {("EA", 1), ("CS", 24)}
    assert ItemBarcode.objects.get(barcode="6001234000013").client_id == sdb.pk
    doc = Document.objects.get(pk=response.json()["document_id"])
    assert doc.kind == "import_file" and doc.client_id == sdb.pk and doc.original_name == "items.csv"


def test_import_is_all_or_nothing_with_row_numbers(admin_api, sdb):
    Item.objects.create(client=sdb, sku="SDB-002", description="Existing")
    csv_text = "sku,description\nSDB-001,Fine\nSDB-002,Duplicate of existing\n"
    response = admin_api.upload(f"/items:import?client_id={sdb.pk}", csv_text)
    assert response.status_code == 422
    errors = response.json()["errors"]
    assert errors == [{"row": 3, "field": "(row)", "message": "duplicate: a record with this key already exists"}]
    assert not Item.objects.filter(sku="SDB-001").exists()
    assert not Document.objects.exists()


def test_invalid_rows_are_reported_before_anything_is_written(admin_api, sdb):
    csv_text = "sku,description,weight_kg,case_uom\nA,,1.5,\nB,Ok,-1,CS\n"
    response = admin_api.upload(f"/items:import?client_id={sdb.pk}", csv_text)
    assert response.status_code == 422
    assert {(e["row"], e["field"]) for e in response.json()["errors"]} == {(2, "description"), (3, "weight_kg")}
    assert not Item.objects.exists()


def test_header_problems_are_reported(admin_api, sdb):
    response = admin_api.upload(f"/items:import?client_id={sdb.pk}", "sku,colour\nA,red\n")
    assert response.status_code == 422
    assert {(e["field"], e["message"]) for e in response.json()["errors"]} == {
        ("description", "required column missing"),
        ("colour", "unknown column"),
    }


def test_non_utf8_and_empty_files_are_rejected(admin_api, sdb):
    bad = admin_api.client.post(
        f"/api/v1/items:import?client_id={sdb.pk}",
        {"file": _file(b"sku,description\n\xff\xfe,x\n")},
        headers={"Idempotency-Key": "enc"},
    )
    assert bad.status_code == 422
    assert admin_api.upload(f"/items:import?client_id={sdb.pk}", "sku,description\n").status_code == 422


def test_driver_import_never_exposes_personal_fields(admin_api):
    csv_text = "first_name,last_name,employee_code,id_number,phone\nThabo,Mokoena,D001,9001015800085,0821234567\n"
    assert admin_api.upload("/drivers:import", csv_text).status_code == 201
    body = admin_api.get("/drivers").json()["items"][0]
    assert "id_number" not in body and "phone" not in body
    assert Person.objects.get(employee_code="D001").id_number == "9001015800085"


def _file(content: bytes):
    return SimpleUploadedFile("bad.csv", content, content_type="text/csv")
