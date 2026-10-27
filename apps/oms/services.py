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

from .models import (
    CANCELLABLE,
    QTY_STEP,
    InboundLineStatus,
    InboundNotice,
    InboundNoticeLine,
    InboundStatus,
    OrderLine,
    OrderLineStatus,
    OrderStatus,
    SalesOrder,
)


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


# --- Outbound sales orders ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderLineIn:
    qty: Decimal
    uom: str = ""
    sku: str | None = None
    barcode: str | None = None


@dataclass(frozen=True, slots=True)
class ShipToIn:
    """A consignee given inline: reused if this client already has the same name and address."""

    name: str
    address: str
    latitude: Decimal | None = None
    longitude: Decimal | None = None


def resolve_ship_to(client: Party, *, code: str = "", inline: ShipToIn | None = None) -> Site:
    if code.strip():
        return masterdata.client_delivery_address(client.pk, code)
    if inline is not None:
        return masterdata.ensure_delivery_address(
            client.pk, name=inline.name, address=inline.address, latitude=inline.latitude, longitude=inline.longitude
        )
    raise Problem(422, "Delivery address required", "Give ship_to_code or a ship_to name and address.")


@transaction.atomic
def create_sales_order(
    *,
    client: Party,
    site: Site,
    ship_to: Site,
    lines: list[OrderLineIn],
    client_ref: str = "",
    required_by: date | None = None,
    contact_name: str = "",
    contact_phone: str = "",
    delivery_instructions: str = "",
    notes: str = "",
) -> SalesOrder:
    """Validate and accept an order. Stock is checked by allocation, which runs on the accepted event: an
    order that can't be filled is backordered with a reason rather than rejected here, because OMS never
    reads warehouse stock."""
    if site.kind != SiteKind.WAREHOUSE or not site.is_active:
        raise Problem(422, "Not a warehouse", f"{site.code} does not ship stock.")
    if ship_to.kind != SiteKind.DELIVERY_ADDRESS or ship_to.party_id != client.pk or not ship_to.is_active:
        raise Problem(422, "Unknown delivery address", f"{ship_to.code} is not a delivery address of this client.")
    if not lines:
        raise Problem(422, "No lines", "An order needs at least one line.")
    client_ref = client_ref.strip()
    if client_ref and SalesOrder.objects.filter(client=client, client_ref=client_ref).exists():
        raise Problem(409, "Duplicate reference", f"An order with reference {client_ref!r} already exists.")

    resolved = []
    errors = []
    for n, line in enumerate(lines, start=1):
        try:
            item = masterdata.client_item(client.pk, sku=line.sku, barcode=line.barcode)
            uom = (line.uom or item.base_uom).strip().upper()
            if line.qty <= 0:
                raise Problem(422, "Invalid quantity", f"{item.sku}: quantity must be positive.")
            base_qty = masterdata.to_base_qty(item, line.qty, uom).quantize(QTY_STEP)
        except Problem as problem:
            errors.append({"line": n, "title": problem.title, "detail": problem.detail})
            continue
        resolved.append((item, uom, line.qty.quantize(QTY_STEP), base_qty))
    if errors:
        detail = "; ".join(f"line {e['line']}: {e['detail'] or e['title']}" for e in errors)
        raise Problem(422, "Invalid order lines", detail, errors=errors)

    try:
        with transaction.atomic():
            order = SalesOrder.objects.create(
                client=client,
                site=site,
                number=next_number("sales_order", "ORD"),
                client_ref=client_ref,
                ship_to=ship_to,
                required_by=required_by,
                contact_name=contact_name.strip(),
                contact_phone=contact_phone.strip(),
                delivery_instructions=delivery_instructions.strip(),
                notes=notes,
            )
    except IntegrityError as exc:  # a concurrent request with the same client_ref won
        raise Problem(409, "Duplicate reference", f"Reference {client_ref!r} already exists.") from exc
    created = [
        OrderLine.objects.create(order=order, line_no=n, item=item, qty=qty, uom=uom, base_qty=base_qty)
        for n, (item, uom, qty, base_qty) in enumerate(resolved, start=1)
    ]
    outbox.emit(
        "oms.sales_order.accepted",
        aggregate_type="oms.sales_order",
        aggregate_id=order.pk,
        client_id=client.pk,
        site_id=site.pk,
        payload={
            "sales_order_id": str(order.pk),
            "number": order.number,
            "client_ref": order.client_ref,
            "ship_to_site_id": str(ship_to.pk),
            "required_by": required_by.isoformat() if required_by else None,
            "lines": [
                {
                    "line_id": str(line.pk),
                    "item_id": str(line.item_id),
                    "qty": str(line.qty),
                    "uom": line.uom,
                    "base_qty": str(line.base_qty),
                }
                for line in created
            ],
        },
    )
    return order


