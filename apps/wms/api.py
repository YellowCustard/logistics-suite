import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from django.conf import settings
from django.db.models import F, Q, QuerySet
from django.http import JsonResponse
from django.utils import timezone
from ninja import File, Router, Schema, Status
from ninja.files import UploadedFile
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from apps.masterdata import services as masterdata
from apps.masterdata.models import Item, Site
from apps.oms import services as oms
from apps.platform import csvimport
from apps.platform.apiutils import Problem, get_scoped, paginate, principal_of, require_roles, require_staff
from apps.platform.auth import Principal
from apps.platform.models import Role

from . import adjustments, allocation, counting, putaway, receiving
from .models import (
    Allocation,
    Condition,
    CountStatus,
    CycleCount,
    InventoryBalance,
    Location,
    LocationKind,
    PutawayTask,
    Receipt,
    ReceiptStatus,
    StockStatus,
    TaskStatus,
)

router = Router(tags=["warehouse"])


class LocationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: Annotated[str, StringConstraints(min_length=1, max_length=30, to_upper=True)]
    zone: str = ""
    kind: LocationKind = LocationKind.STORAGE
    pallet_capacity: Annotated[int, Field(ge=0, le=100)] = 1
    pick_sequence: Annotated[int, Field(ge=0)] = 0


class LocationOut(Schema):
    id: uuid.UUID
    site_id: uuid.UUID
    code: str
    zone: str
    kind: str
    pallet_capacity: int
    pick_sequence: int
    is_active: bool


class LocationPage(Schema):
    items: list[LocationOut]
    next_cursor: uuid.UUID | None


class ImportOut(Schema):
    created: int
    document_id: str


def _site(principal, site_id: uuid.UUID) -> Site:
    require_staff(principal)
    site = Site.objects.filter(pk=site_id).first()
    if site is None or not principal.can_see_site(site.pk):
        raise Problem(404, "Not found", "Unknown site.")
    return site


@router.get("/sites/{site_id}/locations", response=LocationPage)
def list_locations(request, site_id: uuid.UUID, cursor: uuid.UUID | None = None, limit: int = 100):
    site = _site(principal_of(request), site_id)
    items, next_cursor = paginate(Location.objects.filter(site=site), cursor, limit, max_limit=500)
    return {"items": items, "next_cursor": next_cursor}


@router.post("/sites/{site_id}/locations:import", response={201: ImportOut})
def import_locations(request, site_id: uuid.UUID, file: File[UploadedFile]):
    principal = principal_of(request)
    require_roles(principal, Role.SITE_MANAGER)
    site = _site(principal, site_id)

    def create(row: LocationRow) -> Location:
        return Location.objects.create(
            site=site,
            code=row.code,
            zone=(row.zone or "").upper(),
            kind=row.kind,
            pallet_capacity=row.pallet_capacity,
            pick_sequence=row.pick_sequence,
        )

    return Status(201, csvimport.run(file, LocationRow, create, linked_type="masterdata.site", linked_id=str(site.pk)))


# --- Receiving ---------------------------------------------------------------------------------------------

OPERATORS = (Role.WAREHOUSE_OPERATOR, Role.WAREHOUSE_SUPERVISOR, Role.SITE_MANAGER)


def _staff_scoped(principal: Principal, qs: QuerySet) -> QuerySet:
    require_staff(principal)
    return principal.scope(qs, site_field="site_id")


class ReceiptIn(Schema):
    inbound_notice_id: uuid.UUID
    dock_code: str


class ReceiptLineIn(Schema):
    sku: str | None = None
    barcode: str | None = None
    qty: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=3)]
    uom: str = ""
    lot: str = ""
    expiry: date | None = None
    condition: Condition = Condition.GOOD
    lpn: str = ""
    inbound_notice_line_id: uuid.UUID | None = None
    photo_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    command_id: uuid.UUID | None = Field(None, description="Device-generated id; a replay returns the same line")


