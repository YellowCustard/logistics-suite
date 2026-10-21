"""Public interface of the order module. Other modules call these functions; they never touch OMS tables."""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q

from apps.masterdata import services as masterdata
from apps.masterdata.models import Party, Site, SiteKind
from apps.platform import outbox
from apps.platform.apiutils import Problem
from apps.platform.numbers import next_number

from .models import InboundLineStatus, InboundNotice, InboundNoticeLine, InboundStatus


@dataclass(frozen=True, slots=True)
class NoticeLineIn:
    qty: Decimal
    uom: str = ""
    sku: str | None = None
    barcode: str | None = None
    lot: str = ""
    expiry: date | None = None


@transaction.atomic
def create_inbound_notice(
    *,
    client: Party,
    site: Site,
    expected_at: datetime,
    lines: list[NoticeLineIn],
    kind: str = "purchase",
    client_ref: str = "",
    notes: str = "",
) -> InboundNotice:
    if site.kind != SiteKind.WAREHOUSE or not site.is_active:
        raise Problem(422, "Not a warehouse", f"{site.code} does not receive stock.")
    if not lines:
        raise Problem(422, "No lines", "An inbound notice needs at least one line.")
    client_ref = client_ref.strip()
    if client_ref and InboundNotice.objects.filter(client=client, client_ref=client_ref).exists():
        raise Problem(409, "Duplicate reference", f"An inbound notice with reference {client_ref!r} already exists.")

    resolved = []
    for line in lines:
        item = masterdata.client_item(client.pk, sku=line.sku, barcode=line.barcode)
        uom = (line.uom or item.base_uom).strip().upper()
        masterdata.uom_factor(item, uom)  # validates the unit
        if line.qty <= 0:
            raise Problem(422, "Invalid quantity", f"{item.sku}: quantity must be positive.")
        resolved.append((item, uom, line))

    try:
        with transaction.atomic():
            notice = InboundNotice.objects.create(
                client=client,
                site=site,
                number=next_number("asn", "ASN"),
                client_ref=client_ref,
                kind=kind,
                expected_at=expected_at,
                notes=notes,
            )
    except IntegrityError as exc:  # a concurrent request with the same client_ref won
        raise Problem(409, "Duplicate reference", f"Reference {client_ref!r} already exists.") from exc
    created = [
        InboundNoticeLine.objects.create(
            notice=notice, line_no=n, item=item, qty=line.qty, uom=uom, lot=line.lot.strip(), expiry=line.expiry
        )
        for n, (item, uom, line) in enumerate(resolved, start=1)
    ]
    outbox.emit(
        "oms.inbound_notice.accepted",
        aggregate_type="oms.inbound_notice",
        aggregate_id=notice.pk,
        client_id=client.pk,
        site_id=site.pk,
        payload={
            "inbound_notice_id": str(notice.pk),
            "number": notice.number,
            "client_ref": notice.client_ref,
            "kind": notice.kind,
            "expected_at": notice.expected_at.isoformat(),
            "lines": [
                {
                    "line_id": str(line.pk),
                    "item_id": str(line.item_id),
                    "qty": str(line.qty),
                    "uom": line.uom,
                    "lot": line.lot,
                    "expiry": line.expiry.isoformat() if line.expiry else None,
                }
                for line in created
            ],
        },
    )
    return notice


@transaction.atomic
def cancel_inbound_notice(notice_id: uuid.UUID, reason: str) -> InboundNotice:
    if not reason.strip():
        raise Problem(422, "Reason required", "Say why the inbound notice is cancelled.")
    notice = InboundNotice.objects.select_for_update().get(pk=notice_id)
    if notice.status == InboundStatus.CANCELLED:
        return notice
    if notice.status != InboundStatus.ACCEPTED:
        raise Problem(409, "Receiving has started", f"{notice.number} is {notice.status} and can't be cancelled.")
    notice.status = InboundStatus.CANCELLED
    notice.cancelled_reason = reason.strip()
    notice.save()
    for line in notice.lines.filter(status=InboundLineStatus.OPEN):
        line.status = InboundLineStatus.CANCELLED
        line.save()
    outbox.emit(
        "oms.inbound_notice.cancelled",
        aggregate_type="oms.inbound_notice",
        aggregate_id=notice.pk,
        client_id=notice.client_id,
        site_id=notice.site_id,
        payload={"inbound_notice_id": str(notice.pk), "number": notice.number, "reason": notice.cancelled_reason},
    )
    return notice


