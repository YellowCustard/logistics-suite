"""Numbers on the admin home: headline KPI cards and the status chips on each app card.

Platform owns this registry; each business module registers its providers in AppConfig.ready(), so platform never
imports oms, wms or tms. A provider runs one aggregate query and returns Metric rows; a provider that fails is logged
and skipped, so one broken query never takes the admin home down.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone

from .models import (
    DeliveryStatus,
    EventDelivery,
    ExceptionCase,
    ExceptionStatus,
    Severity,
    SyncCommand,
    SyncStatus,
    WebhookDelivery,
    WebhookDeliveryStatus,
)

logger = logging.getLogger(__name__)

Tone = Literal["neutral", "warning", "critical"]


@dataclass(frozen=True)
class Metric:
    label: str
    value: int
    href: str = ""  # KPI cards link here; chips are plain badges
    detail: str = ""
    tone: Tone = "neutral"
    icon: str = "activity"


Provider = Callable[[], list[Metric]]


@dataclass(frozen=True)
class _Entry:
    order: int
    permission: str  # e.g. "oms.view_salesorder"; users without it never see the metric
    provider: Provider


_kpis: list[_Entry] = []
_chips: dict[str, list[_Entry]] = {}


def register_kpi(order: int, permission: str, provider: Provider) -> None:
    if any(e.provider is provider for e in _kpis):  # idempotent if ready() runs again
        return
    _kpis.append(_Entry(order, permission, provider))
    _kpis.sort(key=lambda e: e.order)


def register_chips(app_label: str, permission: str, provider: Provider) -> None:
    entries = _chips.setdefault(app_label, [])
    if not any(e.provider is provider for e in entries):
        entries.append(_Entry(0, permission, provider))


def _run(entries: list[_Entry], request: HttpRequest) -> list[Metric]:
    out: list[Metric] = []
    for entry in entries:
        if not request.user.has_perm(entry.permission):
            continue
        try:
            with transaction.atomic():  # savepoint: a failed query must not poison the request's transaction
                out.extend(entry.provider())
        except Exception:  # the admin home must render even when one provider is broken
            logger.exception("admin dashboard provider failed: %s", entry.provider.__qualname__)
    return out


def kpis(request: HttpRequest) -> list[Metric]:
    return _run(_kpis, request)


def chips(request: HttpRequest, app_label: str) -> list[Metric]:
    return [m for m in _run(_chips.get(app_label, []), request) if m.value]


def changelist(model_key: str, **filters: str) -> str:
    """Admin change list URL with lookup filters, e.g. changelist("oms.salesorder", status__in="a,b")."""
    app_label, model_name = model_key.split(".")
    url = reverse(f"admin:{app_label}_{model_name}_changelist")
    return url + ("?" + "&".join(f"{k}={v}" for k, v in filters.items()) if filters else "")


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# --- Platform's own providers --------------------------------------------------------------------------------------


def open_exceptions() -> list[Metric]:
    c = ExceptionCase.objects.exclude(status=ExceptionStatus.RESOLVED).aggregate(
        open=Count("pk"),
        severe=Count("pk", filter=Q(severity__in=[Severity.HIGH, Severity.CRITICAL])),
        overdue=Count("pk", filter=Q(due_at__lt=timezone.now())),
    )
    if c["severe"]:
        detail, tone = f"{c['severe']} high or critical", "critical"
    elif c["overdue"]:
        detail, tone = f"{c['overdue']} overdue", "warning"
    else:
        detail, tone = "None high or critical", "neutral"
    href = changelist("platform.exceptioncase", status__in=f"{ExceptionStatus.OPEN},{ExceptionStatus.IN_PROGRESS}")
    return [Metric("Open exceptions", c["open"], href, detail=detail, tone=tone, icon="alert")]


def _dead_deliveries() -> tuple[int, int]:
    return (
        EventDelivery.objects.filter(status=DeliveryStatus.DEAD).count(),
        WebhookDelivery.objects.filter(status=WebhookDeliveryStatus.DEAD).count(),
    )


def integration_backlog() -> list[Metric]:
    events, webhooks = _dead_deliveries()
    since = timezone.now() - timedelta(hours=24)
    rejected = SyncCommand.objects.filter(status=SyncStatus.REJECTED, received_at__gte=since).count()
    total = events + webhooks + rejected
    if total:
        detail = f"{events + webhooks} dead, {rejected} rejected scans (24 h)"
    else:
        detail = "Nothing dead or rejected"
    href = changelist("platform.eventdelivery", status__exact=DeliveryStatus.DEAD)
    return [Metric("Integration backlog", total, href, detail=detail, tone="critical" if total else "neutral")]


def dead_event_chip() -> list[Metric]:
    events, _ = _dead_deliveries()
    return [Metric(plural(events, "dead event delivery", "dead event deliveries"), events, tone="critical")]


def dead_webhook_chip() -> list[Metric]:
    _, webhooks = _dead_deliveries()
    return [Metric(plural(webhooks, "dead webhook", "dead webhooks"), webhooks, tone="critical")]


def register() -> None:
    register_kpi(20, "platform.view_exceptioncase", open_exceptions)
    register_kpi(40, "platform.view_eventdelivery", integration_backlog)
    register_chips("platform", "platform.view_eventdelivery", dead_event_chip)
    register_chips("platform", "platform.view_webhookdelivery", dead_webhook_chip)
