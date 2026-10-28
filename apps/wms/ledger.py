"""The inventory ledger. `post()` is the only code that changes on-hand stock; `reserve_available()` and
`release()` are the only code that changes allocated stock."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from apps.platform.apiutils import Problem
from apps.platform.context import current_actor

from .models import InventoryBalance, InventoryMovement, Location


@dataclass(frozen=True, slots=True)
class StockKey:
    client_id: uuid.UUID
    item_id: uuid.UUID
    location_id: uuid.UUID
    lot: str
    expiry: date | None
    status: str

    def filter(self) -> dict:
        return {
            "client_id": self.client_id,
            "item_id": self.item_id,
            "location_id": self.location_id,
            "lot": self.lot,
            "expiry": self.expiry,
            "status": self.status,
        }

    def at(self, location: Location, status: str | None = None) -> "StockKey":
        return StockKey(
            self.client_id, self.item_id, location.pk, self.lot, self.expiry, status if status else self.status
        )

    @classmethod
    def of(cls, balance: InventoryBalance) -> "StockKey":
        return cls(balance.client_id, balance.item_id, balance.location_id, balance.lot, balance.expiry, balance.status)


def already_applied(command_id: uuid.UUID | None) -> bool:
    return command_id is not None and InventoryMovement.objects.filter(command_id=command_id).exists()


def post(
    key: StockKey,
    qty_delta: Decimal,
    *,
    kind: str,
    reason: str = "",
    source_type: str = "",
    source_id: str = "",
    command_id: uuid.UUID | None = None,
) -> InventoryMovement:
    """Apply one signed quantity to one balance and record why, atomically."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("ledger.post() must run inside transaction.atomic()")
    if qty_delta == 0:
        raise ValueError("qty_delta must be non-zero")
    balance, _ = InventoryBalance.objects.get_or_create(**key.filter())
    balance = InventoryBalance.objects.select_for_update().get(pk=balance.pk)
    new_on_hand = balance.qty_on_hand + qty_delta
    if new_on_hand < 0 or new_on_hand < balance.qty_allocated:
        raise Problem(
            409,
            "Insufficient stock",
            f"{balance.qty_on_hand - balance.qty_allocated} available at this location; {-qty_delta} requested.",
        )
    balance.qty_on_hand = new_on_hand
    balance.save(update_fields=["qty_on_hand", "updated_at"])
    return InventoryMovement.objects.create(
        client_id=key.client_id,
        balance=balance,
        kind=kind,
        qty_delta=qty_delta,
        reason=reason,
        source_type=source_type,
        source_id=str(source_id),
        command_id=command_id,
        actor_id=current_actor().id,
    )


def reserve_available(balance_id: uuid.UUID, up_to: Decimal) -> Decimal:
    """Earmark up to `up_to` of a balance's available stock for an order and return how much was taken
    (zero if another transaction got there first)."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("ledger.reserve_available() must run inside transaction.atomic()")
    if up_to <= 0:
        raise ValueError("up_to must be positive")
    balance = InventoryBalance.objects.select_for_update().get(pk=balance_id)
    taken = min(up_to, balance.qty_on_hand - balance.qty_allocated)
    if taken > 0:
        balance.qty_allocated += taken
        balance.save(update_fields=["qty_allocated", "updated_at"])
    return max(taken, Decimal(0))


def release(balance_id: uuid.UUID, qty: Decimal) -> InventoryBalance:
    """Give back `qty` previously reserved on a balance."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("ledger.release() must run inside transaction.atomic()")
    if qty <= 0:
        raise ValueError("qty must be positive")
    balance = InventoryBalance.objects.select_for_update().get(pk=balance_id)
    if balance.qty_allocated < qty:
        raise ValueError(f"releasing {qty} but only {balance.qty_allocated} is allocated on {balance_id}")
    balance.qty_allocated -= qty
    balance.save(update_fields=["qty_allocated", "updated_at"])
    return balance


def transfer(
    source: StockKey, target: StockKey, qty: Decimal, *, kind: str, command_id: uuid.UUID | None = None, **meta
) -> tuple[InventoryMovement, InventoryMovement]:
    if qty <= 0:
        raise ValueError("qty must be positive")
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("ledger.transfer() must run inside transaction.atomic()")
    # Lock both balances up front, in id order: two opposite transfers between the same bins would otherwise each
    # hold one row and wait for the other (a deadlock the load test found with scanners moving stock both ways).
    ids = sorted(InventoryBalance.objects.get_or_create(**key.filter())[0].pk for key in (source, target))
    list(InventoryBalance.objects.select_for_update().filter(pk__in=ids).order_by("pk").values_list("pk"))
    out = post(source, -qty, kind=kind, command_id=command_id, **meta)
    into = post(target, qty, kind=kind, command_id=command_id, **meta)
    return out, into


def drift():
    """Balances whose on-hand quantity differs from the sum of their movements (should always be empty)."""
    ledger_sum = (
        InventoryMovement.objects.filter(balance=OuterRef("pk"))
        .values("balance")
        .annotate(total=Sum("qty_delta"))
        .values("total")
    )
    zero = Value(Decimal(0), output_field=DecimalField(max_digits=14, decimal_places=3))
    return (
        InventoryBalance.objects.annotate(ledger=Coalesce(Subquery(ledger_sum), zero))
        .exclude(qty_on_hand=F("ledger"))
        .order_by("pk")
    )
