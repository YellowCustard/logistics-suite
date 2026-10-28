import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from ninja import Field, File, Query, Router, Schema, Status
from ninja.files import UploadedFile
from pydantic import BaseModel, ConfigDict, StringConstraints

from apps.masterdata import services as masterdata
from apps.masterdata.models import Site
from apps.platform import csvimport
from apps.platform.apiutils import Problem, acting_client_id, get_scoped, paginate, principal_of, require_roles
from apps.platform.auth import Principal
from apps.platform.models import AuditEntry, Role

from . import services
from .models import CANCELLABLE, InboundKind, InboundNotice, InboundStatus, OrderStatus, SalesOrder

router = Router(tags=["orders"])

STAFF_WRITERS = (Role.SITE_MANAGER, Role.WAREHOUSE_SUPERVISOR)


class InboundLineIn(Schema):
    sku: str | None = None
    barcode: str | None = None
    qty: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=3)]
    uom: str = ""
    lot: str = ""
    expiry: date | None = None


class InboundNoticeIn(Schema):
    client_id: uuid.UUID | None = Field(None, description="Staff only; client systems always act for themselves")
    site_code: str
    expected_at: datetime
    kind: InboundKind = InboundKind.PURCHASE
    client_ref: str = Field("", max_length=60)
    notes: str = ""
    lines: list[InboundLineIn] = Field(..., min_length=1, max_length=500)


class InboundLineOut(Schema):
    id: uuid.UUID
    line_no: int
    item_id: uuid.UUID
    sku: str
    qty: Decimal
    uom: str
    lot: str
    expiry: date | None
    qty_received: Decimal
    status: str

    @staticmethod
    def resolve_sku(obj) -> str:
        return obj.item.sku


class InboundNoticeOut(Schema):
    id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    client_ref: str
    kind: str
    status: str
    expected_at: datetime
    notes: str
    cancelled_reason: str
    created_at: datetime
    lines: list[InboundLineOut]

    @staticmethod
    def resolve_lines(obj: InboundNotice):
        return list(obj.lines.select_related("item").order_by("line_no"))


class InboundNoticeSummary(Schema):
    id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    client_ref: str
    kind: str
    status: str
    expected_at: datetime


class InboundNoticePage(Schema):
    items: list[InboundNoticeSummary]
    next_cursor: uuid.UUID | None


class CancelIn(Schema):
    reason: str


def _notices(principal: Principal) -> QuerySet:
    # Staff are limited to their sites as well as their clients; a client sees its notices at every site.
    return principal.scope(InboundNotice.objects.all(), site_field=None if principal.is_client_scoped else "site_id")


def _site_for(principal: Principal, code: str) -> Site:
    site = Site.objects.filter(code=code.strip().upper(), is_active=True).first()
    if site is None or (not principal.is_client_scoped and not principal.can_see_site(site.pk)):
        raise Problem(422, "Unknown site", f"No site {code!r}.")
    return site


@router.post("/inbound-notices", response={201: InboundNoticeOut})
def create_inbound_notice(request, body: InboundNoticeIn):
    principal = principal_of(request)
    if principal.is_client_scoped and principal.kind == "user":
        require_roles(principal, Role.CLIENT_ADMIN, Role.CLIENT_USER)
    client = masterdata.require_client(acting_client_id(principal, body.client_id, *STAFF_WRITERS))
    notice = services.create_inbound_notice(
        client=client,
        site=_site_for(principal, body.site_code),
        expected_at=body.expected_at,
        kind=body.kind,
        client_ref=body.client_ref,
        notes=body.notes,
        lines=[services.NoticeLineIn(**line.dict()) for line in body.lines],
    )
    return Status(201, notice)


