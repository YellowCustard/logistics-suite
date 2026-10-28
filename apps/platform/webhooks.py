"""Outbound webhooks: tell a client's own systems about events concerning that client.

Flow:
  1. Each event type in WEBHOOK_EVENTS has an outbox consumer that fans the event out into one
     WebhookDelivery per active subscription of the event's client.
  2. `send_due()` (run by the outbox worker) leases due deliveries, POSTs them, and records the outcome:
     delivered on any 2xx, otherwise retried with backoff up to WEBHOOK_MAX_ATTEMPTS, then dead with an
     exception case.

Every request is signed: `Nuronteq-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<body>">` using the
subscription's secret. Receivers should check the signature, reject old timestamps, and de-duplicate on
`Nuronteq-Delivery` (stable across retries), because delivery is at-least-once.

Requests only go to HTTPS endpoints whose every resolved address is public, and the connection is pinned to the
address that was checked, so a DNS answer can't be switched to an internal address between check and connect.
Redirects are not followed.
"""

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import random
import secrets
import socket
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import exceptions_queue
from .apiutils import Problem
from .models import (
    OutboxEvent,
    Severity,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from .outbox import Event, subscribe

log = logging.getLogger(__name__)

WEBHOOK_EVENTS = (
    "oms.inbound_notice.accepted",
    "oms.inbound_notice.cancelled",
    "wms.receipt.completed",
    "oms.sales_order.accepted",
    "oms.sales_order.cancelled",
    "wms.allocation.shorted",
    "oms.sales_order.packed",
    "wms.handling_unit.closed",
    "wms.inventory.adjusted",
    "oms.sales_order.dispatched",
    "oms.sales_order.delivered",
    "tms.shipment.delivery_failed",
)
PING = "platform.webhook.ping"
MAX_SUBSCRIPTIONS_PER_CLIENT = 10
LEASE = timedelta(minutes=2)
RESPONSE_SNIPPET = 500


class UnsafeDestination(Exception):
    """The URL resolves to an address webhooks may not reach."""


# --- Subscriptions -----------------------------------------------------------------------------------------------


def check_url(url: str) -> str:
    """Structural checks at subscription time. Addresses are checked again on every send."""
    parts = urlsplit(url.strip())
    allowed = ("https", "http") if settings.WEBHOOK_ALLOW_INSECURE else ("https",)
    if parts.scheme not in allowed:
        raise Problem(422, "HTTPS required", "Webhook URLs must use https://.")
    if not parts.hostname or parts.username or parts.password:
        raise Problem(422, "Invalid URL", "Give a host name and no credentials in the URL.")
    try:
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError as error:
        raise Problem(422, "Invalid URL", "The port isn't valid.") from error
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise Problem(422, "Invalid URL", "Webhooks can't target internal hosts.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not _allowed_ip(literal):
        raise Problem(422, "Invalid URL", "Webhooks can't target private or reserved addresses.")
    return url.strip()


def check_event_types(event_types: list[str]) -> list[str]:
    unknown = sorted(set(event_types) - set(WEBHOOK_EVENTS))
    if unknown or not event_types:
        raise Problem(
            422,
            "Unknown event types",
            f"Choose from: {', '.join(WEBHOOK_EVENTS)}."
            + (f" Not available: {', '.join(unknown)}." if unknown else ""),
        )
    return sorted(set(event_types))


@transaction.atomic
def create_subscription(
    *, client_id: uuid.UUID, url: str, event_types: list[str], description: str, created_by: str
) -> tuple[WebhookSubscription, str]:
    """Returns the subscription and its signing secret, which is shown once."""
    active = WebhookSubscription.objects.select_for_update().filter(client_id=client_id, is_active=True)
    if active.count() >= MAX_SUBSCRIPTIONS_PER_CLIENT:
        raise Problem(409, "Too many webhooks", f"A client can have at most {MAX_SUBSCRIPTIONS_PER_CLIENT}.")
    secret = "whsec_" + secrets.token_urlsafe(32)
    subscription = WebhookSubscription.objects.create(
        client_id=client_id,
        url=check_url(url),
        event_types=check_event_types(event_types),
        secret=secret,
        description=description.strip(),
        created_by=created_by[:80],
    )
    return subscription, secret


@transaction.atomic
def ping(subscription: WebhookSubscription) -> WebhookDelivery:
    """Queue a test event for one subscription only."""
    event = OutboxEvent.objects.create(
        type=PING,
        aggregate_type="platform.webhook_subscription",
        aggregate_id=str(subscription.pk),
        client_id=subscription.client_id,
        payload={"subscription_id": str(subscription.pk), "message": "Test delivery from Nuronteq."},
        published_at=timezone.now(),  # not fanned out to consumers
    )
    return WebhookDelivery.objects.create(subscription=subscription, event=event, next_attempt_at=timezone.now())


# --- Fan-out ------------------------------------------------------------------------------------------------------


def fan_out(event: Event) -> None:
    if event.client_id is None:
        return
    subscriptions = WebhookSubscription.objects.filter(
        client_id=event.client_id, is_active=True, event_types__contains=[event.type]
    )
    now = timezone.now()
    WebhookDelivery.objects.bulk_create(
        [WebhookDelivery(subscription=s, event_id=event.event_id, next_attempt_at=now) for s in subscriptions],
        ignore_conflicts=True,
    )


for _event_type in WEBHOOK_EVENTS:
    subscribe(_event_type, consumer=f"platform.webhooks:{_event_type}")(fan_out)


# --- Sending ------------------------------------------------------------------------------------------------------


def body_for(event: OutboxEvent) -> bytes:
    document = {
        "id": str(event.event_id),
        "type": event.type,
        "version": event.version,
        "occurred_at": event.occurred_at.isoformat(),
        "client_id": str(event.client_id),
        "data": event.payload,
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True, default=str).encode()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(secret: str, header: str, body: bytes, *, tolerance: int = 300, now: int | None = None) -> bool:
    """Reference check for receivers (and tests)."""
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        timestamp = int(fields["t"])
    except (KeyError, ValueError):
        return False
    if abs((now if now is not None else int(time.time())) - timestamp) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), header)


