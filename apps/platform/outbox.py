"""Transactional outbox, fan-out to consumers, and idempotent delivery.

Flow:
  1. `emit()` inserts an OutboxEvent in the same transaction as the business change.
  2. `publish_pending()` fans each new event out into one EventDelivery per subscribed consumer.
  3. `deliver_pending()` claims due deliveries (FOR UPDATE SKIP LOCKED), runs the handler inside a
     savepoint together with an InboxRecord insert, and retries with backoff or dead-letters.

Delivery is at-least-once; the inbox makes handlers effectively-once per (consumer, event).
Ordering is per aggregate only in the common case (deliveries are claimed oldest-first); handlers
must check aggregate state rather than assume global order.
"""

import logging
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.utils.module_loading import autodiscover_modules

from .context import (
    SYSTEM_ACTOR,
    actor_var,
    causation_id_var,
    correlation_id_var,
    current_actor,
)
from .models import DeliveryStatus, EventDelivery, ExceptionCase, InboxRecord, OutboxEvent, Severity

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Event:
    event_id: uuid.UUID
    type: str
    version: int
    aggregate_type: str
    aggregate_id: str
    client_id: uuid.UUID | None
    site_id: uuid.UUID | None
    correlation_id: uuid.UUID | None
    causation_id: uuid.UUID | None
    actor_id: uuid.UUID | None
    occurred_at: datetime
    payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: OutboxEvent) -> "Event":
        return cls(
            event_id=row.event_id,
            type=row.type,
            version=row.version,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            client_id=row.client_id,
            site_id=row.site_id,
            correlation_id=row.correlation_id,
            causation_id=row.causation_id,
            actor_id=row.actor_id,
            occurred_at=row.occurred_at,
            payload=row.payload,
        )


Handler = Callable[[Event], None]


@dataclass(slots=True)
class _Subscription:
    consumer: str
    event_type: str
    versions: frozenset[int]
    handler: Handler


@dataclass(slots=True)
class _Registry:
    by_consumer: dict[str, _Subscription] = field(default_factory=dict)
    discovered: bool = False

    def consumers_for(self, event_type: str, version: int) -> list[str]:
        return sorted(
            s.consumer for s in self.by_consumer.values() if s.event_type == event_type and version in s.versions
        )


registry = _Registry()


def subscribe(event_type: str, *, consumer: str, versions: tuple[int, ...] = (1,)):
    """Register `handler` as consumer `consumer` of `event_type`. Consumer names are stable ids:
    renaming one orphans its pending deliveries."""

    def decorator(handler: Handler) -> Handler:
        existing = registry.by_consumer.get(consumer)
        if existing is not None and existing.handler is not handler:
            raise ValueError(f"Consumer {consumer!r} is already registered")
        registry.by_consumer[consumer] = _Subscription(consumer, event_type, frozenset(versions), handler)
        return handler

    return decorator


def discover_handlers() -> None:
    """Import every installed app's `handlers` module so subscriptions register."""
    if not registry.discovered:
        autodiscover_modules("handlers")
        registry.discovered = True


def emit(
    event_type: str,
    *,
    aggregate_type: str,
    aggregate_id: Any,
    payload: dict[str, Any],
    version: int = 1,
    client_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
) -> OutboxEvent:
    """Record a domain event. Must run inside the transaction that changes the aggregate, so the
    event exists if and only if the change committed."""
    if not connection.in_atomic_block:
        raise RuntimeError("emit() must be called inside transaction.atomic()")
    return OutboxEvent.objects.create(
        type=event_type,
        version=version,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        client_id=client_id,
        site_id=site_id,
        correlation_id=correlation_id_var.get(),
        causation_id=causation_id_var.get(),
        actor_id=current_actor().id,
        payload=payload,
    )


def publish_pending(batch_size: int = 100) -> int:
    """Fan out unpublished events into deliveries. Safe to run concurrently (SKIP LOCKED)."""
    discover_handlers()
    now = timezone.now()
    with transaction.atomic():
        events = list(
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(published_at__isnull=True)
            .order_by("event_id")[:batch_size]
        )
        for event in events:
            consumers = registry.consumers_for(event.type, event.version)
            EventDelivery.objects.bulk_create(
                [EventDelivery(event=event, consumer=c, next_attempt_at=now) for c in consumers],
                ignore_conflicts=True,
            )
            event.published_at = now
            event.save(update_fields=["published_at"])
    return len(events)


