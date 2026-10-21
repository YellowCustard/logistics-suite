"""The inventory ledger. `post()` is the only code that changes an InventoryBalance."""

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


def transfer(
    source: StockKey, target: StockKey, qty: Decimal, *, kind: str, command_id: uuid.UUID | None = None, **meta
) -> tuple[InventoryMovement, InventoryMovement]:
    if qty <= 0:
        raise ValueError("qty must be positive")
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