def _allowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve(host: str, port: int) -> str:
    """The first resolved address, after checking that every resolved address is allowed."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise UnsafeDestination(f"cannot resolve {host}: {error}") from error
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise UnsafeDestination(f"{host} has no addresses")
    for address in addresses:
        if not _allowed_ip(ipaddress.ip_address(address.split("%")[0])):
            raise UnsafeDestination(f"{host} resolves to a non-public address")
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to an already-checked address while verifying the certificate for the host name."""

    def __init__(self, host: str, address: str, port: int, timeout: float):
        self._address = address
        self._tls = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._tls)

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float):
        self._address = address
        super().__init__(host, port, timeout=timeout)

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


def post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    """POST and return (status, start of the response body). Raises OSError/UnsafeDestination on failure."""
    parts = urlsplit(url)
    secure = parts.scheme == "https"
    if not secure and not settings.WEBHOOK_ALLOW_INSECURE:
        raise UnsafeDestination("plain http is not allowed")
    port = parts.port or (443 if secure else 80)
    address = resolve(parts.hostname, port)
    connection_class = _PinnedHTTPSConnection if secure else _PinnedHTTPConnection
    conn = connection_class(parts.hostname, address, port, settings.WEBHOOK_TIMEOUT_SECONDS)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        snippet = response.read(RESPONSE_SNIPPET).decode("utf-8", "replace")
        return response.status, snippet
    finally:
        conn.close()


def _backoff(attempts: int) -> timedelta:
    base = min(30 * 2 ** (attempts - 1), 6 * 3600)
    return timedelta(seconds=base * (1 + random.random() * 0.2))  # noqa: S311 - jitter, not crypto


@dataclass(slots=True)
class SendStats:
    delivered: int = 0
    retried: int = 0
    dead: int = 0