@transaction.atomic
def cancel_sales_order(order_id: uuid.UUID, reason: str) -> SalesOrder:
    if not reason.strip():
        raise Problem(422, "Reason required", "Say why the order is cancelled.")
    order = SalesOrder.objects.select_for_update().get(pk=order_id)
    if order.status == OrderStatus.CANCELLED:
        return order
    if order.status not in CANCELLABLE:
        raise Problem(409, "Picking has started", f"{order.number} is {order.status} and can't be cancelled.")
    order.status = OrderStatus.CANCELLED
    order.cancelled_reason = reason.strip()
    order.save()
    order.lines.filter(status=OrderLineStatus.OPEN).update(status=OrderLineStatus.CANCELLED)
    outbox.emit(
        "oms.sales_order.cancelled",
        aggregate_type="oms.sales_order",
        aggregate_id=order.pk,
        client_id=order.client_id,
        site_id=order.site_id,
        payload={"sales_order_id": str(order.pk), "number": order.number, "reason": order.cancelled_reason},
    )
    return order


# --- Allocation support for WMS -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenOrderLine:
    line_id: uuid.UUID
    line_no: int
    item_id: uuid.UUID
    base_qty: Decimal


@dataclass(frozen=True, slots=True)
class OrderToAllocate:
    sales_order_id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    status: str
    lines: tuple[OpenOrderLine, ...]

    @property
    def allocatable(self) -> bool:
        return self.status in (OrderStatus.ACCEPTED, OrderStatus.ALLOCATED, OrderStatus.BACKORDERED)


def lock_order_for_allocation(order_id: uuid.UUID) -> OrderToAllocate | None:
    """Called by WMS inside its allocation transaction. The row lock serialises allocation against
    cancellation, so stock is never left reserved for an order that was cancelled meanwhile."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("lock_order_for_allocation() must run inside the allocation transaction")
    order = SalesOrder.objects.select_for_update().filter(pk=order_id).first()
    if order is None:
        return None
    lines = tuple(
        OpenOrderLine(line.pk, line.line_no, line.item_id, line.base_qty)
        for line in order.lines.filter(status=OrderLineStatus.OPEN).order_by("line_no")
    )
    return OrderToAllocate(order.pk, order.number, order.client_id, order.site_id, order.status, lines)


def orders_to_allocate(site_id: uuid.UUID, statuses: tuple[str, ...] = (OrderStatus.BACKORDERED,)) -> list[uuid.UUID]:
    """Ids of a site's orders in `statuses`, oldest required-by first: the queue for an allocation re-run."""
    return list(
        SalesOrder.objects.filter(site_id=site_id, status__in=statuses)
        .order_by("required_by", "created_at", "id")
        .values_list("pk", flat=True)
    )


@transaction.atomic
def record_allocation(order_id: uuid.UUID, *, fully_allocated: bool) -> SalesOrder | None:
    """Apply an allocation result. Ignored once picking has started or the order was cancelled, because
    allocation events can arrive after either."""
    order = SalesOrder.objects.select_for_update().filter(pk=order_id).first()
    if order is None or order.status not in (OrderStatus.ACCEPTED, OrderStatus.ALLOCATED, OrderStatus.BACKORDERED):
        return order
    status = OrderStatus.ALLOCATED if fully_allocated else OrderStatus.BACKORDERED
    if order.status != status:
        order.status = status
        order.save()
    return order


def begin_picking(order_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Called by WMS when it releases a wave: move fully allocated orders to picking, after which they can't
    be cancelled. Returns the ids that moved; orders cancelled or short meanwhile are left out."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("begin_picking() must run inside the wave release transaction")
    started = []
    for order in SalesOrder.objects.select_for_update().filter(pk__in=order_ids).order_by("pk"):
        if order.status == OrderStatus.ALLOCATED:
            order.status = OrderStatus.PICKING
            order.save()
            started.append(order.pk)
    return started


@dataclass(frozen=True, slots=True)
class OrderSummary:
    sales_order_id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    status: str
    ship_to_id: uuid.UUID
    client_ref: str


def order_summary(order_id: uuid.UUID) -> OrderSummary | None:
    order = (
        SalesOrder.objects.filter(pk=order_id)
        .values("pk", "number", "client_id", "site_id", "status", "ship_to_id", "client_ref")
        .first()
    )
    if order is None:
        return None
    return OrderSummary(
        order["pk"],
        order["number"],
        order["client_id"],
        order["site_id"],
        order["status"],
        order["ship_to_id"],
        order["client_ref"],
    )


@transaction.atomic
def record_packed(order_id: uuid.UUID) -> SalesOrder | None:
    """WMS reports that everything picked for the order is packed into closed handling units."""
    order = SalesOrder.objects.select_for_update().filter(pk=order_id).first()
    if order is None or order.status != OrderStatus.PICKING:
        return order
    order.status = OrderStatus.PACKED
    order.save()
    outbox.emit(
        "oms.sales_order.packed",
        aggregate_type="oms.sales_order",
        aggregate_id=order.pk,
        client_id=order.client_id,
        site_id=order.site_id,
        payload={"sales_order_id": str(order.pk), "number": order.number, "client_ref": order.client_ref},
    )
    return order
