"""Allocation: reserve stock for sales orders, earliest expiry first (FEFO), then first in (FIFO).

FIFO is by when stock arrived in its bin (the balance's first movement), which matches receipt order unless
stock was moved between bins. Expired stock, and stock in docks, staging lanes or quarantine, is never
allocated. Allocation tops up: re-running it for an order only reserves what its lines still lack.
"""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.oms import services as oms
from apps.platform import exceptions_queue, outbox
from apps.platform.models import ExceptionCase, ExceptionStatus, Severity

from . import ledger
from .models import (
    ACTIVE_ALLOCATION,
    Allocation,
    AllocationStatus,
    InventoryBalance,
    InventoryMovement,
    LocationKind,
    StockStatus,
)

ALLOCATABLE_KINDS = (LocationKind.STORAGE, LocationKind.PICK_FACE)


@dataclass(slots=True)
class LineResult:
    order_line_id: uuid.UUID
    item_id: uuid.UUID
    qty_requested: Decimal
    qty_allocated: Decimal

    @property
    def short(self) -> Decimal:
        return self.qty_requested - self.qty_allocated


@dataclass(slots=True)
class OrderResult:
    sales_order_id: uuid.UUID
    number: str
    lines: list[LineResult] = field(default_factory=list)

    @property
    def fully_allocated(self) -> bool:
        return all(line.short <= 0 for line in self.lines)


def candidates(*, client_id: uuid.UUID, site_id: uuid.UUID, item_id: uuid.UUID):
    """Balances this order line may draw from, in the order they should be used."""
    first_movement = (
        InventoryMovement.objects.filter(balance=OuterRef("pk")).order_by("occurred_at").values("occurred_at")[:1]
    )
    return (
        InventoryBalance.objects.filter(
            client_id=client_id,
            item_id=item_id,
            status=StockStatus.AVAILABLE,
            location__site_id=site_id,
            location__is_active=True,
            location__kind__in=ALLOCATABLE_KINDS,
            qty_on_hand__gt=F("qty_allocated"),
        )
        .filter(Q(expiry__isnull=True) | Q(expiry__gte=timezone.localdate()))
        .annotate(arrived_at=Subquery(first_movement))
        .order_by(
            F("expiry").asc(nulls_last=True), F("arrived_at").asc(nulls_last=True), "location__pick_sequence", "id"
        )
    )


def _allocated(order_line_id: uuid.UUID) -> Decimal:
    total = Allocation.objects.filter(order_line_id=order_line_id, status__in=ACTIVE_ALLOCATION).aggregate(
        total=Sum("qty_allocated")
    )["total"]
    return total or Decimal(0)


@transaction.atomic
def allocate_order(order_id: uuid.UUID) -> OrderResult | None:
    """Reserve what the order's open lines still need. Returns None if the order can no longer be allocated
    (cancelled, already picking, or unknown)."""
    order = oms.lock_order_for_allocation(order_id)
    if order is None or not order.allocatable:
        return None
    result = OrderResult(order.sales_order_id, order.number)
    for line in order.lines:
        have = _allocated(line.line_id)
        need = line.base_qty - have
        if need > 0:
            for balance in candidates(client_id=order.client_id, site_id=order.site_id, item_id=line.item_id):
                taken = ledger.reserve_available(balance.pk, need)
                if taken <= 0:
                    continue
                Allocation.objects.create(
                    client_id=order.client_id,
                    site_id=order.site_id,
                    sales_order_id=order.sales_order_id,
                    order_line_id=line.line_id,
                    balance_id=balance.pk,
                    qty_allocated=taken,
                )
                have, need = have + taken, need - taken
                if need <= 0:
                    break
        result.lines.append(LineResult(line.line_id, line.item_id, line.base_qty, have))
    _publish(order, result)
    return result


def _publish(order: oms.OrderToAllocate, result: OrderResult) -> None:
    lines = [
        {
            "order_line_id": str(line.order_line_id),
            "item_id": str(line.item_id),
            "qty_requested": str(line.qty_requested),
            "qty_allocated": str(line.qty_allocated),
        }
        for line in result.lines
    ]
    envelope = {
        "aggregate_type": "oms.sales_order",
        "aggregate_id": order.sales_order_id,
        "client_id": order.client_id,
        "site_id": order.site_id,
    }
    outbox.emit(
        "wms.allocation.completed",
        payload={
            "sales_order_id": str(order.sales_order_id),
            "fully_allocated": result.fully_allocated,
            "lines": lines,
        },
        **envelope,
    )
    dedupe_key = f"allocation.short:{order.sales_order_id}"
    if result.fully_allocated:
        _resolve_short_case(dedupe_key, "Fully allocated.")
        return
    short = [line for line in result.lines if line.short > 0]
    for line in short:
        outbox.emit(
            "wms.allocation.shorted",
            payload={
                "sales_order_id": str(order.sales_order_id),
                "order_line_id": str(line.order_line_id),
                "item_id": str(line.item_id),
                "qty_requested": str(line.qty_requested),
                "qty_allocated": str(line.qty_allocated),
            },
            **envelope,
        )
    exceptions_queue.raise_case(
        kind="allocation.short",
        severity=Severity.MEDIUM,
        summary=f"{order.number}: {len(short)} line(s) short of stock",
        detail={"sales_order_id": str(order.sales_order_id), "lines": lines},
        client_id=order.client_id,
        site_id=order.site_id,
        entity_type="oms.sales_order",
        entity_id=str(order.sales_order_id),
        dedupe_key=dedupe_key,
    )


def _resolve_short_case(dedupe_key: str, resolution: str) -> None:
    case = ExceptionCase.objects.filter(dedupe_key=dedupe_key).exclude(status=ExceptionStatus.RESOLVED).first()
    if case is not None:
        exceptions_queue.resolve(case, resolution)


@transaction.atomic
def release_order(order_id: uuid.UUID) -> int:
    """Give back every reservation of a cancelled order. Returns how many allocations were released."""
    oms.lock_order_for_allocation(order_id)  # serialise with a concurrent allocation run
    released = 0
    for allocation in Allocation.objects.select_for_update().filter(
        sales_order_id=order_id, status=AllocationStatus.RESERVED
    ):
        ledger.release(allocation.balance_id, allocation.qty_allocated)
        allocation.status = AllocationStatus.CANCELLED
        allocation.save()
        released += 1
    _resolve_short_case(f"allocation.short:{order_id}", "Order cancelled.")
    return released


def rerun(site_id: uuid.UUID, order_ids: list[uuid.UUID] | None = None) -> list[OrderResult]:
    """Top up backordered orders at a site (or the given ones), oldest required-by first. One transaction per
    order, so one failure doesn't undo the rest."""
    queue = order_ids if order_ids is not None else oms.orders_to_allocate(site_id)
    results = []
    for order_id in queue:
        result = allocate_order(order_id)
        if result is not None:
            results.append(result)
    return results


def drift():
    """Balances whose qty_allocated differs from what their open allocations hold (should always be empty)."""
    open_sum = (
        Allocation.objects.filter(
            balance=OuterRef("pk"), status__in=[AllocationStatus.RESERVED, AllocationStatus.RELEASED]
        )
        .values("balance")
        .annotate(total=Sum(F("qty_allocated") - F("qty_picked")))
        .values("total")
    )
    zero = Value(Decimal(0), output_field=DecimalField(max_digits=14, decimal_places=3))
    return (
        InventoryBalance.objects.annotate(held=Coalesce(Subquery(open_sum), zero))
        .exclude(qty_allocated=F("held"))
        .order_by("pk")
    )
