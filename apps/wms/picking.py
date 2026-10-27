"""Waves and picking.

A supervisor plans a wave from allocated orders and releases it. Release moves the orders to picking (they can
no longer be cancelled), marks their allocations released and creates one pick task per allocation. Operators
scan the bin and the item and confirm the quantity. Picked stock leaves the inventory ledger at the bin
(movement kind `pick`); packing (next) tracks it from there.

A short pick takes what was found, gives back the rest of the reservation and immediately tries to allocate the
missing quantity from other bins, adding pick tasks to the same wave. Whatever can't be found raises
`wms.allocation.shorted` and a `pick.short` case. Either way the bin gets an `inventory.short_pick` case so
someone counts it: the ledger still believes the missing stock is there.

Every pick in a wave locks the wave row first, so the last task to finish always sees the others finished and
completes the wave exactly once.
"""

import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.masterdata.models import Item
from apps.oms import services as oms
from apps.platform import exceptions_queue, outbox
from apps.platform.apiutils import Problem
from apps.platform.models import Severity
from apps.platform.numbers import next_number

from . import allocation as allocator
from . import ledger
from .models import (
    OPEN_PICK,
    QTY_STEP,
    Allocation,
    AllocationStatus,
    MovementKind,
    PickTask,
    PickTaskStatus,
    PickWave,
    WaveOrder,
    WaveStatus,
)

MAX_WAVE_ORDERS = 200


# --- Waves -------------------------------------------------------------------------------------------------------


@transaction.atomic
def plan_wave(*, site_id: uuid.UUID, order_ids: list[uuid.UUID], created_by: uuid.UUID | None) -> PickWave:
    order_ids = list(dict.fromkeys(order_ids))
    if not order_ids:
        raise Problem(422, "No orders", "Choose at least one allocated order.")
    if len(order_ids) > MAX_WAVE_ORDERS:
        raise Problem(422, "Too many orders", f"A wave takes at most {MAX_WAVE_ORDERS} orders.")
    summaries, errors = [], []
    already = set(WaveOrder.objects.filter(sales_order_id__in=order_ids).values_list("sales_order_id", flat=True))
    for order_id in order_ids:
        summary = oms.order_summary(order_id)
        if summary is None or summary.site_id != site_id:
            errors.append({"field": "sales_order_ids", "message": f"{order_id}: not an order at this site"})
        elif order_id in already:
            errors.append({"field": "sales_order_ids", "message": f"{summary.number}: already in a wave"})
        elif summary.status != oms.OrderStatus.ALLOCATED:
            errors.append({"field": "sales_order_ids", "message": f"{summary.number}: {summary.status}, not allocated"})
        else:
            summaries.append(summary)
    if errors:
        raise Problem(422, "Orders can't be waved", "; ".join(e["message"] for e in errors), errors=errors)
    wave = PickWave.objects.create(site_id=site_id, number=next_number("wave", "WAV"), created_by=created_by)
    WaveOrder.objects.bulk_create(
        WaveOrder(wave=wave, client_id=s.client_id, sales_order_id=s.sales_order_id, number=s.number) for s in summaries
    )
    return wave


@transaction.atomic
def release_wave(wave_id: uuid.UUID) -> PickWave:
    """Start picking. Orders cancelled or changed since planning are dropped from the wave."""
    wave = PickWave.objects.select_for_update().get(pk=wave_id)
    if wave.status != WaveStatus.PLANNED:
        raise Problem(409, "Wave already released", f"{wave.number} is {wave.status}.")
    planned = list(wave.orders.values_list("sales_order_id", flat=True))
    started = oms.begin_picking(planned)
    wave.orders.exclude(sales_order_id__in=started).delete()
    if not started:
        raise Problem(409, "Nothing to release", "None of the wave's orders is still fully allocated.")
    allocations = (
        Allocation.objects.select_for_update(of=("self",))
        .filter(sales_order_id__in=started, status=AllocationStatus.RESERVED)
        .select_related("balance")
        .order_by("pk")
    )
    for a in allocations:
        a.status = AllocationStatus.RELEASED
        a.pick_wave = wave
        a.save()
        _create_task(a, wave)
    wave.status = WaveStatus.RELEASED
    wave.released_at = timezone.now()
    wave.save()
    return wave