class ReceiptLineOut(Schema):
    id: uuid.UUID
    inbound_notice_line_id: uuid.UUID | None
    item_id: uuid.UUID
    qty: Decimal
    uom: str
    base_qty: Decimal
    lot: str
    expiry: date | None
    condition: str
    lpn: str


class ReceiptOut(Schema):
    id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    inbound_notice_id: uuid.UUID
    dock_id: uuid.UUID
    status: str
    completed_at: datetime | None
    lines: list[ReceiptLineOut]

    @staticmethod
    def resolve_lines(obj: Receipt):
        return list(obj.lines.order_by("created_at", "id"))


def _receipt_for_operator(principal: Principal, receipt_id: uuid.UUID) -> Receipt:
    require_roles(principal, *OPERATORS)
    return get_scoped(_staff_scoped(principal, Receipt.objects.all()), receipt_id)


@router.post("/receipts", response={201: ReceiptOut})
def open_receipt(request, body: ReceiptIn):
    principal = principal_of(request)
    require_staff(principal)
    require_roles(principal, *OPERATORS)
    notice = oms.expected_notice(body.inbound_notice_id)
    if not (principal.can_see_client(notice.client_id) and principal.can_see_site(notice.site_id)):
        raise Problem(404, "Not found", "Unknown inbound notice.")
    dock = Location.objects.filter(site_id=notice.site_id, code=body.dock_code.strip().upper()).first()
    if dock is None:
        raise Problem(422, "Unknown dock", f"No location {body.dock_code!r} at the notice's site.")
    return Status(201, receiving.open_receipt(inbound_notice_id=notice.inbound_notice_id, dock=dock))


class ReceiptPage(Schema):
    items: list[ReceiptOut]
    next_cursor: uuid.UUID | None


@router.get("/receipts", response=ReceiptPage)
def list_receipts(request, status: ReceiptStatus | None = None, cursor: uuid.UUID | None = None, limit: int = 50):
    qs = _staff_scoped(principal_of(request), Receipt.objects.all())
    if status:
        qs = qs.filter(status=status)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/receipts/{receipt_id}", response=ReceiptOut)
def get_receipt(request, receipt_id: uuid.UUID):
    return get_scoped(_staff_scoped(principal_of(request), Receipt.objects.all()), receipt_id)


@router.post("/receipts/{receipt_id}/lines", response={201: ReceiptLineOut})
def add_receipt_line(request, receipt_id: uuid.UUID, body: ReceiptLineIn):
    receipt = _receipt_for_operator(principal_of(request), receipt_id)
    data = body.dict()
    data["photo_document_ids"] = tuple(data["photo_document_ids"])
    return Status(201, receiving.add_line(receipt.pk, receiving.LineIn(**data)))


@router.post("/receipts/{receipt_id}/complete", response=ReceiptOut)
def complete_receipt(request, receipt_id: uuid.UUID):
    receipt = _receipt_for_operator(principal_of(request), receipt_id)
    return receiving.complete_receipt(receipt.pk)


# --- Tasks ----------------------------------------------------------------------------------------------------


class TaskOut(Schema):
    id: uuid.UUID
    type: str = "putaway"
    site_id: uuid.UUID
    client_id: uuid.UUID
    item_id: uuid.UUID
    sku: str
    lot: str
    expiry: date | None
    qty: Decimal
    from_location: str
    suggested_location: str | None
    status: str
    assigned_to: uuid.UUID | None

    @staticmethod
    def resolve_item_id(obj: PutawayTask):
        return obj.from_balance.item_id

    @staticmethod
    def resolve_sku(obj: PutawayTask):
        return obj.from_balance.item.sku

    @staticmethod
    def resolve_lot(obj: PutawayTask):
        return obj.from_balance.lot

    @staticmethod
    def resolve_expiry(obj: PutawayTask):
        return obj.from_balance.expiry

    @staticmethod
    def resolve_from_location(obj: PutawayTask):
        return obj.from_balance.location.code

    @staticmethod
    def resolve_suggested_location(obj: PutawayTask):
        return obj.suggested_location.code if obj.suggested_location else None


