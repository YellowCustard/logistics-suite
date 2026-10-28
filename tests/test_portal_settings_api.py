"""The API behind the client portal's Settings: portal users with one-time setup links, and API credentials."""

from urllib.parse import parse_qs

import pytest
from django.test import Client

from apps.platform.models import ApiClient, AuditEntry, Role

from .conftest import Api, api_for, make_user

pytestmark = pytest.mark.django_db

PASSWORD = "Korhaan-veld-2026"


@pytest.fixture
def clients(make_client):
    return make_client("SDB", "4111111111"), make_client("OTHER", "4222222222")


def _boss(client_party):
    return make_user(f"boss@{client_party.code.lower()}.example", Role.CLIENT_ADMIN, client_ids=[client_party.pk])


def _setup(link: str, password: str = PASSWORD, browser: Client | None = None):
    path, fragment = link.split("#")
    assert path == "/portal/welcome"
    params = {k: v[0] for k, v in parse_qs(fragment).items()}
    return Api(browser or Client()).post(
        "/auth/password", {"uid": params["u"], "token": params["t"], "password": password}
    )


def _sign_in(email: str, password: str = PASSWORD) -> int:
    return (
        Client()
        .post(
            "/api/v1/auth/session",
            {"email": email, "password": password, "app": "portal"},
            content_type="application/json",
        )
        .status_code
    )


def test_client_admin_invites_a_user_who_sets_a_password_once(clients):
    sdb, other = clients
    boss = _boss(sdb)
    admin = api_for(boss)

    invited = admin.post(
        "/client/users", {"email": " Lerato@SDB.example ", "full_name": "Lerato M", "role": "client_user"}
    )
    assert invited.status_code == 201, invited.content
    body = invited.json()
    assert (body["email"], body["role"], body["pending"]) == ("lerato@sdb.example", "client_user", True)
    assert _sign_in("lerato@sdb.example") == 401  # no password until the link is used

    weak = _setup(body["setup_link"], "password")
    assert weak.status_code == 422 and "too common" in weak.json()["detail"]
    assert _setup(body["setup_link"]).status_code == 204
    assert _setup(body["setup_link"], "Another-long-pass-1").status_code == 400  # one use only
    assert _sign_in("lerato@sdb.example") == 200

    listed = {u["email"]: u for u in admin.get("/client/users").json()}
    assert set(listed) == {"boss@sdb.example", "lerato@sdb.example"}
    assert listed["lerato@sdb.example"]["pending"] is False

    # The same address again, or one of another client or of staff, is refused with one message.
    make_user("ops@nuronteq.example", Role.WAREHOUSE_SUPERVISOR, all_sites=True)
    for email in ("LERATO@sdb.example", "boss@other.example", "ops@nuronteq.example"):
        if email == "boss@other.example":
            _boss(other)
        refused = admin.post("/client/users", {"email": email, "role": "client_admin"})
        assert refused.status_code == 409 and refused.json()["title"] == "Email in use"
    assert admin.post("/client/users", {"email": "not-an-email"}).status_code == 422
    assert admin.post("/client/users", {"email": "x@sdb.example", "role": "system_admin"}).status_code == 422


