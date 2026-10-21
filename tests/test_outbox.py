import datetime as dt

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.platform import outbox
from apps.platform.models import DeliveryStatus, EventDelivery, ExceptionCase, InboxRecord, NumberSequence


@pytest.fixture
def consumer():
    """Registers a test handler and removes it afterwards (the registry is process-global)."""
    added: list[str] = []

    def register(name: str, event_type: str, handler):
        outbox.subscribe(event_type, consumer=name)(handler)
        added.append(name)

    yield register
    for name in added:
        outbox.registry.by_consumer.pop(name, None)


def _emit(event_type: str = "test.happened", **payload):
    with transaction.atomic():
        return outbox.emit(event_type, aggregate_type="test", aggregate_id="agg-1", payload=payload)


@pytest.mark.django_db
def test_emit_outside_a_transaction_is_refused(monkeypatch):
    # Simulated rather than using a transactional test, which would flush the seeded reference data.
    monkeypatch.setattr(connection, "in_atomic_block", False)
    with pytest.raises(RuntimeError, match="inside transaction"):
        outbox.emit("test.happened", aggregate_type="test", aggregate_id="1", payload={})


@pytest.mark.django_db
def test_event_is_delivered_once_even_when_replayed(consumer):
    seen = []
    consumer("test.counter", "test.happened", lambda event: seen.append(event.payload["n"]))
    consumer("test.other", "test.unrelated", lambda event: pytest.fail("wrong event type"))
    event = _emit(n=7)

    assert outbox.publish_pending() == 1
    assert outbox.publish_pending() == 0  # already fanned out
    assert outbox.deliver_pending().delivered == 1
    assert seen == [7]

    delivery = EventDelivery.objects.get(event=event, consumer="test.counter")
    assert delivery.status == DeliveryStatus.DELIVERED
    outbox.replay(delivery.pk)
    stats = outbox.deliver_pending()
    assert (stats.delivered, stats.duplicates) == (0, 1)
    assert seen == [7]
    assert InboxRecord.objects.filter(consumer="test.counter", event_id=event.event_id).count() == 1


@pytest.mark.django_db
def test_handler_context_carries_causation(consumer):
    captured = {}

    def handler(event):
        captured["event"] = event
        captured["causation"] = outbox.causation_id_var.get()

    consumer("test.ctx", "test.happened", handler)
    event = _emit()
    outbox.publish_pending()
    outbox.deliver_pending()
    assert captured["causation"] == event.event_id
    assert outbox.causation_id_var.get() is None  # reset after the handler


@pytest.mark.django_db
def test_failing_handler_rolls_back_its_writes_retries_then_dead_letters(consumer, settings):
    settings.OUTBOX_MAX_ATTEMPTS = 2

    def handler(event):
        NumberSequence.objects.create(name="side-effect", prefix="X", year=2026)
        raise ValueError("downstream unavailable")

    consumer("test.flaky", "test.happened", handler)
    event = _emit()
    outbox.publish_pending()

    stats = outbox.deliver_pending()
    assert (stats.retried, stats.dead) == (1, 0)
    delivery = EventDelivery.objects.get(event=event)
    assert delivery.next_attempt_at > timezone.now()
    assert "downstream unavailable" in delivery.last_error
    assert not NumberSequence.objects.filter(name="side-effect").exists()
    assert not InboxRecord.objects.filter(event_id=event.event_id).exists()

    assert outbox.deliver_pending().retried == 0  # not due yet
    EventDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now() - dt.timedelta(seconds=1))
    assert outbox.deliver_pending().dead == 1

    delivery.refresh_from_db()
    assert delivery.status == DeliveryStatus.DEAD
    case = ExceptionCase.objects.get(kind="outbox.dead_letter")
    assert case.entity_id == str(delivery.pk)


def test_backoff_grows_and_is_capped():
    assert outbox._backoff(1) >= dt.timedelta(seconds=5)
    assert outbox._backoff(4) >= dt.timedelta(seconds=40)
    assert outbox._backoff(30) <= dt.timedelta(seconds=3600 * 1.2)