class TaskPage(Schema):
    items: list[TaskOut]
    next_cursor: uuid.UUID | None


class PutawayConfirmIn(Schema):
    to_location_code: str
    reason: str = ""
    command_id: uuid.UUID | None = None


def _tasks(principal: Principal) -> QuerySet:
    qs = PutawayTask.objects.select_related("from_balance__item", "from_balance__location", "suggested_location")
    return _staff_scoped(principal, qs)


@router.get("/tasks", response=TaskPage)
def list_tasks(
    request, mine: bool = False, site_id: uuid.UUID | None = None, cursor: uuid.UUID | None = None, limit: int = 50
):
    principal = principal_of(request)
    require_roles(principal, *OPERATORS)
    qs = _tasks(principal).filter(status__in=[TaskStatus.OPEN, TaskStatus.CLAIMED])
    if mine:
        qs = qs.filter(assigned_to=principal.id)
    if site_id:
        qs = qs.filter(site_id=site_id)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.post("/tasks/{task_id}/claim", response=TaskOut)
def claim_task(request, task_id: uuid.UUID):
    principal = principal_of(request)
    require_roles(principal, *OPERATORS)
    task = get_scoped(_tasks(principal), task_id)
    putaway.claim(task.pk, principal.id)
    return _tasks(principal).get(pk=task.pk)


@router.post("/putaway-tasks/{task_id}/confirm", response=TaskOut)
def confirm_putaway(request, task_id: uuid.UUID, body: PutawayConfirmIn):
    principal = principal_of(request)
    require_roles(principal, *OPERATORS)
    task = get_scoped(_tasks(principal), task_id)
    if task.assigned_to and task.assigned_to != principal.id:
        raise Problem(409, "Task claimed", "Another operator has this task.")
    putaway.confirm(task.pk, to_location_code=body.to_location_code, reason=body.reason, command_id=body.command_id)
    return _tasks(principal).get(pk=task.pk)


# --- Inventory -------------------------------------------------------------------------------------------------


class StockOut(Schema):
    client_id: uuid.UUID
    item_id: uuid.UUID
    sku: str
    site_id: uuid.UUID
    location_code: str
    lot: str
    expiry: date | None
    status: str
    qty_on_hand: Decimal
    qty_allocated: Decimal
    qty_available: Decimal


class StockPage(Schema):
    items: list[StockOut]
    next_cursor: uuid.UUID | None


@router.get("/inventory", response=StockPage)
def list_inventory(
    request,
    item_id: uuid.UUID | None = None,
    sku: str | None = None,
    site_id: uuid.UUID | None = None,
    lot: str | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = 100,
):
    """Stock on hand in base units, one row per location, lot, expiry and status."""
    principal = principal_of(request)
    qs = InventoryBalance.objects.filter(qty_on_hand__gt=0).annotate(
        sku=F("item__sku"),
        site_id=F("location__site_id"),
        location_code=F("location__code"),
        qty_available=F("qty_on_hand") - F("qty_allocated"),
    )
    site_field = None if principal.is_client_scoped else "location__site_id"
    qs = principal.scope(qs, site_field=site_field)
    if item_id:
        qs = qs.filter(item_id=item_id)
    if sku:
        qs = qs.filter(item__sku=sku)
    if site_id:
        qs = qs.filter(location__site_id=site_id)
    if lot is not None:
        qs = qs.filter(lot=lot)
    items, next_cursor = paginate(qs, cursor, limit, max_limit=500)
    return {"items": items, "next_cursor": next_cursor}


# --- Corrections and moves ---------------------------------------------------------------------------------------