class AsnRow(BaseModel):
    """One CSV row is one ASN line; rows sharing a client_ref make one ASN."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_ref: Annotated[str, StringConstraints(min_length=1, max_length=60)]
    site_code: Annotated[str, StringConstraints(min_length=1, to_upper=True)]
    expected_at: datetime
    sku: str | None = None
    barcode: str | None = None
    qty: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=3)]
    uom: str = ""
    lot: str = ""
    expiry: date | None = None
    kind: InboundKind = InboundKind.PURCHASE
    notes: str = ""


class ImportedNotice(Schema):
    id: uuid.UUID
    number: str
    client_ref: str


class AsnImportOut(Schema):
    created: int
    document_id: str
    notices: list[ImportedNotice]


@router.post("/inbound-notices:import", response={201: AsnImportOut})
def import_inbound_notices(request, file: File[UploadedFile], client_id: uuid.UUID | None = None):
    """Upload ASNs as CSV (all or nothing). Columns: client_ref, site_code, expected_at, sku or barcode, qty,
    and optionally uom, lot, expiry, kind, notes. Staff name the client with `?client_id=`."""
    principal = principal_of(request)
    if principal.is_client_scoped and principal.kind == "user":
        require_roles(principal, Role.CLIENT_ADMIN, Role.CLIENT_USER)
    client = masterdata.require_client(acting_client_id(principal, client_id, *STAFF_WRITERS))

    def create(rows: list[AsnRow]) -> InboundNotice:
        head = rows[0]
        header = (head.site_code, head.expected_at, head.kind, head.notes)
        if any((r.site_code, r.expected_at, r.kind, r.notes) != header for r in rows[1:]):
            raise Problem(
                422, "Inconsistent ASN", f"Rows for {head.client_ref!r} must share site_code, expected_at, kind, notes."
            )
        return services.create_inbound_notice(
            client=client,
            site=_site_for(principal, head.site_code),
            expected_at=head.expected_at,
            kind=head.kind,
            client_ref=head.client_ref,
            notes=head.notes,
            lines=[
                services.NoticeLineIn(sku=r.sku, barcode=r.barcode, qty=r.qty, uom=r.uom, lot=r.lot, expiry=r.expiry)
                for r in rows
            ],
        )

    result, notices = csvimport.run_groups(
        file, AsnRow, lambda row: row.client_ref, create, client_id=client.pk, linked_type="oms.import"
    )
    return Status(201, {"created": result.created, "document_id": result.document_id, "notices": notices})


@router.get("/inbound-notices", response=InboundNoticePage)
def list_inbound_notices(
    request,
    status: InboundStatus | None = None,
    site_id: uuid.UUID | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = 50,
):
    qs = _notices(principal_of(request))
    if status:
        qs = qs.filter(status=status)
    if site_id:
        qs = qs.filter(site_id=site_id)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/inbound-notices/{notice_id}", response=InboundNoticeOut)
def get_inbound_notice(request, notice_id: uuid.UUID):
    return get_scoped(_notices(principal_of(request)), notice_id)


@router.post("/inbound-notices/{notice_id}/cancel", response=InboundNoticeOut)
def cancel_inbound_notice(request, notice_id: uuid.UUID, body: CancelIn):
    principal = principal_of(request)
    if not principal.is_client_scoped:
        require_roles(principal, *STAFF_WRITERS)
    elif principal.kind == "user":
        require_roles(principal, Role.CLIENT_ADMIN, Role.CLIENT_USER)
    notice = get_scoped(_notices(principal), notice_id)
    return services.cancel_inbound_notice(notice.pk, body.reason)


# --- Outbound sales orders ------------------------------------------------------------------------------------


class OrderLineIn(Schema):
    sku: str | None = None
    barcode: str | None = None
    qty: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=3)]
    uom: str = ""


class ShipToIn(Schema):
    name: str = Field(..., max_length=120)
    address: str
    latitude: Annotated[Decimal, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[Decimal, Field(ge=-180, le=180)] | None = None


class SalesOrderIn(Schema):
    client_id: uuid.UUID | None = Field(None, description="Staff only; client systems always act for themselves")
    site_code: str = Field(..., description="Shipping warehouse")
    client_ref: str = Field("", max_length=60)
    ship_to_code: str = Field("", description="An existing delivery address; or give ship_to instead")
    ship_to: ShipToIn | None = None
    required_by: date | None = None
    contact_name: str = Field("", max_length=120)
    contact_phone: str = Field("", max_length=30)
    delivery_instructions: str = ""
    notes: str = ""
    lines: list[OrderLineIn] = Field(..., min_length=1, max_length=500)


class OrderLineOut(Schema):
    id: uuid.UUID
    line_no: int
    item_id: uuid.UUID
    sku: str
    qty: Decimal
    uom: str
    base_qty: Decimal
    status: str

    @staticmethod
    def resolve_sku(obj) -> str:
        return obj.item.sku


class ShipToOut(Schema):
    id: uuid.UUID
    code: str
    name: str
    address: str


class SalesOrderOut(Schema):
    id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    client_ref: str
    ship_to: ShipToOut
    required_by: date | None
    status: str
    can_cancel: bool = Field(..., description="Cancellation is possible until picking starts")
    contact_name: str
    contact_phone: str
    delivery_instructions: str
    notes: str
    cancelled_reason: str
    created_at: datetime
    lines: list[OrderLineOut]

    @staticmethod
    def resolve_can_cancel(obj: SalesOrder) -> bool:
        return obj.status in CANCELLABLE

    @staticmethod
    def resolve_lines(obj: SalesOrder):
        return list(obj.lines.select_related("item").order_by("line_no"))


class SalesOrderSummary(Schema):
    id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    client_ref: str
    ship_to_id: uuid.UUID
    ship_to_name: str
    required_by: date | None
    status: str
    created_at: datetime

    @staticmethod
    def resolve_ship_to_name(obj) -> str:
        return obj.ship_to.name


class SalesOrderPage(Schema):
    items: list[SalesOrderSummary]
    next_cursor: uuid.UUID | None


def _orders(principal: Principal) -> QuerySet:
    return principal.scope(SalesOrder.objects.all(), site_field=None if principal.is_client_scoped else "site_id")


def _order_client(principal: Principal, client_id: uuid.UUID | None):
    if principal.is_client_scoped and principal.kind == "user":
        require_roles(principal, Role.CLIENT_ADMIN, Role.CLIENT_USER)
    return masterdata.require_client(acting_client_id(principal, client_id, *STAFF_WRITERS))


@router.post("/orders", response={201: SalesOrderOut})
def create_order(request, body: SalesOrderIn):
    """Place an outbound order. Give the delivery address as `ship_to_code` (an address on file) or as an inline
    `ship_to`, which is reused when the same name and address come again. Stock is allocated right after
    acceptance; an order that can't be filled becomes `backordered`."""
    principal = principal_of(request)
    client = _order_client(principal, body.client_id)
    ship_to = services.resolve_ship_to(
        client,
        code=body.ship_to_code,
        inline=services.ShipToIn(**body.ship_to.dict()) if body.ship_to else None,
    )
    order = services.create_sales_order(
        client=client,
        site=_site_for(principal, body.site_code),
        ship_to=ship_to,
        client_ref=body.client_ref,
        required_by=body.required_by,
        contact_name=body.contact_name,
        contact_phone=body.contact_phone,
        delivery_instructions=body.delivery_instructions,
        notes=body.notes,
        lines=[services.OrderLineIn(**line.dict()) for line in body.lines],
    )
    return Status(201, order)