def test_roles_deactivation_and_password_resets(clients):
    sdb, other = clients
    boss = _boss(sdb)
    admin = api_for(boss)
    created = admin.post("/client/users", {"email": "thabo@sdb.example", "role": "client_user"}).json()
    _setup(created["setup_link"])
    thabo = Client()
    assert (
        thabo.post(
            "/api/v1/auth/session",
            {"email": "thabo@sdb.example", "password": PASSWORD, "app": "portal"},
            content_type="application/json",
        ).status_code
        == 200
    )
    assert Api(thabo).get("/client/users").status_code == 403  # not an admin

    promoted = admin.patch(f"/client/users/{created['id']}", {"role": "client_admin", "full_name": "Thabo N"})
    assert promoted.status_code == 200 and promoted.json()["role"] == "client_admin"
    assert Api(thabo).get("/client/users").status_code == 200  # the role applies on the next request

    # A reset ends the password and the open session; the new link sets a new one.
    link = admin.post(f"/client/users/{created['id']}/setup-link").json()["setup_link"]
    assert Api(thabo).get("/auth/session").status_code == 401
    assert _sign_in("thabo@sdb.example") == 401
    assert AuditEntry.objects.filter(action="password_reset", entity_id=created["id"]).exists()
    assert _setup(link, "Brand-new-pass-2026").status_code == 204
    assert _sign_in("thabo@sdb.example", "Brand-new-pass-2026") == 200

    deactivated = admin.patch(f"/client/users/{created['id']}", {"is_active": False})
    assert deactivated.json()["is_active"] is False
    assert _sign_in("thabo@sdb.example", "Brand-new-pass-2026") == 401
    assert admin.post(f"/client/users/{created['id']}/setup-link").status_code == 409

    # Admins can't demote, deactivate or reset themselves, so a client always keeps one admin.
    assert admin.patch(f"/client/users/{boss.pk}", {"role": "client_user"}).status_code == 409
    assert admin.patch(f"/client/users/{boss.pk}", {"is_active": False}).status_code == 409
    assert admin.post(f"/client/users/{boss.pk}/setup-link").status_code == 409

    # Other clients' users and staff are out of reach.
    stranger = _boss(other)
    staff = make_user("ops@nuronteq.example", Role.SITE_MANAGER, all_sites=True, all_clients=True)
    for target in (stranger, staff):
        assert admin.patch(f"/client/users/{target.pk}", {"is_active": False}).status_code == 404
        assert admin.post(f"/client/users/{target.pk}/setup-link").status_code == 404
    assert api_for(staff).get("/client/users").status_code == 403
    # Setup links only ever set portal users' passwords.
    assert _setup("/portal/welcome#u=bm9wZQ&t=x").status_code == 400


def test_api_credentials_create_rotate_revoke(clients, bearer_api):
    sdb, other = clients
    admin = api_for(_boss(sdb))

    created = admin.post("/client/api-credentials", {"name": "SDB ERP"})
    assert created.status_code == 201
    cred = created.json()
    assert cred["key_id"].startswith("lk_") and cred["secret"]

    def token(secret):
        return Client().post(
            "/api/v1/auth/token",
            {"grant_type": "client_credentials", "client_id": cred["key_id"], "client_secret": secret},
        )

    first = token(cred["secret"])
    assert first.status_code == 200
    system = Api(Client(), {"Authorization": f"Bearer {first.json()['access_token']}"})
    assert system.get("/orders").status_code == 200
    assert system.get("/client/api-credentials").status_code == 403  # systems can't mint credentials

    rotated = admin.post(f"/client/api-credentials/{cred['id']}/rotate").json()
    assert rotated["key_id"] == cred["key_id"] and rotated["secret"] != cred["secret"]
    assert token(cred["secret"]).status_code == 401
    assert token(rotated["secret"]).status_code == 200
    assert system.get("/orders").status_code == 200  # issued tokens live until they expire

    listed = admin.get("/client/api-credentials").json()
    assert [c["key_id"] for c in listed] == [cred["key_id"]] and "secret" not in listed[0]

    revoked = admin.delete(f"/client/api-credentials/{cred['id']}")
    assert revoked.json()["is_active"] is False
    assert system.get("/orders").status_code == 401
    assert token(rotated["secret"]).status_code == 401
    assert admin.post(f"/client/api-credentials/{cred['id']}/rotate").status_code == 409

    theirs = ApiClient.objects.create(name="Other", client_id=other.pk, key_id="lk_other", secret_hash="x")
    assert admin.delete(f"/client/api-credentials/{theirs.pk}").status_code == 404
    buyer = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[sdb.pk]))
    assert buyer.get("/client/api-credentials").status_code == 403
    assert buyer.post("/client/api-credentials", {"name": "x"}).status_code == 403
