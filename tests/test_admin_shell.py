"""Nuronteq admin shell: KPI registry, permission gating, the palette data and the stock-admin fixes."""

import json
import logging
import re
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.contrib import admin as django_admin
from django.contrib.auth.models import Group, Permission
from django.test import Client, RequestFactory

from apps.platform import admin_dashboard
from apps.platform.admin_dashboard import Metric
from apps.platform.models import (
    AuditEntry,
    DeliveryStatus,
    EventDelivery,
    ExceptionCase,
    ExceptionStatus,
    OutboxEvent,
    Severity,
    User,
)
from apps.platform.templatetags.nq_admin import ID_ROUTES

pytestmark = pytest.mark.django_db

KPI_LABELS = ["Active orders", "Open exceptions", "Shipments in transit", "Integration backlog"]


@pytest.fixture
def superuser() -> User:
    return User.objects.create_superuser(email="root@example.com", password="pw-not-used", full_name="Ada Lovelace")


@pytest.fixture
def su_client(superuser) -> Client:
    client = Client()
    client.force_login(superuser)
    return client


def staff_with(*codenames: str) -> Client:
    user = User.objects.create_user(email=f"staff-{len(codenames)}@example.com", password="pw-not-used", is_staff=True)
    user.user_permissions.set(Permission.objects.filter(codename__in=codenames))
    client = Client()
    client.force_login(user)
    return client


def open_case(summary: str, severity: str = Severity.MEDIUM, status: str = ExceptionStatus.OPEN) -> ExceptionCase:
    resolution = "Fixed" if status == ExceptionStatus.RESOLVED else ""
    return ExceptionCase.objects.create(
        kind="test", summary=summary, severity=severity, status=status, resolution=resolution
    )


def kpi_values(html: str) -> dict[str, str]:
    return dict(re.findall(r'data-nq-kpi="([^"]+)">([^<]*)<', html))


def palette(html: str) -> dict:
    match = re.search(r'<script id="nq-palette-data" type="application/json">(.*?)</script>', html, re.S)
    assert match, "palette data missing"
    return json.loads(match.group(1))


def test_home_shows_live_kpis_in_order(su_client):
    open_case("Short pick", Severity.HIGH)
    open_case("Late ASN", status=ExceptionStatus.IN_PROGRESS)
    open_case("Done", status=ExceptionStatus.RESOLVED)

    html = su_client.get("/admin/").content.decode()

    values = kpi_values(html)
    assert list(values) == KPI_LABELS
    assert values["Open exceptions"] == "2"
    assert "1 high or critical" in html
    assert 'id="nq-sidebar"' in html and 'id="nq-palette"' in html
    assert ">AL<" in html  # avatar initials from full_name


def test_kpi_links_open_filtered_change_lists(su_client, superuser):
    open_case("Open one")
    open_case("Resolved one", status=ExceptionStatus.RESOLVED)
    request = RequestFactory().get("/admin/")
    request.user = superuser

    metrics = {m.label: m for m in admin_dashboard.kpis(request)}

    assert set(metrics) == set(KPI_LABELS)
    for metric in metrics.values():
        response = su_client.get(metric.href)
        # The admin answers a lookup it refuses with a redirect to ?e=1, so a 200 proves the filter applied.
        assert response.status_code == 200, metric.href
    exceptions = su_client.get(metrics["Open exceptions"].href)
    assert exceptions.context["cl"].result_count == 1


def test_kpis_and_palette_follow_permissions():
    open_case("Visible to the exception desk")
    client = staff_with("view_exceptioncase")

    html = client.get("/admin/").content.decode()

    assert list(kpi_values(html)) == ["Open exceptions"]
    keys = {item["key"] for item in palette(html)["items"]}
    assert keys == {"platform.exceptioncase"}
    assert "Recent activity" not in html  # needs view_auditentry


def test_failing_provider_is_logged_and_skipped(su_client, monkeypatch, caplog):
    def broken() -> list[Metric]:
        raise RuntimeError("boom")

    entries = [*admin_dashboard._kpis, admin_dashboard._Entry(15, "platform.view_exceptioncase", broken)]
    monkeypatch.setattr(admin_dashboard, "_kpis", sorted(entries, key=lambda e: e.order))

    with caplog.at_level(logging.ERROR, logger="apps.platform.admin_dashboard"):
        response = su_client.get("/admin/")

    assert response.status_code == 200
    assert list(kpi_values(response.content.decode())) == KPI_LABELS
    assert any("broken" in r.getMessage() for r in caplog.records)