class OrderRow(BaseModel):
    """One CSV row is one order line; rows sharing a client_ref make one order."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_ref: Annotated[str, StringConstraints(min_length=1, max_length=60)]
    site_code: Annotated[str, StringConstraints(min_length=1, to_upper=True)]
    ship_to_code: str = ""
    ship_to_name: str = ""
    ship_to_address: str = ""
    required_by: date | None = None
    sku: str | None = None
    barcode: str | None = None
    qty: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=3)]
    uom: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    delivery_instructions: str = ""
    notes: str = ""

    def header(self) -> tuple:
        return (
            self.site_code,
            self.ship_to_code,
            self.ship_to_name,
            self.ship_to_address,
            self.required_by,
            self.contact_name,
            self.contact_phone,
            self.delivery_instructions,
            self.notes,
        )


class ImportedOrder(Schema):
    id: uuid.UUID
    number: str
    client_ref: str


class OrderImportOut(Schema):
    created: int
    document_id: str
    orders: list[ImportedOrder]


@router.post("/orders:import", response={201: OrderImportOut})
def import_orders(request, file: File[UploadedFile], client_id: uuid.UUID | None = None):
    """Upload orders as CSV (all or nothing). Columns: client_ref, site_code, ship_to_code (or ship_to_name and
    ship_to_address), sku or barcode, qty, and optionally uom, required_by, contact_name, contact_phone,
    delivery_instructions, notes. Rows sharing a client_ref make one order and must agree on everything but
    the line columns. Staff name the client with `?client_id=`."""
    principal = principal_of(request)
    client = _order_client(principal, client_id)

    def create(rows: list[OrderRow]) -> SalesOrder:
        head = rows[0]
        if any(r.header() != head.header() for r in rows[1:]):
            raise Problem(
                422,
                "Inconsistent order",
                f"Rows for {head.client_ref!r} must agree on everything but sku, qty and uom.",
            )
        inline = (
            services.ShipToIn(name=head.ship_to_name, address=head.ship_to_address)
            if head.ship_to_name or head.ship_to_address
            else None
        )
        return services.create_sales_order(
            client=client,
            site=_site_for(principal, head.site_code),
            ship_to=services.resolve_ship_to(client, code=head.ship_to_code, inline=inline),
            client_ref=head.client_ref,
            required_by=head.required_by,
            contact_name=head.contact_name,
            contact_phone=head.contact_phone,
            delivery_instructions=head.delivery_instructions,
            notes=head.notes,
            lines=[services.OrderLineIn(sku=r.sku, barcode=r.barcode, qty=r.qty, uom=r.uom) for r in rows],
        )

    result, orders = csvimport.run_groups(
        file, OrderRow, lambda row: row.client_ref, create, client_id=client.pk, linked_type="oms.import"
    )
    return Status(201, {"created": result.created, "document_id": result.document_id, "orders": orders})


@router.get("/orders", response=SalesOrderPage)
def list_orders(
    request,
    status: OrderStatus | None = None,
    site_id: uuid.UUID | None = None,
    required_by: date | None = None,
    q: Annotated[str, Query(max_length=60, description="Order number or your reference, or the start of it")] = "",
    newest_first: bool = False,
    cursor: uuid.UUID | None = None,
    limit: int = 50,
):
    qs = _orders(principal_of(request)).select_related("ship_to")
    if status:
        qs = qs.filter(status=status)
    if site_id:
        qs = qs.filter(site_id=site_id)
    if required_by:
        qs = qs.filter(required_by__lte=required_by)
    if q.strip():
        term = q.strip()
        qs = qs.filter(Q(number__istartswith=term) | Q(client_ref__istartswith=term))
    items, next_cursor = paginate(qs, cursor, limit, newest_first=newest_first)
    return {"items": items, "next_cursor": next_cursor}


# Finished orders count only when they finished recently; open ones count however old they are.
RECENT_DAYS = 7
FINISHED = (OrderStatus.DELIVERED, OrderStatus.CANCELLED)


class OrderCountsOut(Schema):
    counts: dict[str, int] = Field(..., description="Every status, zero included")
    finished_since: datetime = Field(..., description="Delivered and cancelled orders count from this time")


@router.get("/orders/status-counts", response=OrderCountsOut)
def order_status_counts(request, site_id: uuid.UUID | None = None):
    """Orders by status: every open order, plus orders delivered or cancelled in the last 7 days."""
    since = timezone.now() - timedelta(days=RECENT_DAYS)
    qs = _orders(principal_of(request)).filter(~Q(status__in=FINISHED) | Q(updated_at__gte=since))
    if site_id:
        qs = qs.filter(site_id=site_id)
    counts = dict.fromkeys(OrderStatus.values, 0)
    counts.update(dict(qs.order_by().values_list("status").annotate(n=Count("id"))))
    return {"counts": counts, "finished_since": since}


class OrderEventOut(Schema):
    at: datetime
    status: str
    detail: str = ""


def _status_history(order: SalesOrder) -> list[dict]:
    """Status changes from the audit log: every order change is audited, so this is the complete history."""
    entries = AuditEntry.objects.filter(entity_type="oms.salesorder", entity_id=str(order.pk)).order_by(
        "occurred_at", "id"
    )
    events, last = [], None
    for entry in entries.only("occurred_at", "after"):
        status = (entry.after or {}).get("status")
        if status and status != last:
            detail = order.cancelled_reason if status == OrderStatus.CANCELLED else ""
            events.append({"at": entry.occurred_at, "status": status, "detail": detail})
            last = status
    return events


@router.get("/orders/{order_id}/timeline", response=list[OrderEventOut])
def order_timeline(request, order_id: uuid.UUID):
    """The order's status changes, oldest first. Delivery detail (ETA, proof of delivery) is on its shipment:
    `GET /shipments?sales_order_id=`."""
    return _status_history(get_scoped(_orders(principal_of(request)), order_id))


@router.get("/orders/{order_id}", response=SalesOrderOut)
def get_order(request, order_id: uuid.UUID):
    return get_scoped(_orders(principal_of(request)).select_related("ship_to"), order_id)


@router.post("/orders/{order_id}/cancel", response=SalesOrderOut)
def cancel_order(request, order_id: uuid.UUID, body: CancelIn):
    """Cancel an order until picking starts."""
    principal = principal_of(request)
    if not principal.is_client_scoped:
        require_roles(principal, *STAFF_WRITERS)
    elif principal.kind == "user":
        require_roles(principal, Role.CLIENT_ADMIN, Role.CLIENT_USER)
    order = get_scoped(_orders(principal), order_id)
    return services.cancel_sales_order(order.pk, body.reason)