def _lease(limit: int) -> list[uuid.UUID]:
    now = timezone.now()
    with transaction.atomic():
        due = list(
            WebhookDelivery.objects.select_for_update(skip_locked=True)
            .filter(status=WebhookDeliveryStatus.PENDING, next_attempt_at__lte=now)
            .order_by("next_attempt_at")[:limit]
        )
        for delivery in due:
            delivery.attempts += 1
            delivery.next_attempt_at = now + LEASE  # if this worker dies mid-send, another retries after the lease
            delivery.save(update_fields=["attempts", "next_attempt_at"])
    return [d.pk for d in due]


def send_due(limit: int = 20) -> SendStats:
    stats = SendStats()
    for delivery_id in _lease(limit):
        delivery = WebhookDelivery.objects.select_related("subscription", "event").get(pk=delivery_id)
        outcome = _send(delivery)
        if outcome == WebhookDeliveryStatus.DELIVERED:
            stats.delivered += 1
        elif outcome == WebhookDeliveryStatus.DEAD:
            stats.dead += 1
        else:
            stats.retried += 1
    return stats


def requeue(delivery_id: uuid.UUID) -> bool:
    """Send a dead delivery again from scratch, after the receiver is fixed. False if it isn't dead."""
    return bool(
        WebhookDelivery.objects.filter(pk=delivery_id, status=WebhookDeliveryStatus.DEAD).update(
            status=WebhookDeliveryStatus.PENDING, attempts=0, next_attempt_at=timezone.now()
        )
    )


def _send(delivery: WebhookDelivery) -> str:
    subscription = delivery.subscription
    if not subscription.is_active:
        return _finish(delivery, WebhookDeliveryStatus.DEAD, None, "Subscription disabled.")
    body = body_for(delivery.event)
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Nuronteq-Webhooks/1",
        "Nuronteq-Event": delivery.event.type,
        "Nuronteq-Delivery": str(delivery.pk),
        "Nuronteq-Signature": sign(subscription.secret, timestamp, body),
    }
    try:
        status, snippet = post(subscription.url, body, headers)
    except (OSError, http.client.HTTPException, UnsafeDestination) as error:
        return _retry_or_die(delivery, None, f"{type(error).__name__}: {error}"[:RESPONSE_SNIPPET])
    if 200 <= status < 300:
        return _finish(delivery, WebhookDeliveryStatus.DELIVERED, status, "")
    return _retry_or_die(delivery, status, snippet)


def _retry_or_die(delivery: WebhookDelivery, status: int | None, error: str) -> str:
    if delivery.attempts >= settings.WEBHOOK_MAX_ATTEMPTS:
        return _finish(delivery, WebhookDeliveryStatus.DEAD, status, error)
    delivery.next_attempt_at = timezone.now() + _backoff(delivery.attempts)
    delivery.last_status_code = status
    delivery.last_error = error
    delivery.save(update_fields=["next_attempt_at", "last_status_code", "last_error"])
    log.warning("webhook retry", extra={"data": {"delivery_id": str(delivery.pk), "status": status}})
    return WebhookDeliveryStatus.PENDING


def _finish(delivery: WebhookDelivery, status: str, code: int | None, error: str) -> str:
    delivery.status = status
    delivery.last_status_code = code
    delivery.last_error = error
    if status == WebhookDeliveryStatus.DELIVERED:
        delivery.delivered_at = timezone.now()
    delivery.save(update_fields=["status", "last_status_code", "last_error", "delivered_at"])
    subscription = delivery.subscription
    if status == WebhookDeliveryStatus.DEAD and subscription.is_active:
        exceptions_queue.raise_case(
            kind="webhook.dead_letter",
            severity=Severity.MEDIUM,
            summary=f"Webhook to {subscription.url} failed {delivery.attempts} times",
            detail={
                "subscription_id": str(subscription.pk),
                "delivery_id": str(delivery.pk),
                "event_type": delivery.event.type,
                "last_status_code": code,
                "last_error": error,
            },
            client_id=subscription.client_id,
            entity_type="platform.webhooksubscription",
            entity_id=str(subscription.pk),
            dedupe_key=f"webhook.dead_letter:{subscription.pk}",
        )
    return status
