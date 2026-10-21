"""Receiving against an inbound notice: open at the dock, scan lines, complete.

Completing posts RECEIVE movements into the dock location, creates one putaway task per line, raises a
variance exception when quantities differ from the notice, and emits wms.receipt.completed.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.masterdata import services as masterdata
from apps.oms import services as oms
from apps.platform import exceptions_queue, outbox
from apps.platform.apiutils import Problem
from apps.platform.context import current_actor
from apps.platform.models import Document, DocumentKind, Severity
from apps.platform.numbers import next_number

from . import ledger, putaway
from .models import (
    Condition,
    Location,
    LocationKind,
    MovementKind,
    Receipt,
    ReceiptLine,
    ReceiptStatus,
    StockStatus,
)


@transaction.atomic
def open_receipt(*, inbound_notice_id: uuid.UUID, dock: Location) -> Receipt:
    notice = oms.begin_receiving(inbound_notice_id)  # locks the notice: concurrent opens queue here
    existing = Receipt.objects.filter(inbound_notice_id=inbound_notice_id).first()
    if existing is not None:
        if existing.status == ReceiptStatus.OPEN and existing.dock_id == dock.pk:
            return existing  # a second scan of the same ASN at the same dock resumes the receipt
        raise Problem(409, "Already received", f"Receipt {existing.number} exists for this inbound notice.")
    if dock.site_id != notice.site_id:
        raise Problem(422, "Wrong site", f"{notice.number} is expected at another site.")
    if dock.kind != LocationKind.DOCK or not dock.is_active:
        raise Problem(422, "Not a dock", f"{dock.code} is not an active dock location.")
    return Receipt.objects.create(
        client_id=notice.client_id,
        site_id=notice.site_id,
        number=next_number("grn", "GRN"),
        inbound_notice_id=notice.inbound_notice_id,
        dock=dock,
    )


@dataclass(frozen=True, slots=True)
class LineIn:
    qty: Decimal
    uom: str = ""
    sku: str | None = None
    barcode: str | None = None
    lot: str = ""
    expiry: date | None = None
    condition: str = Condition.GOOD
    lpn: str = ""
    inbound_notice_line_id: uuid.UUID | None = None
    photo_document_ids: tuple[uuid.UUID, ...] = ()
    command_id: uuid.UUID | None = None


def _match_notice_line(notice: oms.ExpectedNotice, item_id: uuid.UUID, lot: str, wanted: uuid.UUID | None):
    if wanted is not None:
        line = next((x for x in notice.lines if x.line_id == wanted), None)
        if line is None or line.item_id != item_id:
            raise Problem(422, "Line mismatch", "That inbound notice line is not open for this item.")
        return line
    same_item = [x for x in notice.lines if x.item_id == item_id]
    return next((x for x in same_item if x.lot and x.lot == lot), None) or next(iter(same_item), None)


@transaction.atomic
def add_line(receipt_id: uuid.UUID, line: LineIn) -> ReceiptLine:
    if line.command_id is not None:
        replay = ReceiptLine.objects.filter(command_id=line.command_id).first()
        if replay is not None:
            if replay.receipt_id != receipt_id:
                raise Problem(409, "Command reused", "This command_id belongs to another receipt.")
            return replay
    receipt = Receipt.objects.select_for_update().get(pk=receipt_id)
    if receipt.status != ReceiptStatus.OPEN:
        raise Problem(409, "Receipt completed", f"{receipt.number} is closed to new lines.")
    item = masterdata.client_item(receipt.client_id, sku=line.sku, barcode=line.barcode)
    uom = (line.uom or item.base_uom).strip().upper()
    if line.qty <= 0:
        raise Problem(422, "Invalid quantity", "Quantity must be positive.")
    base_qty = masterdata.to_base_qty(item, line.qty, uom)
    lot = line.lot.strip()
    if item.lot_tracked and not lot:
        raise Problem(422, "Lot required", f"{item.sku} is lot-tracked.")
    if item.expiry_tracked and line.expiry is None:
        raise Problem(422, "Expiry required", f"{item.sku} is expiry-tracked.")

    photos = list(
        Document.objects.filter(
            pk__in=line.photo_document_ids, kind=DocumentKind.RECEIPT_PHOTO, client_id=receipt.client_id
        )
    )
    if len(photos) != len(set(line.photo_document_ids)):
        raise Problem(422, "Unknown photo", "Every photo must be a receipt photo uploaded for this client.")
    if line.condition == Condition.DAMAGED and not photos:
        raise Problem(422, "Photo required", "Damaged stock needs at least one photo.")

    notice = oms.expected_notice(receipt.inbound_notice_id)
    matched = _match_notice_line(notice, item.pk, lot, line.inbound_notice_line_id)
    created = ReceiptLine.objects.create(
        receipt=receipt,
        inbound_notice_line_id=matched.line_id if matched else None,
        item=item,
        qty=line.qty,
        uom=uom,
        base_qty=base_qty,
        lot=lot,
        expiry=line.expiry,
        condition=line.condition,
        lpn=line.lpn.strip(),
        command_id=line.command_id,
    )
    for photo in photos:
        if not photo.linked_type:
            photo.linked_type, photo.linked_id = "wms.receiptline", str(created.pk)
            photo.save(update_fields=["linked_type", "linked_id", "updated_at"])
    return created


def _variance(notice: oms.ExpectedNotice, lines: list[ReceiptLine]) -> list[dict]:
    received: dict[uuid.UUID | None, Decimal] = defaultdict(Decimal)
    for line in lines:
        received[line.inbound_notice_line_id] += line.base_qty
    variance = [
        {
            "inbound_notice_line_id": str(expected.line_id),
            "item_id": str(expected.item_id),
            "expected_base_qty": str(expected.base_qty),
            "received_base_qty": str(received.get(expected.line_id, Decimal(0))),
        }
        for expected in notice.lines
        if received.get(expected.line_id, Decimal(0)) != expected.base_qty
    ]
    variance += [
        {
            "inbound_notice_line_id": None,
            "item_id": str(line.item_id),
            "expected_base_qty": "0",
            "received_base_qty": str(line.base_qty),
            "receipt_line_id": str(line.pk),
        }
        for line in lines
        if line.inbound_notice_line_id is None
    ]
    return variance


@transaction.atomic
def complete_receipt(receipt_id: uuid.UUID) -> Receipt:
    receipt = Receipt.objects.select_for_update().select_related("dock").get(pk=receipt_id)
    if receipt.status == ReceiptStatus.COMPLETED:
        return receipt
    lines = list(receipt.lines.select_related("item").order_by("created_at", "id"))
    if not lines:
        raise Problem(422, "Nothing received", "Scan at least one line before completing.")
    notice = oms.expected_notice(receipt.inbound_notice_id)

    for line in lines:
        status = StockStatus.DAMAGED if line.condition == Condition.DAMAGED else StockStatus.AVAILABLE
        key = ledger.StockKey(receipt.client_id, line.item_id, receipt.dock_id, line.lot, line.expiry, status)
        movement = ledger.post(
            key,
            line.base_qty,
            kind=MovementKind.RECEIVE,
            source_type="wms.receiptline",
            source_id=str(line.pk),
        )
        putaway.create_task(receipt_line=line, from_balance=movement.balance)

    variance = _variance(notice, lines)
    receipt.status = ReceiptStatus.COMPLETED
    receipt.completed_at = timezone.now()
    receipt.completed_by = current_actor().id
    receipt.save()

    if variance:
        exceptions_queue.raise_case(
            kind="receipt.variance",
            severity=Severity.MEDIUM,
            summary=f"{receipt.number} ({notice.number}): {len(variance)} line(s) differ from the inbound notice",
            detail={
                "receipt_id": str(receipt.pk),
                "inbound_notice_id": str(notice.inbound_notice_id),
                "variance": variance,
            },
            client_id=receipt.client_id,
            site_id=receipt.site_id,
            entity_type="wms.receipt",
            entity_id=str(receipt.pk),
            dedupe_key=f"receipt.variance:{receipt.pk}",
        )
    outbox.emit(
        "wms.receipt.completed",
        aggregate_type="wms.receipt",
        aggregate_id=receipt.pk,
        client_id=receipt.client_id,
        site_id=receipt.site_id,
        payload={
            "receipt_id": str(receipt.pk),
            "number": receipt.number,
            "inbound_notice_id": str(receipt.inbound_notice_id),
            "lines": [
                {
                    "line_id": str(line.pk),
                    "inbound_notice_line_id": str(line.inbound_notice_line_id) if line.inbound_notice_line_id else None,
                    "item_id": str(line.item_id),
                    "qty": str(line.qty),
                    "uom": line.uom,
                    "base_qty": str(line.base_qty),
                    "lot": line.lot,
                    "expiry": line.expiry.isoformat() if line.expiry else None,
                    "condition": line.condition,
                    "pallets": 1,  # Phase 1: one scanned line is one pallet
                }
                for line in lines
            ],
            "variance": variance,
        },
    )
    return receipt