SUPERVISORS = (Role.WAREHOUSE_SUPERVISOR, Role.SITE_MANAGER)


class StockRefIn(Schema):
    site_id: uuid.UUID
    location_code: str
    client_id: uuid.UUID | None = Field(None, description="Needed when the SKU or barcode exists for several clients")
    sku: str | None = None
    barcode: str | None = None
    uom: str = ""
    lot: str = ""
    expiry: date | None = None
    status: StockStatus = StockStatus.AVAILABLE
    command_id: uuid.UUID | None = None


class AdjustmentIn(StockRefIn):
    qty_delta: Annotated[Decimal, Field(max_digits=14, decimal_places=3)]
    reason: str


class MoveIn(StockRefIn):
    to_location_code: str
    qty: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=3)]
    reason: str = ""


class MovementOut(Schema):
    id: uuid.UUID
    kind: str
    qty_delta: Decimal
    balance_id: uuid.UUID
    reason: str


def _stock_ref(principal: Principal, body: StockRefIn, roles: tuple[str, ...]) -> tuple[adjustments.StockRef, object]:
    require_staff(principal)
    require_roles(principal, *roles)
    if not principal.can_see_site(body.site_id):
        raise Problem(404, "Not found", "Unknown site.")
    item = adjustments.find_item(client_id=body.client_id, sku=body.sku, barcode=body.barcode)
    if not principal.can_see_client(item.client_id):
        raise Problem(422, "Unknown item", "No such item for your clients.")
    ref = adjustments.StockRef(
        site_id=body.site_id,
        location_code=body.location_code,
        client_id=item.client_id,
        sku=item.sku,
        lot=body.lot,
        expiry=body.expiry,
        status=body.status,
    )
    return ref, item


@router.post("/inventory/adjustments", response={201: MovementOut})
def adjust_stock(request, body: AdjustmentIn):
    ref, item = _stock_ref(principal_of(request), body, SUPERVISORS)
    if body.qty_delta == 0:
        raise Problem(422, "Invalid quantity", "qty_delta must not be zero.")
    sign = 1 if body.qty_delta > 0 else -1
    base = masterdata.to_base_qty(item, abs(body.qty_delta), body.uom or item.base_uom) * sign
    return Status(201, adjustments.adjust(ref, base, reason=body.reason, command_id=body.command_id))


@router.post("/inventory/moves", response={201: MovementOut})
def move_stock(request, body: MoveIn):
    # Moves don't change quantities, so operators can make them; every move is a ledger entry with its author.
    ref, item = _stock_ref(principal_of(request), body, OPERATORS)
    base = masterdata.to_base_qty(item, body.qty, body.uom or item.base_uom)
    movement = adjustments.move(ref, body.to_location_code, base, command_id=body.command_id, reason=body.reason)
    return Status(201, movement)


# --- Cycle counts ---------------------------------------------------------------------------------------------------


class CycleCountIn(Schema):
    site_id: uuid.UUID
    location_codes: list[str] = Field(..., min_length=1, max_length=500)


class CountLineIn(Schema):
    location_code: str
    client_id: uuid.UUID | None = None
    sku: str | None = None
    barcode: str | None = None
    qty: Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=3)]
    uom: str = ""
    lot: str = ""
    expiry: date | None = None
    stock_status: StockStatus = StockStatus.AVAILABLE
    command_id: uuid.UUID | None = None


class BlindLineOut(Schema):
    """What a counter sees: their own finding, never the system quantity."""

    id: uuid.UUID
    location_code: str
    item_id: uuid.UUID
    sku: str
    lot: str
    expiry: date | None
    stock_status: str
    counted_qty: Decimal

    @staticmethod
    def resolve_location_code(obj):
        return obj.location.code

    @staticmethod
    def resolve_sku(obj):
        return obj.item.sku


