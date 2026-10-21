"""Direct stock corrections and bin-to-bin moves, plus the shared inventory.adjusted event."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from apps.masterdata.models import Item
from apps.platform import outbox
from apps.platform.apiutils import Problem

from . import ledger
from .models import QTY_STEP, InventoryBalance, InventoryMovement, Location, LocationKind, MovementKind, StockStatus


def needs_approval(system_qty: Decimal, qty_delta: Decimal) -> bool:
    """A variance above INVENTORY_VARIANCE_APPROVAL_PCT of the system quantity, or any change to a zero
    balance, needs a second pair of eyes."""
    if qty_delta == 0:
        return False
    if system_qty == 0:
        return True
    return abs(qty_delta) * 100 > Decimal(str(settings.INVENTORY_VARIANCE_APPROVAL_PCT)) * system_qty


def find_item(*, client_id: uuid.UUID | None, sku: str | None = None, barcode: str | None = None) -> Item:
    """Find an item by SKU or barcode. Shared warehouses hold several clients' stock, so a code that matches
    more than one client's item needs the client named."""
    if not (sku or barcode):
        raise Problem(422, "Item required", "Give a SKU or a barcode.")
    qs = Item.objects.filter(is_active=True)
    qs = qs.filter(sku=sku.strip()) if sku else qs.filter(barcodes__barcode=barcode.strip())
    if client_id is not None:
        qs = qs.filter(client_id=client_id)
    matches = list(qs.distinct()[:2])
    if not matches:
        raise Problem(422, "Unknown item", f"No active item {sku or barcode!r}.")
    if len(matches) > 1:
        raise Problem(422, "Ambiguous item", f"{sku or barcode!r} belongs to more than one client; name the client.")
    return matches[0]


def location_at(site_id: uuid.UUID, code: str) -> Location:
    location = Location.objects.filter(site_id=site_id, code=code.strip().upper(), is_active=True).first()
    if location is None:
        raise Problem(422, "Unknown bin", f"No active bin {code!r} at this site.")
    return location


def on_hand(key: ledger.StockKey) -> Decimal:
    value = InventoryBalance.objects.filter(**key.filter()).values_list("qty_on_hand", flat=True).first()
    return value if value is not None else Decimal(0)


def emit_adjusted(
    movement: InventoryMovement, *, site_id: uuid.UUID, qty_before: Decimal, approved_by: uuid.UUID | None
) -> None:
    balance = movement.balance
    outbox.emit(
        "wms.inventory.adjusted",
        aggregate_type="wms.inventorybalance",
        aggregate_id=balance.pk,
        client_id=balance.client_id,
        site_id=site_id,
        payload={
            "movement_id": str(movement.pk),
            "item_id": str(balance.item_id),
            "location_id": str(balance.location_id),
            "lot": balance.lot,
            "expiry": balance.expiry.isoformat() if balance.expiry else None,
            "status": balance.status,
            "qty_before": str(qty_before.quantize(QTY_STEP)),
            "qty_delta": str(movement.qty_delta.quantize(QTY_STEP)),
            "reason": movement.reason,
            "source_type": movement.source_type,
            "source_id": movement.source_id,
            "approved_by": str(approved_by) if approved_by else None,
        },
    )


@dataclass(frozen=True, slots=True)
class StockRef:
    site_id: uuid.UUID
    location_code: str
    client_id: uuid.UUID | None
    sku: str | None = None
    barcode: str | None = None
    lot: str = ""
    expiry: date | None = None
    status: str = StockStatus.AVAILABLE

    def resolve(self, location_code: str | None = None) -> tuple[ledger.StockKey, Location]:
        location = location_at(self.site_id, location_code or self.location_code)
        item = find_item(client_id=self.client_id, sku=self.sku, barcode=self.barcode)
        key = ledger.StockKey(item.client_id, item.pk, location.pk, self.lot.strip(), self.expiry, self.status)
        return key, location


@transaction.atomic
def adjust(ref: StockRef, qty_delta: Decimal, *, reason: str, command_id: uuid.UUID | None = None):
    if not reason.strip():
        raise Problem(422, "Reason required", "Say why the stock is being corrected.")
    if ledger.already_applied(command_id):
        return InventoryMovement.objects.filter(command_id=command_id).first()
    key, _ = ref.resolve()
    before = on_hand(key)
    movement = ledger.post(
        key, qty_delta, kind=MovementKind.ADJUST, reason=reason.strip(), source_type="manual", command_id=command_id
    )
    emit_adjusted(movement, site_id=ref.site_id, qty_before=before, approved_by=None)
    return movement


MOVE_TARGET_KINDS = (LocationKind.STORAGE, LocationKind.PICK_FACE, LocationKind.QUARANTINE, LocationKind.STAGING)


@transaction.atomic
def move(ref: StockRef, to_location_code: str, qty: Decimal, *, command_id: uuid.UUID | None = None, reason=""):
    if qty <= 0:
        raise Problem(422, "Invalid quantity", "Quantity must be positive.")
    if ledger.already_applied(command_id):
        return InventoryMovement.objects.filter(command_id=command_id).order_by("qty_delta").first()
    source, origin = ref.resolve()
    target = location_at(ref.site_id, to_location_code)
    if target.pk == origin.pk:
        raise Problem(422, "Same bin", "Choose a different destination bin.")
    if target.kind not in MOVE_TARGET_KINDS:
        raise Problem(422, "Not a storage bin", f"{target.code} is a {target.kind} location.")
    if source.status != StockStatus.AVAILABLE and target.kind != LocationKind.QUARANTINE:
        raise Problem(422, "Quarantine bin required", f"{source.status} stock must stay in quarantine bins.")
    out, _ = ledger.transfer(
        source,
        source.at(target),
        qty,
        kind=MovementKind.MOVE,
        command_id=command_id,
        source_type="manual",
        reason=reason.strip(),
    )
    return out