def _create_task(a: Allocation, wave: PickWave) -> PickTask:
    return PickTask.objects.create(
        client_id=a.client_id,
        site_id=a.site_id,
        wave=wave,
        allocation=a,
        sales_order_id=a.sales_order_id,
        item_id=a.balance.item_id,
        from_location_id=a.balance.location_id,
        qty=a.qty_allocated,
    )


def _complete_if_done(wave: PickWave) -> None:
    """Caller holds the wave lock."""
    if wave.status != WaveStatus.RELEASED or wave.tasks.filter(status__in=OPEN_PICK).exists():
        return
    wave.status = WaveStatus.COMPLETED
    wave.completed_at = timezone.now()
    wave.save()
    tasks = wave.tasks.all()
    outbox.emit(
        "wms.pick_wave.completed",
        aggregate_type="wms.pick_wave",
        aggregate_id=wave.pk,
        site_id=wave.site_id,
        payload={
            "pick_wave_id": str(wave.pk),
            "number": wave.number,
            "sales_order_ids": [
                str(i) for i in wave.orders.order_by("number").values_list("sales_order_id", flat=True)
            ],
            "tasks_total": tasks.count(),
            "tasks_short": tasks.filter(status=PickTaskStatus.SHORT).count(),
            "completed_at": wave.completed_at.isoformat(),
        },
    )


# --- Tasks -------------------------------------------------------------------------------------------------------


def _lock(task_id: uuid.UUID) -> tuple[PickWave, PickTask]:
    wave_id = PickTask.objects.filter(pk=task_id).values_list("wave_id", flat=True).first()
    if wave_id is None:
        raise Problem(404, "Not found")
    wave = PickWave.objects.select_for_update().get(pk=wave_id)
    task = (
        PickTask.objects.select_for_update(of=("self",))
        .select_related("allocation__balance", "from_location", "item")
        .get(pk=task_id)
    )
    return wave, task


@transaction.atomic
def claim(task_id: uuid.UUID, user_id: uuid.UUID) -> PickTask:
    task = PickTask.objects.select_for_update().get(pk=task_id)
    if task.status not in OPEN_PICK:
        raise Problem(409, "Task already finished", f"This pick is {task.status}.")
    if task.assigned_to and task.assigned_to != user_id:
        raise Problem(409, "Task claimed", "Another operator has this task.")
    task.assigned_to = user_id
    task.status = PickTaskStatus.CLAIMED
    task.save()
    return task


def _check_open(task: PickTask, command_id: uuid.UUID | None) -> bool:
    """False if this is a replay of the scan that finished the task; raises if someone else finished it."""
    if task.status in OPEN_PICK:
        return True
    if command_id is not None and ledger.already_applied(command_id):
        return False
    raise Problem(409, "Task already finished", f"This pick is {task.status}.")


def _check_bin(task: PickTask, location_code: str) -> None:
    if location_code.strip().upper() != task.from_location.code:
        raise Problem(422, "Wrong bin", f"This pick is from {task.from_location.code}, not {location_code.strip()!r}.")


def _check_item(item: Item, item_code: str) -> None:
    code = item_code.strip()
    if code.upper() != item.sku.upper() and not item.barcodes.filter(barcode=code).exists():
        raise Problem(422, "Wrong item", f"{code!r} is not {item.sku}.")


def _take(task: PickTask, qty: Decimal, command_id: uuid.UUID | None) -> None:
    """Consume `qty` of the task's reservation: release it and post the pick movement in one transaction."""
    balance = task.allocation.balance
    ledger.release(balance.pk, qty)
    ledger.post(
        ledger.StockKey.of(balance),
        -qty,
        kind=MovementKind.PICK,
        source_type="wms.picktask",
        source_id=str(task.pk),
        command_id=command_id,
    )


