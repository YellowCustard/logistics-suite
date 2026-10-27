"""Outbound webhooks: subscriptions, signed delivery, retries, dead letters, and SSRF protection."""

import json
import socket
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from django.db import transaction
from django.utils import timezone

from apps.platform import outbox, webhooks
from apps.platform.apiutils import Problem
from apps.platform.models import ExceptionCase, Role, WebhookDelivery, WebhookSubscription

from .conftest import api_for, make_user

pytestmark = pytest.mark.django_db


# --- Signing and URL safety --------------------------------------------------------------------------------------


def test_signature_round_trip():
    body = b'{"id":"x"}'
    header = webhooks.sign("whsec_test", 1_800_000_000, body)
    assert header.startswith("t=1800000000,v1=")
    assert webhooks.verify("whsec_test", header, body, now=1_800_000_100)
    assert not webhooks.verify("whsec_test", header, body + b" ", now=1_800_000_100)  # tampered body
    assert not webhooks.verify("whsec_other", header, body, now=1_800_000_100)  # wrong secret
    assert not webhooks.verify("whsec_test", header, body, now=1_800_000_301 + 100)  # too old
    assert not webhooks.verify("whsec_test", "garbage", body)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",
        "https://localhost/hook",
        "https://intranet.local/hook",
        "https://10.1.2.3/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/hook",
        "https://user:pw@example.com/hook",
        "ftp://example.com/hook",
        "https://example.com:99999/hook",
    ],
)
def test_unsafe_urls_are_refused(url):
    with pytest.raises(Problem) as excinfo:
        webhooks.check_url(url)
    assert excinfo.value.status == 422


def test_public_https_url_is_accepted():
    assert webhooks.check_url(" https://erp.example.co.za/hooks/nuronteq?x=1 ") == (
        "https://erp.example.co.za/hooks/nuronteq?x=1"
    )


def _fake_dns(monkeypatch, *addresses):
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", getaddrinfo)


def test_resolution_must_be_entirely_public(monkeypatch):
    _fake_dns(monkeypatch, "93.184.216.34")
    assert webhooks.resolve("erp.example.com", 443) == "93.184.216.34"
    _fake_dns(monkeypatch, "93.184.216.34", "10.0.0.5")  # one private answer poisons the lot
    with pytest.raises(webhooks.UnsafeDestination):
        webhooks.resolve("erp.example.com", 443)
    _fake_dns(monkeypatch, "127.0.0.1")
    with pytest.raises(webhooks.UnsafeDestination):
        webhooks.resolve("rebind.example.com", 443)


def test_ipv4_mapped_ipv6_is_checked_as_ipv4(settings):
    import ipaddress

    assert not webhooks._allowed_ip(ipaddress.ip_address("::ffff:192.168.1.1"))
    assert webhooks._allowed_ip(ipaddress.ip_address("::ffff:93.184.216.34"))


# --- Subscriptions API -------------------------------------------------------------------------------------------


@pytest.fixture
def clients(make_client):
    sdb = make_client("SDB", "4111111111")
    other = make_client("OTHER", "4222222222")
    admin = make_user("admin@sdb.example", Role.CLIENT_ADMIN, client_ids=[sdb.pk])
    user = make_user("user@sdb.example", Role.CLIENT_USER, client_ids=[sdb.pk])
    return {"sdb": sdb, "other": other, "admin": api_for(admin), "user": api_for(user)}


def subscribe(api, events=("oms.sales_order.packed",), url="https://erp.example.com/hook", **extra):
    return api.post("/webhooks", {"url": url, "event_types": list(events), **extra})


def test_client_admin_subscribes_and_sees_the_secret_once(clients):
    response = subscribe(clients["admin"], description="ERP")
    assert response.status_code == 201, response.content
    created = response.json()
    assert created["secret"].startswith("whsec_") and created["client_id"] == str(clients["sdb"].pk)
    listed = clients["admin"].get("/webhooks").json()
    assert [w["id"] for w in listed] == [created["id"]] and "secret" not in listed[0]
    stored = WebhookSubscription.objects.get()
    assert stored.secret == created["secret"]  # decrypts
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT secret FROM platform_webhooksubscription")
        assert created["secret"] not in cursor.fetchone()[0]  # encrypted at rest


def test_subscription_rules(clients, bearer_api):
    assert subscribe(clients["user"]).status_code == 403  # client users can't
    assert subscribe(clients["admin"], events=["wms.pick_wave.completed"]).status_code == 422
    assert subscribe(clients["admin"], url="http://erp.example.com/hook").status_code == 422
    system = bearer_api(clients["sdb"])
    response = subscribe(system, client_id=str(clients["other"].pk))  # a client system acts for itself only
    assert response.status_code == 201 and response.json()["client_id"] == str(clients["sdb"].pk)
    for _ in range(webhooks.MAX_SUBSCRIPTIONS_PER_CLIENT - 1):
        assert subscribe(clients["admin"]).status_code == 201
    assert subscribe(clients["admin"]).status_code == 409


def test_other_clients_and_plain_staff_cant_touch_a_subscription(clients, bearer_api):
    created = subscribe(clients["admin"]).json()
    other_system = bearer_api(clients["other"])
    assert other_system.get(f"/webhooks/{created['id']}/deliveries").status_code == 404
    assert other_system.delete(f"/webhooks/{created['id']}").status_code == 404
    supervisor = make_user("sup@example.com", Role.WAREHOUSE_SUPERVISOR, all_clients=True, all_sites=True)
    assert api_for(supervisor).get("/webhooks").status_code == 403


