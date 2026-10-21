import datetime as dt

import pytest
from django.db import DatabaseError, connection, transaction

from apps.masterdata.models import Person, Site
from apps.platform import audit
from apps.platform.ids import uuid7, uuid7_timestamp_ms
from apps.platform.models import AuditEntry
from apps.platform.numbers import next_number


def test_uuid7_is_version_7_and_strictly_increasing():
    ids = [uuid7() for _ in range(5000)]
    assert all(u.version == 7 for u in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    now_ms = dt.datetime.now(dt.UTC).timestamp() * 1000
    assert abs(uuid7_timestamp_ms(ids[-1]) - now_ms) < 5000


@pytest.mark.django_db
def test_business_numbers_are_sequential_per_name():
    year = dt.date.today().year
    assert next_number("invoice", "INV") == f"INV-{year}-000001"
    assert next_number("invoice", "INV") == f"INV-{year}-000002"
    assert next_number("grn", "GRN") == f"GRN-{year}-000001"


@pytest.mark.django_db
def test_audit_records_create_update_delete_with_changed_fields():
    site = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    site.name = "Teakwood DC"
    site.save()
    site.save()  # no change: no entry
    site_id = str(site.pk)
    site.delete()

    entries = list(AuditEntry.objects.filter(entity_type="masterdata.site", entity_id=site_id).order_by("id"))
    assert [e.action for e in entries] == ["create", "update", "delete"]
    assert entries[1].changed_fields == ["name"]  # timestamps are excluded from audit snapshots
    assert entries[1].before["name"] == "Teakwood" and entries[1].after["name"] == "Teakwood DC"
    assert entries[2].after is None


@pytest.mark.django_db
def test_personal_fields_are_encrypted_at_rest_and_masked_in_audit():
    person = Person.objects.create(first_name="Thabo", last_name="M", id_number="9001015800085", phone="0821234567")
    with connection.cursor() as cursor:
        cursor.execute("SELECT id_number, phone FROM masterdata_person WHERE id = %s", [person.pk])
        raw_id, raw_phone = cursor.fetchone()
    assert "9001015800085" not in raw_id and "0821234567" not in raw_phone
    assert Person.objects.get(pk=person.pk).id_number == "9001015800085"

    entry = AuditEntry.objects.get(entity_type="masterdata.person", entity_id=str(person.pk))
    assert entry.after["id_number"] == audit.MASK and entry.after["phone"] == audit.MASK


@pytest.mark.django_db
def test_audit_log_is_append_only_in_the_database():
    entry = audit.record(action="test", entity_type="x", entity_id="1")
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditEntry.objects.filter(pk=entry.pk).update(action="tampered")
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditEntry.objects.filter(pk=entry.pk).delete()
    assert AuditEntry.objects.get(pk=entry.pk).action == "test"


def test_health(client):
    assert client.get("/api/v1/healthz").json() == {"status": "ok"}


@pytest.mark.django_db
def test_ready_reports_outbox_lag(client):
    body = client.get("/api/v1/readyz").json()
    assert body["database"] == "up" and body["outbox_lag_seconds"] == 0.0