def test_failed_query_does_not_poison_the_request(su_client, monkeypatch):
    def bad_sql() -> list[Metric]:
        AuditEntry.objects.raw("SELECT no_such_column FROM platform_auditentry")[0]
        return []

    monkeypatch.setattr(
        admin_dashboard,
        "_kpis",
        [admin_dashboard._Entry(0, "platform.view_auditentry", bad_sql), *admin_dashboard._kpis],
    )

    response = su_client.get("/admin/")

    assert response.status_code == 200  # later providers and templates still query after the savepoint rollback
    assert "Open exceptions" in kpi_values(response.content.decode())


def test_registration_is_idempotent():
    before = len(admin_dashboard._kpis)
    admin_dashboard.register()
    assert len(admin_dashboard._kpis) == before


def test_status_chips_show_only_non_zero_counts(su_client):
    html = su_client.get("/admin/").content.decode()
    assert "dead event" not in html

    event = OutboxEvent.objects.create(
        type="test.event", version=1, aggregate_type="test", aggregate_id="1", payload={}
    )
    EventDelivery.objects.create(
        event=event, consumer="test", status=DeliveryStatus.DEAD, next_attempt_at=event.occurred_at
    )

    html = su_client.get("/admin/").content.decode()
    assert "1 dead event delivery" in html
    assert kpi_values(html)["Integration backlog"] == "1"


def test_phone_add_buttons_only_for_addable_models(su_client):
    html = su_client.get("/admin/wms/").content.decode()

    adds = re.findall(r'<a href="([^"]+)" aria-label="[^"]*" data-nq-add-mobile', html)

    assert adds == ["/admin/wms/location/add/"]  # every other warehouse record changes only through services


def test_current_route_is_marked_in_sidebar_and_bottom_bar(su_client):
    html = su_client.get("/admin/wms/location/").content.decode()

    assert re.search(r'href="/admin/wms/location/" aria-current="page" data-current', html)
    assert re.search(r'href="/admin/wms/" aria-current="true" data-current', html)  # Warehouse tab


def test_id_routes_point_at_searchable_admin_lists(su_client):
    data = palette(su_client.get("/admin/").content.decode())

    assert data["ids"] == ID_ROUTES
    keys = {item["key"] for item in data["items"]}
    for prefix, target in ID_ROUTES.items():
        model = django_apps.get_model(target)
        model_admin = django_admin.site._registry[model]
        assert target in keys, prefix
        assert "number" in model_admin.search_fields, prefix


def test_change_form_and_popup(su_client):
    page = su_client.get("/admin/masterdata/site/add/")
    assert page.status_code == 200 and 'id="nq-sidebar"' in page.content.decode()

    popup = su_client.get("/admin/masterdata/site/?_popup=1&_to_field=id").content.decode()
    assert 'id="nq-sidebar"' not in popup and 'class="nq"' not in popup


def test_login_page_keeps_the_stock_header():
    html = Client().get("/admin/login/").content.decode()
    assert "brand/admin.css" in html and "nq-admin/django-layer.css" in html
    assert 'id="nq-sidebar"' not in html and 'id="nq-palette-data"' not in html


def test_stock_admin_fixes(su_client):
    assert django_admin.site.site_url == "/portal/"
    assert Group not in django_admin.site._registry
    html = su_client.get("/admin/").content.decode()
    for name in ["Audit entries", "Event deliveries", "Webhook deliveries", "Cycle counts", "Sync commands"]:
        assert f">{name}<" in html, name
    assert "entrys" not in html and "deliverys" not in html


def test_templates_never_use_the_bare_hidden_class():
    # Django's base.css has .hidden { display: none !important }; from its low cascade layer that !important beats
    # every Tailwind utility, so "hidden lg:flex" would stay hidden on desktop. Use max-lg:hidden instead.
    root = Path(__file__).resolve().parent.parent / "apps/platform/templates/admin"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.html")
        for cls in re.findall(r'class="([^"]*)"', path.read_text())
        if "hidden" in cls.split()
    ]
    assert offenders == []