# --- Receiving support for WMS -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedLine:
    line_id: uuid.UUID
    item_id: uuid.UUID
    qty: Decimal
    uom: str
    base_qty: Decimal
    lot: str
    expiry: date | None


@dataclass(frozen=True, slots=True)
class ExpectedNotice:
    inbound_notice_id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    status: str
    lines: tuple[ExpectedLine, ...]
    client_ref: str = ""
    expected_at: datetime | None = None


def _expected(notice: InboundNotice) -> ExpectedNotice:
    lines = tuple(
        ExpectedLine(
            line_id=line.pk,
            item_id=line.item_id,
            qty=line.qty,
            uom=line.uom,
            base_qty=masterdata.to_base_qty(line.item, line.qty, line.uom),
            lot=line.lot,
            expiry=line.expiry,
        )
        for line in notice.lines.select_related("item").filter(status=InboundLineStatus.OPEN).order_by("line_no")
    )
    return ExpectedNotice(
        notice.pk,
        notice.number,
        notice.client_id,
        notice.site_id,
        notice.status,
        lines,
        client_ref=notice.client_ref,
        expected_at=notice.expected_at,
    )


def dock_notices(
    site_id: uuid.UUID, *, client_ids: frozenset[uuid.UUID] | None, changed_since: datetime | None
) -> list[ExpectedNotice]:
    """The dock plan for scanners. A full read returns notices still to receive; an incremental one returns
    every notice touched since `changed_since`, closed and cancelled included, so devices can drop them.
    `client_ids=None` means every client."""
    qs = InboundNotice.objects.filter(site_id=site_id)
    if client_ids is not None:
        qs = qs.filter(client_id__in=client_ids)
    if changed_since is None:
        qs = qs.filter(status__in=[InboundStatus.ACCEPTED, InboundStatus.RECEIVING])
    else:
        qs = qs.filter(Q(updated_at__gte=changed_since) | Q(lines__updated_at__gte=changed_since)).distinct()
    return [_expected(notice) for notice in qs.order_by("expected_at", "id")]


def expected_notice(notice_id: uuid.UUID) -> ExpectedNotice:
    notice = InboundNotice.objects.filter(pk=notice_id).first()
    if notice is None:
        raise Problem(404, "Not found", "Unknown inbound notice.")
    return _expected(notice)


def begin_receiving(notice_id: uuid.UUID) -> ExpectedNotice:
    """Called by WMS inside the transaction that opens a receipt. The row lock serialises it against
    cancellation, so a notice is never cancelled while it is being received."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("begin_receiving() must run inside the receipt transaction")
    notice = InboundNotice.objects.select_for_update().filter(pk=notice_id).first()
    if notice is None:
        raise Problem(404, "Not found", "Unknown inbound notice.")
    if notice.status not in (InboundStatus.ACCEPTED, InboundStatus.RECEIVING):
        raise Problem(409, "Not receivable", f"{notice.number} is {notice.status}.")
    if notice.status == InboundStatus.ACCEPTED:
        notice.status = InboundStatus.RECEIVING
        notice.save()
    return _expected(notice)


@transaction.atomic
def record_receipt(inbound_notice_id: uuid.UUID, received: list[dict]) -> InboundNotice:
    """Apply a completed receipt: set received quantities and close the notice. Idempotent: re-applying the
    same receipt sets the same values."""
    notice = InboundNotice.objects.select_for_update().get(pk=inbound_notice_id)
    base_by_line: dict[str, Decimal] = defaultdict(Decimal)
    for row in received:
        if row.get("inbound_notice_line_id"):
            base_by_line[row["inbound_notice_line_id"]] += Decimal(row["base_qty"])
    for line in notice.lines.select_related("item").exclude(status=InboundLineStatus.CANCELLED):
        factor = masterdata.uom_factor(line.item, line.uom)
        line.qty_received = base_by_line.get(str(line.pk), Decimal(0)) / factor
        line.status = InboundLineStatus.CLOSED
        line.save()
    if notice.status != InboundStatus.CLOSED:
        notice.status = InboundStatus.CLOSED
        notice.save()
    return notice
