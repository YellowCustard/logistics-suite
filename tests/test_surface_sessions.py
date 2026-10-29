"""The admin, the scanner and the client portal keep separate sign-ins in one browser."""

import pytest
from django.test import Client

from apps.platform.models import Role, User

from .conftest import make_user

pytestmark = pytest.mark.django_db

PASSWORD = "Korhaan-veld-2026"
SCANNER = {"HTTP_REFERER": "http://testserver/scanner/"}
PORTAL = {"HTTP_X_NURONTEQ_APP": "portal"}


@pytest.fixture
def people(make_client):
    sdb = make_client("SDB", "4111111111")
    boss = User.objects.create_superuser(email="boss@example.com", password=PASSWORD)
    operator = make_user("op@example.com", Role.WAREHOUSE_OPERATOR, all_sites=True, all_clients=True)
    client_user = make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[sdb.pk])
    for user in (operator, client_user):
        user.set_password(PASSWORD)
        user.save()
    return boss, operator, client_user


def _sign_in(browser: Client, email: str, app: str, **extra) -> int:
    body = {"email": email, "password": PASSWORD, "app": app}
    return browser.post("/api/v1/auth/session", body, content_type="application/json", **extra).status_code


def _who(browser: Client, **extra) -> str | None:
    response = browser.get("/api/v1/auth/session", **extra)
    return response.json()["email"] if response.status_code == 200 else None


def test_signing_in_to_the_scanner_or_portal_leaves_the_admin_signed_in(people):
    browser = Client()
    response = browser.post("/admin/login/?next=/admin/", {"username": "boss@example.com", "password": PASSWORD})
    assert response.status_code == 302 and "nq_admin_session" in browser.cookies

    assert _sign_in(browser, "op@example.com", "scanner", **SCANNER) == 200
    assert "nq_scanner_session" in browser.cookies
    assert _sign_in(browser, "buyer@sdb.example", "portal", **PORTAL) == 200

    assert browser.get("/admin/").status_code == 200  # still the superuser
    assert browser.get("/api/v1/docs").status_code == 200  # the API docs follow the admin sign-in
    assert _who(browser, **SCANNER) == "op@example.com"
    assert _who(browser, **PORTAL) == "buyer@sdb.example"
    # A download link has no custom header; the portal page it was clicked on picks the session.
    assert _who(browser, HTTP_REFERER="http://testserver/portal/shipments/1") == "buyer@sdb.example"
    assert _who(browser) is None  # nothing says which app: no session

    browser.delete("/api/v1/auth/session", **SCANNER)  # signing out of the scanner ends only that sign-in
    assert _who(browser, **SCANNER) is None
    assert _who(browser, **PORTAL) == "buyer@sdb.example"
    assert browser.get("/admin/").status_code == 200


def test_another_sites_referer_picks_no_session(people):
    browser = Client()
    assert _sign_in(browser, "op@example.com", "scanner", **SCANNER) == 200
    assert _who(browser, HTTP_REFERER="https://evil.example/scanner/") is None
