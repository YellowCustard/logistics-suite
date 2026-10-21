"""Blind cycle counts.

Operators record what they find; they never see system quantities. On submit, bins that held stock but were
not counted are recorded as counted zero. Variances within INVENTORY_VARIANCE_APPROVAL_PCT post at once;
otherwise a site manager who took no part in the count approves (posting the adjustments) or rejects.
"""

import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.masterdata import services as masterdata
from apps.platform.apiutils import Problem
from apps.platform.numbers import next_number

from . import adjustments, ledger
from .models import (
    QTY_STEP,
    CountStatus,
    CycleCount,
    CycleCountLine,
    InventoryBalance,
    Location,
    MovementKind,
    StockStatus,
)


@transaction.atomic
def create_count(*, site_id: uuid.UUID, location_codes: list[str], created_by: uuid.UUID | None) -> CycleCount:
    codes = sorted({c.strip().upper() for c in location_codes if c.strip()})
    if not codes:
        raise Problem(422, "No bins", "Name at least one bin to count.")
    locations = list(Location.objects.filter(site_id=site_id, code__in=codes, is_active=True))
    missing = sorted(set(codes) - {loc.code for loc in locations})
    if missing:
        raise Problem(422, "Unknown bin", f"Not active bins at this site: {', '.join(missing)}.")
    busy = (
        CycleCount.objects.filter(status__in=[CountStatus.OPEN, CountStatus.AWAITING_APPROVAL], locations__in=locations)
        .values_list("number", flat=True)
        .distinct()
    )
    if busy:
        raise Problem(409, "Bins already being counted", f"See {', '.join(busy)}.")
    count = CycleCount.objects.create(site_id=site_id, number=next_number("cc", "CC"), created_by=created_by)
    count.locations.set(locations)
    return count


@transaction.atomic
def record_line(
    count_id: uuid.UUID,
    *,
    location_code: str,
    qty: Decimal,
    counted_by: uuid.UUID,
    client_id: uuid.UUID | None = None,
    sku: str | None = None,
    barcode: str | None = None,
    uom: str = "",
    lot: str = "",
    expiry=None,
    stock_status: str = StockStatus.AVAILABLE,
    command_id: uuid.UUID | None = None,
) -> CycleCountLine:
    if command_id is not None:
        replay = CycleCountLine.objects.filter(command_ids__contains=[command_id]).first()
        if replay is not None:
            return replay
    count = CycleCount.objects.select_for_update().get(pk=count_id)
    if count.status != CountStatus.OPEN:
        raise Problem(409, "Count closed", f"{count.number} is {count.status}.")
    location = count.locations.filter(code=location_code.strip().upper()).first()
    if location is None:
        raise Problem(422, "Bin not in this count", f"{location_code!r} is not part of {count.number}.")
    if qty < 0:
        raise Problem(422, "Invalid quantity", "Counted quantity can't be negative.")
    item = adjustments.find_item(client_id=client_id, sku=sku, barcode=barcode)
    base_qty = masterdata.to_base_qty(item, qty, uom or item.base_uom).quantize(QTY_STEP)
    key = ledger.StockKey(item.client_id, item.pk, location.pk, lot.strip(), expiry, stock_status)
    line, _ = CycleCountLine.objects.update_or_create(
        count=count,
        location=location,
        client_id=item.client_id,
        item=item,
        lot=key.lot,
        expiry=expiry,
        stock_status=stock_status,
        defaults={
            "counted_qty": base_qty,
            "system_qty": adjustments.on_hand(key),  # a recount refreshes the snapshot too
            "counted_by": counted_by,
        },
    )
    if command_id is not None:
        line.command_ids.append(command_id)
        line.save(update_fields=["command_ids", "updated_at"])
    return line


def _post(count: CycleCount, approved_by: uuid.UUID | None) -> None:
    for line in count.lines.exclude(counted_qty=F("system_qty")).select_related("location"):
        key = ledger.StockKey(line.client_id, line.item_id, line.location_id, line.lot, line.expiry, line.stock_status)
        before = adjustments.on_hand(key)
        movement = ledger.post(
            key,
            line.variance,
            kind=MovementKind.ADJUST,
            reason=f"Cycle count {count.number}",
            source_type="wms.cyclecount",
            source_id=str(count.pk),
        )
        adjustments.emit_adjusted(movement, site_id=count.site_id, qty_before=before, approved_by=approved_by)


@transaction.atomic
def submit(count_id: uuid.UUID, *, submitted_by: uuid.UUID) -> CycleCount:
    count = CycleCount.objects.select_for_update().get(pk=count_id)
    if count.status != CountStatus.OPEN:
        raise Problem(409, "Count not open", f"{count.number} is {count.status}.")
    counted = {
        (line.location_id, line.client_id, line.item_id, line.lot, line.expiry, line.stock_status)
        for line in count.lines.all()
    }
    for balance in InventoryBalance.objects.filter(location__in=count.locations.all(), qty_on_hand__gt=0):
        dims = (balance.location_id, balance.client_id, balance.item_id, balance.lot, balance.expiry, balance.status)
        if dims not in counted:  # stock the counters did not find counts as zero
            CycleCountLine.objects.create(
                count=count,
                location_id=balance.location_id,
                client_id=balance.client_id,
                item_id=balance.item_id,
                lot=balance.lot,
                expiry=balance.expiry,
                stock_status=balance.status,
                counted_qty=0,
                system_qty=balance.qty_on_hand,
            )
    if not count.lines.exists():
        raise Problem(422, "Nothing counted", "The bins hold no stock and nothing was recorded.")
    count.submitted_by = submitted_by
    count.submitted_at = timezone.now()
    if any(adjustments.needs_approval(line.system_qty, line.variance) for line in count.lines.all()):
        count.status = CountStatus.AWAITING_APPROVAL
    else:
        _post(count, approved_by=None)
        count.status = CountStatus.POSTED
        count.decision_note = "Within tolerance; posted on submit"
        count.decided_at = timezone.now()
    count.save()
    return count


def _participants(count: CycleCount) -> set[uuid.UUID]:
    people = set(count.lines.exclude(counted_by=None).values_list("counted_by", flat=True))
    return people | ({count.submitted_by} if count.submitted_by else set())


@transaction.atomic
def decide(count_id: uuid.UUID, *, approve: bool, decided_by: uuid.UUID, note: str = "") -> CycleCount:
    count = CycleCount.objects.select_for_update().get(pk=count_id)
    if count.status != CountStatus.AWAITING_APPROVAL:
        raise Problem(409, "Nothing to approve", f"{count.number} is {count.status}.")
    if decided_by in _participants(count):
        raise Problem(403, "Separation of duties", "Someone who counted or submitted can't approve this count.")
    if not approve and not note.strip():
        raise Problem(422, "Reason required", "Say why the count is rejected.")
    if approve:
        _post(count, approved_by=decided_by)
    count.status = CountStatus.POSTED if approve else CountStatus.REJECTED
    count.decided_by = decided_by
    count.decided_at = timezone.now()
    count.decision_note = note.strip()
    count.save()
    return count
