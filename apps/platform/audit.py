"""Audit trail for every model marked `Audited`.

Signals cover ORM saves and deletes (admin, services, CSV imports). QuerySet.update() and bulk_create()
bypass signals, so services must not use them on audited models; domain actions that are more than a
field change (publish, approve) are recorded explicitly with `record()`.
"""

import datetime as dt
import decimal
import uuid
from typing import Any

from django.db import models
from django.db.models.signals import post_delete, post_save, pre_save

from .context import current_actor, request_id_var
from .fields import EncryptedTextField
from .models import Audited, AuditEntry

MASK = "***"


def _json_safe(value: Any) -> Any:
    if isinstance(value, (uuid.UUID, decimal.Decimal)):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def snapshot(instance: models.Model) -> dict[str, Any]:
    """Concrete field values, with encrypted (personal) fields masked and excluded fields dropped."""
    exclude = set(getattr(instance, "audit_exclude_fields", ()))
    data: dict[str, Any] = {}
    for field in instance._meta.concrete_fields:
        if field.name in exclude:
            continue
        value = field.value_from_object(instance)
        if isinstance(field, EncryptedTextField):
            value = MASK if value else value
        data[field.attname] = _json_safe(value)
    return data


def record(
    *,
    action: str,
    entity_type: str,
    entity_id: Any,
    before: dict | None = None,
    after: dict | None = None,
    client_id: uuid.UUID | None = None,
) -> AuditEntry:
    actor = current_actor()
    changed = sorted(k for k in set(before or {}) | set(after or {}) if (before or {}).get(k) != (after or {}).get(k))
    return AuditEntry.objects.create(
        actor_kind=actor.kind,
        actor_id=actor.id,
        actor_label=actor.label,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        client_id=client_id,
        changed_fields=changed,
        before=before,
        after=after,
        request_id=request_id_var.get(),
    )


def _entity_type(instance: models.Model) -> str:
    return instance._meta.label_lower


def _client_id(instance: models.Model) -> uuid.UUID | None:
    value = getattr(instance, "client_id", None)
    return value if isinstance(value, uuid.UUID) else None


def _pre_save(sender, instance, raw=False, **kwargs):
    if raw or not isinstance(instance, Audited):
        return
    instance._audit_before = None
    if instance._state.adding or instance.pk is None:
        return
    previous = sender._base_manager.filter(pk=instance.pk).first()
    instance._audit_before = snapshot(previous) if previous is not None else None


def _post_save(sender, instance, created, raw=False, **kwargs):
    if raw or not isinstance(instance, Audited):
        return
    before = None if created else getattr(instance, "_audit_before", None)
    after = snapshot(instance)
    if not created and before == after:
        return
    record(
        action="create" if created else "update",
        entity_type=_entity_type(instance),
        entity_id=instance.pk,
        before=before,
        after=after,
        client_id=_client_id(instance),
    )


def _post_delete(sender, instance, **kwargs):
    if not isinstance(instance, Audited):
        return
    record(
        action="delete",
        entity_type=_entity_type(instance),
        entity_id=instance.pk,
        before=snapshot(instance),
        after=None,
        client_id=_client_id(instance),
    )


def connect() -> None:
    pre_save.connect(_pre_save, dispatch_uid="platform.audit.pre_save")
    post_save.connect(_post_save, dispatch_uid="platform.audit.post_save")
    post_delete.connect(_post_delete, dispatch_uid="platform.audit.post_delete")
