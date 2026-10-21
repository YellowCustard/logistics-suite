"""Putaway: suggest a bin, then move received stock from the dock into it."""

import uuid

from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.platform.apiutils import Problem

from . import ledger
from .models import (
    InventoryBalance,
    Location,
    LocationKind,
    MovementKind,
    PutawayTask,
    StockStatus,
    TaskStatus,
)

PUTAWAY_TARGET_KINDS = (LocationKind.STORAGE, LocationKind.PICK_FACE, LocationKind.QUARANTINE)


def _with_occupancy(qs):
    """Occupied pallet positions = non-empty balances plus putaway tasks already heading there."""
    stock = (
        InventoryBalance.objects.filter(location=OuterRef("pk"), qty_on_hand__gt=0)
        .values("location")
        .annotate(n=Count("pk"))
        .values("n")
    )
    pending = (
        PutawayTask.objects.filter(suggested_location=OuterRef("pk"), status__in=[TaskStatus.OPEN, TaskStatus.CLAIMED])
        .values("suggested_location")
        .annotate(n=Count("pk"))
        .values("n")
    )
    zero = Value(0, output_field=IntegerField())
    return qs.annotate(
        stock_slots=Coalesce(Subquery(stock, output_field=IntegerField()), zero),
        pending_slots=Coalesce(Subquery(pending, output_field=IntegerField()), zero),
    )


def suggest(*, site_id: uuid.UUID, client_id: uuid.UUID, item_id: uuid.UUID, status: str) -> Location | None:
    """Same SKU first (a bin already holding this client's item with room left), then the first empty bin
    on the pick path. Damaged or quarantined stock goes to quarantine bins only."""
    kinds = [LocationKind.QUARANTINE] if status != StockStatus.AVAILABLE else [LocationKind.STORAGE]
    candidates = _with_occupancy(
        Location.objects.filter(site_id=site_id, is_active=True, kind__in=kinds, pallet_capacity__gt=0)
    ).order_by("pick_sequence", "code")
    holding = InventoryBalance.objects.filter(
        client_id=client_id, item_id=item_id, status=status, qty_on_hand__gt=0
    ).values("location_id")
    for location in candidates.filter(pk__in=holding):
        if location.stock_slots + location.pending_slots < location.pallet_capacity:
            return location
    return candidates.filter(stock_slots=0, pending_slots=0).first()


def create_task(*, receipt_line, from_balance: InventoryBalance) -> PutawayTask:
    return PutawayTask.objects.create(
        client_id=from_balance.client_id,
        site_id=receipt_line.receipt.site_id,
        receipt_line=receipt_line,
        from_balance=from_balance,
        qty=receipt_line.base_qty,
        suggested_location=suggest(
            site_id=receipt_line.receipt.site_id,
            client_id=from_balance.client_id,
            item_id=from_balance.item_id,
            status=from_balance.status,
        ),
    )


@transaction.atomic
def claim(task_id: uuid.UUID, user_id: uuid.UUID) -> PutawayTask:
    task = PutawayTask.objects.select_for_update().get(pk=task_id)
    if task.status == TaskStatus.DONE:
        raise Problem(409, "Task already done")
    if task.assigned_to and task.assigned_to != user_id:
        raise Problem(409, "Task claimed", "Another operator has this task.")
    task.assigned_to = user_id
    task.status = TaskStatus.CLAIMED
    task.save()
    return task


@transaction.atomic
def confirm(
    task_id: uuid.UUID, *, to_location_code: str, reason: str = "", command_id: uuid.UUID | None = None
) -> PutawayTask:
    task = PutawayTask.objects.select_for_update().select_related("from_balance").get(pk=task_id)
    if task.status == TaskStatus.DONE:
        if command_id is not None and ledger.already_applied(command_id):
            return task  # replay of the same scan
        raise Problem(409, "Task already done")
    target = Location.objects.filter(
        site_id=task.site_id, code=to_location_code.strip().upper(), is_active=True
    ).first()
    if target is None:
        raise Problem(422, "Unknown bin", f"No active bin {to_location_code!r} at this site.")
    if target.kind not in PUTAWAY_TARGET_KINDS:
        raise Problem(422, "Not a storage bin", f"{target.code} is a {target.kind} location.")
    status = task.from_balance.status
    if status != StockStatus.AVAILABLE and target.kind != LocationKind.QUARANTINE:
        raise Problem(422, "Quarantine bin required", f"{status} stock must go to a quarantine bin.")
    overridden = task.suggested_location_id is not None and target.pk != task.suggested_location_id
    if (overridden or task.suggested_location_id is None) and not reason.strip():
        raise Problem(422, "Reason required", "Say why the suggested bin was not used.")

    source = ledger.StockKey.of(task.from_balance)
    ledger.transfer(
        source,
        source.at(target),
        task.qty,
        kind=MovementKind.PUTAWAY,
        command_id=command_id,
        source_type="wms.putawaytask",
        source_id=str(task.pk),
        reason=reason.strip(),
    )
    task.to_location = target
    task.override_reason = reason.strip()
    task.status = TaskStatus.DONE
    task.completed_at = timezone.now()
    task.save()
    return task
