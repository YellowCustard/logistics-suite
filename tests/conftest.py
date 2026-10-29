import itertools
import uuid

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.masterdata import services
from apps.masterdata.models import PartyRoleType
from apps.platform.auth import create_api_client
from apps.platform.models import Role, RoleGrant, User
from apps.platform.sessions import COOKIES

_keys = itertools.count()


def admin_client(user: User) -> Client:
    """A browser signed in to the admin. force_login writes the default session cookie; the admin reads its own."""
    client = Client()
    client.force_login(user)
    client.cookies[COOKIES["admin"]] = client.cookies[settings.SESSION_COOKIE_NAME].value
    return client


class Api:
    """Test client for /api/v1 that sends an Idempotency-Key with every mutating request."""

    def __init__(self, client: Client, headers: dict[str, str] | None = None):
        self.client = client
        self.headers = headers or {}

    def _headers(self, method: str, extra: dict[str, str] | None) -> dict[str, str]:
        headers = dict(self.headers)
        if method != "GET":
            headers.setdefault("Idempotency-Key", f"test-{next(_keys)}-{uuid.uuid4()}")
        headers.update(extra or {})
        return headers

    def get(self, path: str, **params):
        return self.client.get(f"/api/v1{path}", params, headers=self._headers("GET", None))

    def post(self, path: str, json=None, headers=None):
        return self.client.post(
            f"/api/v1{path}", json or {}, content_type="application/json", headers=self._headers("POST", headers)
        )

    def patch(self, path: str, json, headers=None):
        return self.client.patch(
            f"/api/v1{path}", json, content_type="application/json", headers=self._headers("PATCH", headers)
        )

    def delete(self, path: str, headers=None):
        return self.client.delete(f"/api/v1{path}", headers=self._headers("DELETE", headers))

    def upload(self, path: str, csv_text: str, name: str = "data.csv", headers=None):
        file = SimpleUploadedFile(name, csv_text.encode(), content_type="text/csv")
        return self.client.post(f"/api/v1{path}", {"file": file}, headers=self._headers("POST", headers))


def make_user(email: str, role: str, **grant) -> User:
    user = User.objects.create_user(email=email, password="pw-not-used")
    RoleGrant.objects.create(user=user, role=role, **grant)
    return user


def api_for(user: User) -> Api:
    client = Client()
    client.force_login(user)
    return Api(client)


@pytest.fixture
def admin(db) -> User:
    return make_user("admin@example.com", Role.SYSTEM_ADMIN, all_clients=True, all_sites=True)


@pytest.fixture
def admin_api(admin) -> Api:
    return api_for(admin)


@pytest.fixture
def make_client(db):
    def factory(code: str = "SDB", vat: str = "4123456789"):
        return services.create_party(
            code=code, legal_name=f"{code} (Pty) Ltd", roles=[PartyRoleType.CLIENT], vat_number=vat
        )

    return factory


@pytest.fixture
def bearer_api(db):
    """An Api authenticated as a client system via OAuth2 client credentials."""

    def factory(client_party) -> Api:
        api_client, secret = create_api_client(name=f"{client_party.code} ERP", client_id=client_party.pk)
        response = Client().post(
            "/api/v1/auth/token",
            {"grant_type": "client_credentials", "client_id": api_client.key_id, "client_secret": secret},
        )
        assert response.status_code == 200, response.content
        return Api(Client(), {"Authorization": f"Bearer {response.json()['access_token']}"})

    return factory
