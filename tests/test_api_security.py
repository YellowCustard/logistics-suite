import pytest
from django.test import Client

from apps.masterdata.models import Item, Party, Site
from apps.platform.auth import create_api_client
from apps.platform.models import ApiClient, IdempotencyRecord, Role

from .conftest import api_for, make_user

pytestmark = pytest.mark.django_db

CLIENT_BODY = {"code": "ACME", "legal_name": "Acme (Pty) Ltd", "vat_number": "4000000001"}


# --- Idempotency ----------------------------------------------------------------------------------------


def test_mutating_request_without_key_is_rejected(admin):
    client = Client()
    client.force_login(admin)
    response = client.post("/api/v1/clients", CLIENT_BODY, content_type="application/json")
    assert response.status_code == 400
    assert response["Content-Type"] == "application/problem+json"
    assert not Party.objects.exists()


def test_same_key_and_body_replays_without_a_second_write(admin_api):
    first = admin_api.post("/clients", CLIENT_BODY, headers={"Idempotency-Key": "k-1"})
    second = admin_api.post("/clients", CLIENT_BODY, headers={"Idempotency-Key": "k-1"})
    assert first.status_code == second.status_code == 201
    assert second["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    assert Party.objects.count() == 1


def test_same_key_with_different_body_is_refused(admin_api):
    admin_api.post("/clients", CLIENT_BODY, headers={"Idempotency-Key": "k-2"})
    response = admin_api.post("/clients", {**CLIENT_BODY, "code": "OTHER"}, headers={"Idempotency-Key": "k-2"})
    assert response.status_code == 422
    assert Party.objects.count() == 1


def test_error_responses_are_not_stored_so_a_corrected_retry_succeeds(admin_api):
    bad = admin_api.post("/clients", {**CLIENT_BODY, "vat_number": "123"}, headers={"Idempotency-Key": "k-3"})
    assert bad.status_code == 422
    assert not IdempotencyRecord.objects.filter(key="k-3").exists()
    good = admin_api.post("/clients", CLIENT_BODY, headers={"Idempotency-Key": "k-3"})
    assert good.status_code == 201


def test_keys_are_scoped_per_principal(admin_api, db):
    other = api_for(make_user("other-admin@example.com", Role.SYSTEM_ADMIN, all_clients=True, all_sites=True))
    assert admin_api.post("/clients", CLIENT_BODY, headers={"Idempotency-Key": "shared"}).status_code == 201
    response = other.post("/clients", {**CLIENT_BODY, "code": "ACME2"}, headers={"Idempotency-Key": "shared"})
    assert response.status_code == 201


def test_unauthenticated_request_gets_401_not_400():
    response = Client().post("/api/v1/clients", CLIENT_BODY, content_type="application/json")
    assert response.status_code == 401


# --- OAuth2 client credentials ------------------------------------------------------------------------------


@pytest.fixture
def credentials(make_client):
    api_client, secret = create_api_client(name="SDB ERP", client_id=make_client().pk)
    return api_client, secret


def _token(client_id: str, secret: str, grant_type: str = "client_credentials"):
    return Client().post(
        "/api/v1/auth/token", {"grant_type": grant_type, "client_id": client_id, "client_secret": secret}
    )


def test_token_issued_for_valid_credentials_only(credentials):
    api_client, secret = credentials
    ok = _token(api_client.key_id, secret)
    assert ok.status_code == 200 and ok.json()["token_type"] == "Bearer"
    assert _token(api_client.key_id, "wrong").status_code == 401
    assert _token("lk_unknown", secret).status_code == 401
    assert _token(api_client.key_id, secret, grant_type="password").status_code == 400
    assert ApiClient.objects.get(pk=api_client.pk).secret_hash != secret


def _get_with(token: str):
    return Client().get("/api/v1/items", headers={"Authorization": f"Bearer {token}"})


def test_tampered_expired_and_revoked_tokens_are_rejected(credentials, settings):
    api_client, secret = credentials
    token = _token(api_client.key_id, secret).json()["access_token"]
    assert _get_with(token).status_code == 200
    assert _get_with(token[:-2] + "xx").status_code == 401

    settings.API_TOKEN_TTL_SECONDS = -1
    assert _get_with(token).status_code == 401
    settings.API_TOKEN_TTL_SECONDS = 3600

    ApiClient.objects.filter(pk=api_client.pk).update(is_active=False)
    assert _get_with(token).status_code == 401


# --- Tenant isolation -------------------------------------------------------------------------------------------


@pytest.fixture
def two_clients(make_client):
    a, b = make_client("SDB", "4111111111"), make_client("OTHER", "4222222222")
    for party in (a, b):
        Item.objects.create(client=party, sku=f"{party.code}-1", description="Widget")
    return a, b


def test_client_system_sees_only_its_own_client(two_clients, bearer_api):
    a, b = two_clients
    api = bearer_api(a)
    assert [i["sku"] for i in api.get("/items").json()["items"]] == ["SDB-1"]
    assert api.get(f"/clients/{a.pk}").status_code == 200
    assert api.get(f"/clients/{b.pk}").status_code == 404
    other_item = Item.objects.get(client=b)
    assert api.get(f"/items/{other_item.pk}").status_code == 404


def test_client_system_writes_land_on_its_own_client(two_clients, bearer_api):
    a, b = two_clients
    response = bearer_api(a).post(f"/items?client_id={b.pk}", {"sku": "NEW", "description": "New item"})
    assert response.status_code == 201
    assert response.json()["client_id"] == str(a.pk)


def test_client_user_is_pinned_to_one_client_and_kept_off_staff_endpoints(two_clients):
    a, _ = two_clients
    api = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[a.pk]))
    assert [i["sku"] for i in api.get("/items").json()["items"]] == ["SDB-1"]
    assert api.get("/sites").json()["items"] == []  # no warehouses or addresses in this fixture
    assert api.get("/exceptions").status_code == 403


def test_site_scoped_staff_cannot_reach_other_sites(db):
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    other = Site.objects.create(code="DBN", name="Durban", kind="warehouse")
    api = api_for(make_user("manager@example.com", Role.SITE_MANAGER, site_ids=[teak.pk]))
    assert api.get(f"/sites/{teak.pk}/locations").status_code == 200
    assert api.get(f"/sites/{other.pk}/locations").status_code == 404
    assert [s["code"] for s in api.get("/sites").json()["items"]] == ["TEAK"]
    response = api.upload(f"/sites/{other.pk}/locations:import", "code\nA-01\n")
    assert response.status_code == 404


def test_roles_are_enforced(two_clients):
    api = api_for(make_user("operator@example.com", Role.WAREHOUSE_OPERATOR, all_sites=True, all_clients=True))
    assert api.post("/clients", CLIENT_BODY).status_code == 403
    assert api.get("/drivers").status_code == 403


def test_api_docs_are_staff_only(admin):
    assert Client().get("/api/v1/openapi.json").status_code == 302
    staff = Client()
    admin.is_staff = True
    admin.save()
    staff.force_login(admin)
    assert staff.get("/api/v1/openapi.json").status_code == 200
