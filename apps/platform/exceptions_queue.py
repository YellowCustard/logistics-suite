"""Exception queue service used by every module and by reconciliation jobs."""

import uuid
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from .apiutils import Problem
from .models import ExceptionCase, ExceptionStatus, Severity


def raise_case(
    *,
    kind: str,
    summary: str,
    severity: str = Severity.MEDIUM,
    detail: dict[str, Any] | None = None,
    client_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    entity_type: str = "",
    entity_id: str = "",
    dedupe_key: str = "",
) -> ExceptionCase:
    """Open a case, or return the open case with the same dedupe_key (idempotent for recurring jobs)."""
    if dedupe_key:
        existing = ExceptionCase.objects.filter(dedupe_key=dedupe_key).exclude(status=ExceptionStatus.RESOLVED).first()
        if existing:
            return existing
    try:
        with transaction.atomic():
            return ExceptionCase.objects.create(
                kind=kind,
                summary=summary[:300],
                severity=severity,
                detail=detail or {},
                client_id=client_id,
                site_id=site_id,
                entity_type=entity_type,
                entity_id=str(entity_id),
                dedupe_key=dedupe_key,
            )
    except IntegrityError:  # lost a race with an identical detection
        return ExceptionCase.objects.get(dedupe_key=dedupe_key, status__in=["open", "in_progress"])


def resolve(case: ExceptionCase, resolution: str) -> ExceptionCase:
    if not resolution.strip():
        raise Problem(422, "Resolution required", "Describe how the exception was resolved.")
    if case.status == ExceptionStatus.RESOLVED:
        return case
    case.status = ExceptionStatus.RESOLVED
    case.resolution = resolution.strip()
    case.resolved_at = timezone.now()
    case.save()
    return case