class CountLineOut(BlindLineOut):
    client_id: uuid.UUID
    system_qty: Decimal
    variance: Decimal
    needs_approval: bool

    @staticmethod
    def resolve_needs_approval(obj):
        return adjustments.needs_approval(obj.system_qty, obj.variance)


class CountDecisionIn(Schema):
    note: str = ""


class _CountBase(Schema):
    id: uuid.UUID
    number: str
    site_id: uuid.UUID
    status: str
    location_codes: list[str]
    submitted_at: datetime | None
    decided_at: datetime | None
    decision_note: str

    @staticmethod
    def resolve_location_codes(obj: CycleCount):
        return sorted(obj.locations.values_list("code", flat=True))


class BlindCountOut(_CountBase):
    lines: list[BlindLineOut]

    @staticmethod
    def resolve_lines(obj: CycleCount):
        return list(obj.lines.select_related("location", "item").filter(counted_by=obj._viewer).order_by("created_at"))


class CycleCountOut(_CountBase):
    lines: list[CountLineOut]

    @staticmethod
    def resolve_lines(obj: CycleCount):
        return list(obj.lines.select_related("location", "item").order_by("location__code", "created_at"))


def _counts(principal: Principal) -> QuerySet:
    require_staff(principal)
    require_roles(principal, *OPERATORS)
    return principal.scope(CycleCount.objects.all(), client_field=None, site_field="site_id")


def _count_view(principal: Principal, count: CycleCount) -> JsonResponse:
    # Serialised here because Ninja can't validate an ORM object against a union of resolver-based schemas;
    # the union in the route signature still documents both shapes.
    if principal.has_role(*SUPERVISORS):
        return JsonResponse(CycleCountOut.from_orm(count).model_dump(mode="json"))
    count._viewer = principal.id
    return JsonResponse(BlindCountOut.from_orm(count).model_dump(mode="json"))


@router.post("/cycle-counts", response={201: CycleCountOut})
def create_cycle_count(request, body: CycleCountIn):
    principal = principal_of(request)
    require_staff(principal)
    require_roles(principal, *SUPERVISORS)
    if not principal.can_see_site(body.site_id):
        raise Problem(404, "Not found", "Unknown site.")
    count = counting.create_count(site_id=body.site_id, location_codes=body.location_codes, created_by=principal.id)
    return Status(201, count)


class CycleCountPage(Schema):
    items: list[_CountBase]
    next_cursor: uuid.UUID | None


@router.get("/cycle-counts", response=CycleCountPage)
def list_cycle_counts(request, status: CountStatus | None = None, cursor: uuid.UUID | None = None, limit: int = 50):
    qs = _counts(principal_of(request))
    if status:
        qs = qs.filter(status=status)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/cycle-counts/{count_id}", response=CycleCountOut | BlindCountOut)
def get_cycle_count(request, count_id: uuid.UUID):
    principal = principal_of(request)
    return _count_view(principal, get_scoped(_counts(principal), count_id))


@router.post("/cycle-counts/{count_id}/lines", response={201: BlindLineOut})
def record_count_line(request, count_id: uuid.UUID, body: CountLineIn):
    principal = principal_of(request)
    count = get_scoped(_counts(principal), count_id)
    item = adjustments.find_item(client_id=body.client_id, sku=body.sku, barcode=body.barcode)
    if not principal.can_see_client(item.client_id):
        raise Problem(422, "Unknown item", "No such item for your clients.")
    data = body.dict() | {"client_id": item.client_id, "sku": item.sku, "barcode": None}
    return Status(201, counting.record_line(count.pk, counted_by=principal.id, **data))


@router.post("/cycle-counts/{count_id}/submit", response=CycleCountOut)
def submit_cycle_count(request, count_id: uuid.UUID):
    principal = principal_of(request)
    require_roles(principal, *SUPERVISORS)
    count = get_scoped(_counts(principal), count_id)
    return counting.submit(count.pk, submitted_by=principal.id)