# --- Fan-out and delivery ----------------------------------------------------------------------------------------


def emit(client_id, event_type="oms.sales_order.packed", **payload):
    with transaction.atomic():
        return outbox.emit(
            event_type,
            aggregate_type="oms.sales_order",
            aggregate_id="x",
            client_id=client_id,
            payload={"number": "ORD-1", **payload},
        )


def fan_out():
    for _ in range(5):
        published = outbox.publish_pending()
        stats = outbox.deliver_pending()
        if not published and not stats.delivered:
            return


@pytest.fixture
def capture(monkeypatch):
    calls = []
    responses = []

    def post(url, body, headers):
        calls.append({"url": url, "body": body, "headers": headers})
        result = responses.pop(0) if responses else (200, "ok")
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(webhooks, "post", post)
    return calls, responses


def test_events_reach_only_matching_subscriptions_signed(clients, bearer_api, capture):
    calls, _ = capture
    mine = subscribe(clients["admin"], events=["oms.sales_order.packed", "wms.handling_unit.closed"]).json()
    subscribe(clients["admin"], events=["oms.sales_order.cancelled"])  # not this event
    subscribe(bearer_api(clients["other"]))  # another client's
    event = emit(clients["sdb"].pk)
    emit(None)  # events without a client never go out
    fan_out()
    assert WebhookDelivery.objects.count() == 1
    stats = webhooks.send_due()
    assert stats.delivered == 1 and len(calls) == 1
    call = calls[0]
    body = json.loads(call["body"])
    assert body["id"] == str(event.event_id) and body["type"] == "oms.sales_order.packed"
    assert body["data"]["number"] == "ORD-1"
    assert call["headers"]["Nuronteq-Event"] == "oms.sales_order.packed"
    assert webhooks.verify(mine["secret"], call["headers"]["Nuronteq-Signature"], call["body"])
    delivery = WebhookDelivery.objects.get()
    assert delivery.status == "delivered" and delivery.last_status_code == 200
    assert call["headers"]["Nuronteq-Delivery"] == str(delivery.pk)
    assert webhooks.send_due().delivered == 0  # nothing left
    fan_out()  # replays don't duplicate
    assert WebhookDelivery.objects.count() == 1


def test_failures_retry_with_backoff_then_dead_letter(clients, capture, settings):
    settings.WEBHOOK_MAX_ATTEMPTS = 3
    calls, responses = capture
    created = subscribe(clients["admin"]).json()
    emit(clients["sdb"].pk)
    fan_out()
    responses.extend([(500, "boom"), OSError("connection refused"), (302, "")])  # redirects aren't followed
    for expected in ["pending", "pending", "dead"]:
        stats = webhooks.send_due()
        delivery = WebhookDelivery.objects.get()
        assert delivery.status == expected
        if expected == "pending":
            assert stats.retried == 1 and delivery.next_attempt_at > timezone.now()
            WebhookDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    assert delivery.attempts == 3 and delivery.last_status_code == 302
    assert len(calls) == 3
    case = ExceptionCase.objects.get(kind="webhook.dead_letter")
    assert case.detail["subscription_id"] == created["id"] and case.client_id == clients["sdb"].pk
    deliveries = clients["admin"].get(f"/webhooks/{created['id']}/deliveries").json()
    assert deliveries[0]["status"] == "dead" and deliveries[0]["attempts"] == 3
    assert webhooks.requeue(delivery.pk) and not webhooks.requeue(delivery.pk)  # only dead ones
    assert webhooks.send_due().delivered == 1


def test_a_leased_delivery_is_not_sent_twice(clients, capture):
    """`capture` stubs the network."""
    subscribe(clients["admin"])
    emit(clients["sdb"].pk)
    fan_out()
    leased = webhooks._lease(10)
    assert len(leased) == 1 and webhooks._lease(10) == []  # a second worker finds nothing
    WebhookDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))  # lease expired
    assert webhooks.send_due().delivered == 1


def test_disabling_drops_queued_deliveries_quietly(clients, capture):
    created = subscribe(clients["admin"]).json()
    emit(clients["sdb"].pk)
    fan_out()
    response = clients["admin"].delete(f"/webhooks/{created['id']}")
    assert response.status_code == 200 and response.json()["is_active"] is False
    assert webhooks.send_due().dead == 1
    assert not ExceptionCase.objects.filter(kind="webhook.dead_letter").exists()
    assert clients["admin"].get("/webhooks").json() == []


def test_ping_over_real_http(clients, settings):
    """End to end through the pinned connection, against a local server (allowed only by the test flags)."""
    settings.WEBHOOK_ALLOW_INSECURE = True
    settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS = True
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append((self.path, dict(self.headers), self.rfile.read(length)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/hooks?token=abc"
        created = subscribe(clients["admin"], url=url).json()
        response = clients["admin"].post(f"/webhooks/{created['id']}/ping")
        assert response.status_code == 202, response.content
        assert webhooks.send_due().delivered == 1
    finally:
        server.shutdown()
    path, headers, body = received[0]
    assert path == "/hooks?token=abc"
    assert headers["Nuronteq-Event"] == "platform.webhook.ping"
    assert webhooks.verify(created["secret"], headers["Nuronteq-Signature"], body, now=int(time.time()))
    assert json.loads(body)["data"]["subscription_id"] == created["id"]