@transaction.atomic
def confirm(
    task_id: uuid.UUID, *, location_code: str, item_code: str, qty: Decimal, command_id: uuid.UUID | None = None
) -> PickTask:
    qty = qty.quantize(QTY_STEP)
    wave, task = _lock(task_id)
    if not _check_open(task, command_id):
        return task
    _check_bin(task, location_code)
    _check_item(task.item, item_code)
    if qty != task.qty:
        raise Problem(
            422,
            "Quantity differs",
            f"This pick is {task.qty}. If there isn't enough in the bin, report a short pick instead.",
        )
    _take(task, qty, command_id)
    a = task.allocation
    a.qty_picked = qty
    a.status = AllocationStatus.PICKED
    a.save()
    task.qty_picked = qty
    task.status = PickTaskStatus.DONE
    task.completed_at = timezone.now()
    task.save()
    _complete_if_done(wave)
    return task


@transaction.atomic
def short(
    task_id: uuid.UUID,
    *,
    location_code: str,
    qty_found: Decimal,
    reason: str,
    command_id: uuid.UUID | None = None,
) -> PickTask:
    qty_found = qty_found.quantize(QTY_STEP)
    wave, task = _lock(task_id)
    if not _check_open(task, command_id):
        return task
    _check_bin(task, location_code)
    if not reason.strip():
        raise Problem(422, "Reason required", "Say why the full quantity couldn't be picked.")
    if qty_found < 0 or qty_found >= task.qty:
        raise Problem(422, "Invalid quantity", f"A short pick finds between 0 and less than {task.qty}.")
    if qty_found > 0:
        _take(task, qty_found, command_id)
    a = task.allocation
    balance = a.balance
    ledger.release(balance.pk, task.qty - qty_found)
    a.qty_picked = qty_found
    a.status = AllocationStatus.SHORT
    a.save()
    task.qty_picked = qty_found
    task.status = PickTaskStatus.SHORT
    task.short_reason = reason.strip()[:200]
    task.completed_at = timezone.now()
    task.save()

    exceptions_queue.raise_case(
        kind="inventory.short_pick",
        severity=Severity.MEDIUM,
        summary=f"Count {task.from_location.code}: {task.item.sku} short by {task.qty - qty_found} on a pick",
        detail={
            "balance_id": str(balance.pk),
            "location": task.from_location.code,
            "item_id": str(task.item_id),
            "lot": balance.lot,
            "expected": str(task.qty),
            "found": str(qty_found),
            "reason": task.short_reason,
            "pick_task_id": str(task.pk),
        },
        client_id=task.client_id,
        site_id=task.site_id,
        entity_type="wms.inventorybalance",
        entity_id=str(balance.pk),
        dedupe_key=f"inventory.short_pick:{balance.pk}",
    )
    _reallocate(wave, task, exclude_balance_id=balance.pk)
    _complete_if_done(wave)
    return task


def _reallocate(wave: PickWave, task: PickTask, *, exclude_balance_id: uuid.UUID) -> None:
    """Find the missing quantity elsewhere and add it to this wave; report what can't be found."""
    order = oms.lock_order_for_allocation(task.sales_order_id)
    line = next((ln for ln in order.lines if ln.line_id == task.allocation.order_line_id), None) if order else None
    if line is None:
        return
    have, created = allocator.top_up_line(
        order,
        line,
        status=AllocationStatus.RELEASED,
        pick_wave_id=wave.pk,
        exclude_balance_ids=(exclude_balance_id,),
    )
    for a in Allocation.objects.filter(pk__in=[c.pk for c in created]).select_related("balance"):
        _create_task(a, wave)
    if have >= line.base_qty:
        return
    result = allocator.LineResult(line.line_id, line.item_id, line.base_qty, have)
    allocator.emit_shorted(order, result)
    exceptions_queue.raise_case(
        kind="pick.short",
        severity=Severity.HIGH,
        summary=f"{order.number}: no stock left for {task.item.sku}, {line.base_qty - have} short",
        detail={
            "sales_order_id": str(order.sales_order_id),
            "order_line_id": str(line.line_id),
            "item_id": str(line.item_id),
            "qty_requested": str(line.base_qty),
            "qty_covered": str(have),
        },
        client_id=order.client_id,
        site_id=order.site_id,
        entity_type="oms.sales_order",
        entity_id=str(order.sales_order_id),
        dedupe_key=f"pick.short:{line.line_id}",
    )