@router.post("/cycle-counts/{count_id}/approve", response=CycleCountOut)
def approve_cycle_count(request, count_id: uuid.UUID, body: CountDecisionIn):
    principal = principal_of(request)
    require_roles(principal, Role.SITE_MANAGER)
    count = get_scoped(_counts(principal), count_id)
    return counting.decide(count.pk, approve=True, decided_by=principal.id, note=body.note)


@router.post("/cycle-counts/{count_id}/reject", response=CycleCountOut)
def reject_cycle_count(request, count_id: uuid.UUID, body: CountDecisionIn):
    principal = principal_of(request)
    require_roles(principal, Role.SITE_MANAGER)
    count = get_scoped(_counts(principal), count_id)
    return counting.decide(count.pk, approve=False, decided_by=principal.id, note=body.note)


# --- Scanner sync (pull) ----------------------------------------------------------------------------------------


class PullUom(Schema):
    uom: str
    factor: int


class PullBarcode(Schema):
    barcode: str
    uom: str


class PullItem(Schema):
    id: uuid.UUID
    client_id: uuid.UUID
    sku: str
    description: str
    base_uom: str
    lot_tracked: bool
    expiry_tracked: bool
    is_active: bool
    uoms: list[PullUom]
    barcodes: list[PullBarcode]

    @staticmethod
    def resolve_uoms(obj: Item):
        return list(obj.uoms.all())

    @staticmethod
    def resolve_barcodes(obj: Item):
        return list(obj.barcodes.all())


class PullLocation(Schema):
    id: uuid.UUID
    code: str
    kind: str
    is_active: bool


class PullNoticeLine(Schema):
    id: uuid.UUID
    item_id: uuid.UUID
    qty: Decimal
    uom: str
    lot: str
    expiry: date | None


class PullNotice(Schema):
    id: uuid.UUID
    number: str
    client_id: uuid.UUID
    client_ref: str
    status: str
    expected_at: datetime | None
    lines: list[PullNoticeLine]


class PullCount(Schema):
    """Blind: bins only, never system quantities."""

    id: uuid.UUID
    number: str
    status: str
    location_codes: list[str]

    @staticmethod
    def resolve_location_codes(obj: CycleCount):
        return sorted(location.code for location in obj.locations.all())


class PullOut(Schema):
    cursor: datetime = Field(description="Send back as `cursor` on the next pull")
    full: bool
    site_id: uuid.UUID
    locations: list[PullLocation]
    items: list[PullItem]
    notices: list[PullNotice]
    tasks: list[TaskOut]
    counts: list[PullCount]


@router.get("/sync/pull", response=PullOut)
def sync_pull(request, site_id: uuid.UUID, cursor: datetime | None = None):
    """Reference data and open work for one site. Without a cursor it is a full snapshot of what is active or
    open; with one it returns everything changed since (closed and deactivated rows included, so devices
    can drop them). Devices upsert by id. Hard-deleted pack sizes and barcodes only disappear on a full pull,
    so devices do one at sign-in and daily."""
    principal = principal_of(request)
    require_roles(principal, *OPERATORS)
    site = _site(principal, site_id)
    next_cursor = timezone.now()
    since = cursor - timedelta(seconds=settings.SYNC_PULL_OVERLAP_SECONDS) if cursor else None

    locations = Location.objects.filter(site=site)
    items = principal.scope(Item.objects.all(), client_field="client_id").prefetch_related("uoms", "barcodes")
    tasks = _tasks(principal).filter(site_id=site.pk)
    counts = CycleCount.objects.filter(site=site).prefetch_related("locations")
    if since is None:
        locations = locations.filter(is_active=True)
        items = items.filter(is_active=True)
        tasks = tasks.filter(status__in=[TaskStatus.OPEN, TaskStatus.CLAIMED])
        counts = counts.filter(status=CountStatus.OPEN)
    else:
        locations = locations.filter(updated_at__gte=since)
        items = items.filter(
            Q(updated_at__gte=since) | Q(uoms__updated_at__gte=since) | Q(barcodes__updated_at__gte=since)
        ).distinct()
        tasks = tasks.filter(updated_at__gte=since)
        counts = counts.filter(updated_at__gte=since)

    notices = oms.dock_notices(
        site.pk, client_ids=None if principal.all_clients else principal.client_ids, changed_since=since
    )
    return {
        "cursor": next_cursor,
        "full": since is None,
        "site_id": site.pk,
        "locations": list(locations.order_by("code")),
        "items": list(items.order_by("client_id", "sku")),
        "notices": [
            {
                "id": n.inbound_notice_id,
                "number": n.number,
                "client_id": n.client_id,
                "client_ref": n.client_ref,
                "status": n.status,
                "expected_at": n.expected_at,
                "lines": [
                    {
                        "id": ln.line_id,
                        "item_id": ln.item_id,
                        "qty": ln.qty,
                        "uom": ln.uom,
                        "lot": ln.lot,
                        "expiry": ln.expiry,
                    }
                    for ln in n.lines
                ],
            }
            for n in notices
        ],
        "tasks": list(tasks.order_by("created_at")),
        "counts": list(counts.order_by("created_at")),
    }


