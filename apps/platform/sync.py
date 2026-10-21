"""Offline command sync.

Scanners queue commands while offline and push them, in order, when they reconnect. Each command runs in
its own transaction and is recorded once under its device-generated id, so a retry after a lost response
returns the stored outcome instead of applying twice. A rejected command is final: the device shows it to
the operator, and a corrected action is a new command with a new id.

Modules register handlers in a `sync_commands` module:

    @sync.command("putaway.confirm", PutawayConfirmCommand)
    def confirm(request, cmd, command_id) -> dict: ...

Handlers reuse the module's API functions, so authorisation and validation are identical online and offline.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pydantic
from django.db import DatabaseError, IntegrityError, transaction
from django.utils.module_loading import autodiscover_modules

from .apiutils import Problem
from .auth import Principal
from .models import SyncCommand, SyncStatus

log = logging.getLogger(__name__)

Handler = Callable[[Any, Any, uuid.UUID], dict[str, Any]]
RETRY = "retry"  # not stored: the device keeps the command queued and sends it again later


@dataclass(frozen=True, slots=True)
class _Registered:
    type: str
    schema: type[pydantic.BaseModel]
    handler: Handler


_registry: dict[str, _Registered] = {}
_discovered = False


def command(type_: str, schema: type[pydantic.BaseModel]) -> Callable[[Handler], Handler]:
    def decorator(handler: Handler) -> Handler:
        existing = _registry.get(type_)
        if existing is not None and existing.handler is not handler:
            raise ValueError(f"Sync command {type_!r} is already registered")
        _registry[type_] = _Registered(type_, schema, handler)
        return handler

    return decorator


def discover() -> None:
    global _discovered
    if not _discovered:
        autodiscover_modules("sync_commands")
        _discovered = True


def registered_types() -> list[str]:
    discover()
    return sorted(_registry)


@dataclass(frozen=True, slots=True)
class Outcome:
    command_id: uuid.UUID
    status: str  # applied | rejected | retry
    replayed: bool = False
    result: dict[str, Any] | None = None
    problem: dict[str, Any] | None = None


def _replay(stored: SyncCommand, principal: Principal) -> Outcome:
    if stored.actor != principal.idempotency_scope:
        # Never hand one user's outcome to another; a colliding id from a healthy device is practically impossible.
        problem = Problem(409, "Command id in use", "Generate a new command id.").body()
        return Outcome(stored.pk, SyncStatus.REJECTED, problem=problem)
    return Outcome(stored.pk, stored.status, replayed=True, result=stored.result, problem=stored.problem)


def _stored(command_id: uuid.UUID) -> SyncCommand | None:
    return SyncCommand.objects.filter(pk=command_id).first()


def apply(
    request,
    principal: Principal,
    *,
    device_id: str,
    command_id: uuid.UUID,
    type_: str,
    payload: dict[str, Any],
    recorded_at: datetime | None = None,
) -> Outcome:
    """Apply one command. Never raises for a business or validation failure; those become `rejected`."""
    discover()
    if (stored := _stored(command_id)) is not None:
        return _replay(stored, principal)
    record = {
        "pk": command_id,
        "type": type_[:60],
        "actor": principal.idempotency_scope,
        "device_id": device_id,
        "recorded_at": recorded_at,
    }
    try:
        registered = _registry.get(type_)
        if registered is None:
            raise Problem(422, "Unknown command", f"{type_!r} is not a sync command.")
        try:
            cmd = registered.schema.model_validate(payload)
        except pydantic.ValidationError as exc:
            errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
            raise Problem(422, "Validation failed", errors=errors) from exc
        with transaction.atomic():
            # Claiming the id first makes a concurrent retry of the same command wait here, then replay.
            row = SyncCommand.objects.create(status=SyncStatus.APPLIED, **record)
            row.result = registered.handler(request, cmd, command_id)
            row.save(update_fields=["result"])
        return Outcome(command_id, SyncStatus.APPLIED, result=row.result)
    except Problem as exc:
        problem = exc.body()
        try:
            SyncCommand.objects.create(status=SyncStatus.REJECTED, problem=problem, **record)
        except IntegrityError:
            if (stored := _stored(command_id)) is not None:
                return _replay(stored, principal)
            raise
        return Outcome(command_id, SyncStatus.REJECTED, problem=problem)
    except IntegrityError:
        if (stored := _stored(command_id)) is not None:
            return _replay(stored, principal)
        log.warning("sync command %s (%s) hit an integrity error; the device will retry", command_id, type_)
        return Outcome(command_id, RETRY, problem=Problem(409, "Conflict", "Try again shortly.").body())
    except DatabaseError:
        log.warning("sync command %s (%s) hit a database error; the device will retry", command_id, type_)
        return Outcome(command_id, RETRY, problem=Problem(503, "Try again", "The server is busy.").body())
    except Exception:
        log.exception("sync command %s (%s) failed unexpectedly", command_id, type_)
        return Outcome(command_id, RETRY, problem=Problem(500, "Server error", "Reported; try again later.").body())