def _backoff(attempts: int) -> timedelta:
    base = min(5 * 2 ** (attempts - 1), 3600)
    return timedelta(seconds=base * (1 + random.random() * 0.2))  # noqa: S311 - jitter, not crypto


def _run(delivery: EventDelivery) -> bool:
    """Run one delivery's handler. Returns False when the inbox shows it already ran."""
    subscription = registry.by_consumer.get(delivery.consumer)
    if subscription is None:
        raise LookupError(f"No handler registered for consumer {delivery.consumer!r}")
    _, created = InboxRecord.objects.get_or_create(consumer=delivery.consumer, event_id=delivery.event_id)
    if not created:
        return False
    event = Event.from_row(delivery.event)
    tokens = (
        actor_var.set(SYSTEM_ACTOR),
        correlation_id_var.set(event.correlation_id),
        causation_id_var.set(event.event_id),
    )
    try:
        subscription.handler(event)
    finally:
        actor_var.reset(tokens[0])
        correlation_id_var.reset(tokens[1])
        causation_id_var.reset(tokens[2])
    return True


@dataclass(slots=True)
class DeliveryStats:
    delivered: int = 0
    duplicates: int = 0
    retried: int = 0
    dead: int = 0


def deliver_pending(max_deliveries: int = 100) -> DeliveryStats:
    """Process due deliveries one per transaction, so a slow or failing handler never holds others."""
    discover_handlers()
    stats = DeliveryStats()
    max_attempts = settings.OUTBOX_MAX_ATTEMPTS
    for _ in range(max_deliveries):
        with transaction.atomic():
            delivery = (
                EventDelivery.objects.select_for_update(skip_locked=True)
                .select_related("event")
                .filter(status=DeliveryStatus.PENDING, next_attempt_at__lte=timezone.now())
                .order_by("next_attempt_at", "id")
                .first()
            )
            if delivery is None:
                break
            delivery.attempts += 1
            try:
                with transaction.atomic():  # savepoint: handler effects + inbox row commit together
                    ran = _run(delivery)
            except Exception as exc:
                delivery.last_error = f"{type(exc).__name__}: {exc}"[:4000]
                if delivery.attempts >= max_attempts:
                    delivery.status = DeliveryStatus.DEAD
                    stats.dead += 1
                    _open_dead_letter_case(delivery)
                    log.error("delivery dead-lettered", extra={"data": _log_data(delivery)})
                else:
                    delivery.next_attempt_at = timezone.now() + _backoff(delivery.attempts)
                    stats.retried += 1
                    log.warning("delivery failed, will retry", extra={"data": _log_data(delivery)})
            else:
                delivery.status = DeliveryStatus.DELIVERED
                delivery.delivered_at = timezone.now()
                delivery.last_error = ""
                if ran:
                    stats.delivered += 1
                else:
                    stats.duplicates += 1
            delivery.save()
    return stats


def replay(delivery_id: uuid.UUID) -> None:
    """Put a dead or delivered delivery back in the queue. The inbox still prevents a second effect
    unless the inbox row is removed deliberately."""
    with transaction.atomic():
        delivery = EventDelivery.objects.select_for_update().get(pk=delivery_id)
        delivery.status = DeliveryStatus.PENDING
        delivery.attempts = 0
        delivery.next_attempt_at = timezone.now()
        delivery.save()


def _open_dead_letter_case(delivery: EventDelivery) -> None:
    ExceptionCase.objects.get_or_create(
        dedupe_key=f"outbox.dead:{delivery.pk}",
        status__in=["open", "in_progress"],
        defaults={
            "kind": "outbox.dead_letter",
            "severity": Severity.HIGH,
            "summary": f"{delivery.consumer} failed {delivery.attempts}× on {delivery.event.type}",
            "detail": {"event_id": str(delivery.event_id), "error": delivery.last_error},
            "client_id": delivery.event.client_id,
            "entity_type": "platform.eventdelivery",
            "entity_id": str(delivery.pk),
        },
    )


def _log_data(delivery: EventDelivery) -> dict[str, Any]:
    return {
        "delivery_id": str(delivery.pk),
        "event_id": str(delivery.event_id),
        "consumer": delivery.consumer,
        "attempts": delivery.attempts,
        "error": delivery.last_error,
    }
