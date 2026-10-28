"""The client portal page: served with a strict CSP, client users sign in to it, staff don't, and its shipment list
comes newest first. The data itself is the tracking API (tests/test_tracking.py)."""

import json

import pytest
from django.test import Client

from apps.platform.models import Role
from apps.portal import views

from .conftest import api_for, make_user
from .test_delivery import evidence, pod, road  # noqa: F401
from .test_sync import user_with_password
from .test_trips import new_trip, order, pick_and_pack, put, stops, yard  # noqa: F401

pytestmark = pytest.mark.django_db

BUILT = {"js": "portal/assets/index-abc123.js", "css": ["portal/assets/index-abc123.css"]}


@pytest.fixture(autouse=True)
def built_portal(monkeypatch):
    """CI's Python job doesn't build the React app; the page only needs the manifest's entry."""
    monkeypatch.setattr(views, "_read_entry", lambda: BUILT)
    monkeypatch.setattr(views, "_cached_entry", lambda: BUILT)


def _portal():
    browser = Client(enforce_csrf_checks=True)
    page = browser.get("/portal/")
    assert page.status_code == 200
    return browser, page


def _sign_in(browser, email, password, app="portal"):
    return browser.post(
        "/api/v1/auth/session",
        {"email": email, "password": password, "app": app},
        content_type="application/json",
        headers={"X-CSRFToken": browser.cookies["csrftoken"].value},
    )


def test_portal_page_is_locked_down():
    _, page = _portal()
    csp = page["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe" not in csp
    assert b'<script type="module" src="/static/portal/assets/index-abc123.js">' in page.content
    assert b'href="/static/portal/assets/index-abc123.css"' in page.content
    assert b'data-csrf-cookie="csrftoken"' in page.content and "csrftoken" in page.cookies
    assert page["Cache-Control"] == "no-cache"


def test_client_side_routes_serve_the_same_page():
    browser, _ = _portal()
    deep = browser.get("/portal/shipments/3f2a/")
    assert deep.status_code == 200 and b"index-abc123.js" in deep.content
    token = browser.cookies["csrftoken"].value
    assert browser.post("/portal/", headers={"X-CSRFToken": token}).status_code == 405


def test_unbuilt_portal_says_so(monkeypatch):
    monkeypatch.setattr(views, "_read_entry", lambda: None)
    monkeypatch.setattr(views, "_cached_entry", lambda: None)
    page = Client().get("/portal/")
    assert page.status_code == 503 and b"hasn't been built" in page.content and b"<script" not in page.content


def test_entry_comes_from_the_vite_manifest(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"index.html": {"file": "assets/index-x.js", "css": ["assets/index-x.css"]}}))
    monkeypatch.undo()  # drop the autouse stub so the real reader runs
    monkeypatch.setattr(views.finders, "find", lambda path: str(manifest))
    assert views._read_entry() == {"js": "portal/assets/index-x.js", "css": ["portal/assets/index-x.css"]}
    manifest.write_text("{}")
    assert views._read_entry() is None


def test_client_users_sign_in_to_the_portal_and_staff_do_not(make_client):
    sdb = make_client("SDB", "4111111111")
    user_with_password("buyer@sdb.example", "pw-123456789", role=Role.CLIENT_USER, client_ids=[sdb.pk])
    user_with_password("op@example.com", "pw-123456789")

    browser, _ = _portal()
    signed_in = _sign_in(browser, "buyer@sdb.example", "pw-123456789")
    assert signed_in.status_code == 200 and signed_in.json()["roles"] == ["client_user"]
    assert browser.get("/api/v1/auth/session").status_code == 200

    staff, _ = _portal()
    refused = _sign_in(staff, "op@example.com", "pw-123456789")
    assert refused.status_code == 403 and "portal is for client users" in refused.json()["detail"]
    assert staff.get("/api/v1/auth/session").status_code == 401  # not left signed in

    assert _sign_in(_portal()[0], "buyer@sdb.example", "pw-123456789", app="scanner").status_code == 403
    assert _sign_in(_portal()[0], "buyer@sdb.example", "pw-123456789", app="other").status_code == 422


def test_shipments_newest_first_with_paging(road):  # noqa: F811
    buyer = api_for(make_user("buyer@sdb.example", Role.CLIENT_USER, client_ids=[road["sdb"].pk]))
    oldest_first = [s["id"] for s in buyer.get("/shipments").json()["items"]]
    assert len(oldest_first) == 2
    first = buyer.get("/shipments", newest_first="true", limit=1).json()
    second = buyer.get("/shipments", newest_first="true", limit=1, cursor=first["next_cursor"]).json()
    assert [first["items"][0]["id"], second["items"][0]["id"]] == oldest_first[::-1]
    assert second["next_cursor"] is None
