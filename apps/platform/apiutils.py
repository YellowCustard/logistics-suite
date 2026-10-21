"""Shared API plumbing: RFC 9457 problems, role checks, scoped lookups, cursor pagination."""

import uuid
from typing import Any

from django.db.models import Model, QuerySet

from .auth import Principal


class Problem(Exception):
    """Raise from services or endpoints; rendered as application/problem+json."""

    def __init__(self, status: int, title: str, detail: str = "", errors: list[dict[str, Any]] | None = None):
        super().__init__(f"{status} {title}: {detail}")
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = errors or []

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": "about:blank", "title": self.title, "status": self.status}
        if self.detail:
            body["detail"] = self.detail
        if self.errors:
            body["errors"] = self.errors
        return body


def principal_of(request) -> Principal:
    principal = getattr(request, "auth", None)
    if not isinstance(principal, Principal):
        raise Problem(401, "Unauthorized")
    return principal


def require_roles(principal: Principal, *roles: str) -> None:
    if not principal.has_role(*roles):
        raise Problem(403, "Forbidden", "Your role does not allow this action.")


def require_staff(principal: Principal) -> None:
    if principal.is_client_scoped:
        raise Problem(403, "Forbidden", "Staff only.")


def get_scoped(qs: QuerySet, pk: uuid.UUID) -> Model:
    """404 (never 403) for rows outside the principal's scope, so ids of other clients do not leak."""
    obj = qs.filter(pk=pk).first()
    if obj is None:
        raise Problem(404, "Not found")
    return obj


def paginate(
    qs: QuerySet, cursor: uuid.UUID | None, limit: int, max_limit: int = 200
) -> tuple[list[Model], uuid.UUID | None]:
    """Keyset pagination on the UUIDv7 primary key (time-ordered, stable under inserts)."""
    limit = max(1, min(limit, max_limit))
    if cursor is not None:
        qs = qs.filter(pk__gt=cursor)
    rows = list(qs.order_by("pk")[: limit + 1])
    next_cursor = rows[limit - 1].pk if len(rows) > limit else None
    return rows[:limit], next_cursor