# --- Allocation ---------------------------------------------------------------------------------------------------


class AllocationRunIn(Schema):
    site_id: uuid.UUID
    sales_order_ids: list[uuid.UUID] | None = Field(
        None, max_length=500, description="Defaults to every backordered order at the site"
    )


class AllocationLineOut(Schema):
    order_line_id: uuid.UUID
    item_id: uuid.UUID
    qty_requested: Decimal
    qty_allocated: Decimal


class AllocationResultOut(Schema):
    sales_order_id: uuid.UUID
    number: str
    fully_allocated: bool
    lines: list[AllocationLineOut]


class AllocationOut(Schema):
    id: uuid.UUID
    sales_order_id: uuid.UUID
    order_line_id: uuid.UUID
    balance_id: uuid.UUID
    location_code: str
    lot: str
    expiry: date | None
    qty_allocated: Decimal
    qty_picked: Decimal
    status: str


@router.post("/allocations:run", response=list[AllocationResultOut])
def run_allocation(request, body: AllocationRunIn):
    """Re-run allocation, e.g. after stock arrives: tops up backordered orders, oldest required-by first."""
    principal = principal_of(request)
    require_staff(principal)
    require_roles(principal, *SUPERVISORS)
    if not principal.can_see_site(body.site_id):
        raise Problem(404, "Not found", "Unknown site.")
    order_ids = body.sales_order_ids
    if order_ids is not None:
        # Only orders at this site for clients the caller can see; others are silently skipped (no id leaks).
        order_ids = [
            order_id
            for order_id in order_ids
            if (order := oms.order_summary(order_id)) is not None
            and order.site_id == body.site_id
            and principal.can_see_client(order.client_id)
        ]
    else:
        order_ids = [
            order_id
            for order_id in oms.orders_to_allocate(body.site_id)
            if principal.can_see_client(oms.order_summary(order_id).client_id)
        ]
    return [
        {
            "sales_order_id": result.sales_order_id,
            "number": result.number,
            "fully_allocated": result.fully_allocated,
            "lines": result.lines,
        }
        for result in allocation.rerun(body.site_id, order_ids)
    ]


@router.get("/allocations", response=list[AllocationOut])
def list_allocations(request, sales_order_id: uuid.UUID):
    """Where an order's stock is reserved, bin by bin."""
    principal = principal_of(request)
    require_staff(principal)
    qs = principal.scope(Allocation.objects.filter(sales_order_id=sales_order_id), site_field="site_id")
    return list(
        qs.annotate(
            location_code=F("balance__location__code"), lot=F("balance__lot"), expiry=F("balance__expiry")
        ).order_by("created_at", "id")
    )
