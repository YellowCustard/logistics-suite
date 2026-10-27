import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from django.conf import settings
from django.db.models import F, Q, QuerySet
from django.http import HttpResponse, JsonResponse
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

from . import adjustments, allocation, counting, labels, packing, picking, putaway, receiving
from .models import (
    Allocation,
    Condition,
    CountStatus,
    CycleCount,
    HandlingUnit,
    HandlingUnitKind,
    HandlingUnitStatus,
    InventoryBalance,
    Location,
    LocationKind,
    PickTask,
    PickTaskStatus,
    PickWave,
    PutawayTask,
    Receipt,
    ReceiptStatus,
    StockStatus,
    TaskStatus,
    WaveStatus,
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


class PickTaskOut(Schema):
    id: uuid.UUID
    type: str = "pick"
    site_id: uuid.UUID
    client_id: uuid.UUID
    wave_id: uuid.UUID
    wave_number: str
    sales_order_id: uuid.UUID
    item_id: uuid.UUID
    sku: str
    lot: str
    expiry: date | None
    qty: Decimal
    qty_picked: Decimal
    from_location: str
    pick_sequence: int
    status: str
    assigned_to: uuid.UUID | None
    short_reason: str

    @staticmethod
    def resolve_wave_number(obj: PickTask):
        return obj.wave.number

    @staticmethod
    def resolve_sku(obj: PickTask):
        return obj.item.sku

    @staticmethod
    def resolve_lot(obj: PickTask):
        return obj.allocation.balance.lot

    @staticmethod
    def resolve_expiry(obj: PickTask):
        return obj.allocation.balance.expiry

    @staticmethod
    def resolve_from_location(obj: PickTask):
        return obj.from_location.code

    @staticmethod
    def resolve_pick_sequence(obj: PickTask):
        return obj.from_location.pick_sequence


def _pick_tasks(principal: Principal) -> QuerySet:
    qs = PickTask.objects.select_related("wave", "item", "from_location", "allocation__balance")
    return _staff_scoped(principal, qs)


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


@router.post("/tasks/{task_id}/claim", response=TaskOut | PickTaskOut)
def claim_task(request, task_id: uuid.UUID):
    """Claim a putaway or a pick task."""
    principal = principal_of(request)
    require_roles(principal, *OPERATORS)
    if _pick_tasks(principal).filter(pk=task_id).exists():
        picking.claim(task_id, principal.id)
        return _pick_tasks(principal).get(pk=task_id)
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
    pick_tasks: list[PickTaskOut]
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
    pick_tasks = _pick_tasks(principal).filter(site_id=site.pk)
    counts = CycleCount.objects.filter(site=site).prefetch_related("locations")
    if since is None:
        locations = locations.filter(is_active=True)
        items = items.filter(is_active=True)
        tasks = tasks.filter(status__in=[TaskStatus.OPEN, TaskStatus.CLAIMED])
        pick_tasks = pick_tasks.filter(status__in=[PickTaskStatus.OPEN, PickTaskStatus.CLAIMED])
        counts = counts.filter(status=CountStatus.OPEN)
    else:
        locations = locations.filter(updated_at__gte=since)
        items = items.filter(
            Q(updated_at__gte=since) | Q(uoms__updated_at__gte=since) | Q(barcodes__updated_at__gte=since)
        ).distinct()
        tasks = tasks.filter(updated_at__gte=since)
        pick_tasks = pick_tasks.filter(updated_at__gte=since)
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
        "pick_tasks": list(pick_tasks.order_by("from_location__pick_sequence", "from_location__code", "created_at")),
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


# --- Waves and picking --------------------------------------------------------------------------------------------


class WaveIn(Schema):
    site_id: uuid.UUID
    sales_order_ids: list[uuid.UUID] = Field(min_length=1, max_length=picking.MAX_WAVE_ORDERS)


class WaveOrderOut(Schema):
    sales_order_id: uuid.UUID
    number: str
    client_id: uuid.UUID


class WaveOut(Schema):
    id: uuid.UUID
    number: str
    site_id: uuid.UUID
    status: str
    orders: list[WaveOrderOut]
    tasks_total: int
    tasks_open: int
    tasks_short: int
    released_at: datetime | None
    completed_at: datetime | None

    @staticmethod
    def resolve_orders(obj: PickWave):
        return list(obj.orders.order_by("number"))

    @staticmethod
    def resolve_tasks_total(obj: PickWave):
        return obj.tasks.count()

    @staticmethod
    def resolve_tasks_open(obj: PickWave):
        return obj.tasks.filter(status__in=[PickTaskStatus.OPEN, PickTaskStatus.CLAIMED]).count()

    @staticmethod
    def resolve_tasks_short(obj: PickWave):
        return obj.tasks.filter(status=PickTaskStatus.SHORT).count()


class WavePage(Schema):
    items: list[WaveOut]
    next_cursor: uuid.UUID | None


class PickTaskPage(Schema):
    items: list[PickTaskOut]
    next_cursor: uuid.UUID | None


class PickConfirmIn(Schema):
    location_code: str
    item_code: str = Field(description="Item barcode or SKU, as scanned")
    qty: Decimal = Field(gt=0, description="Base units")
    command_id: uuid.UUID | None = None


class PickShortIn(Schema):
    location_code: str
    qty_found: Decimal = Field(ge=0, description="Base units actually picked")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    command_id: uuid.UUID | None = None


def _waves(principal: Principal) -> QuerySet:
    require_staff(principal)
    return principal.scope(PickWave.objects.all(), client_field=None, site_field="site_id")


@router.post("/waves", response={201: WaveOut})
def plan_wave(request, body: WaveIn):
    """Plan a wave from allocated orders at one site. Nothing reaches the floor until it is released."""
    principal = principal_of(request)
    require_staff(principal)
    require_roles(principal, *SUPERVISORS)
    site = _site(principal, body.site_id)
    hidden = [
        order_id
        for order_id in body.sales_order_ids
        if (order := oms.order_summary(order_id)) is None or not principal.can_see_client(order.client_id)
    ]
    if hidden:
        raise Problem(422, "Orders can't be waved", f"{len(hidden)} unknown order(s).")
    wave = picking.plan_wave(site_id=site.pk, order_ids=body.sales_order_ids, created_by=principal.id)
    return Status(201, wave)


@router.get("/waves", response=WavePage)
def list_waves(
    request,
    site_id: uuid.UUID | None = None,
    status: WaveStatus | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = 50,
):
    qs = _waves(principal_of(request))
    if site_id:
        qs = qs.filter(site_id=site_id)
    if status:
        qs = qs.filter(status=status)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/waves/{wave_id}", response=WaveOut)
def get_wave(request, wave_id: uuid.UUID):
    return get_scoped(_waves(principal_of(request)), wave_id)


@router.post("/waves/{wave_id}/release", response=WaveOut)
def release_wave(request, wave_id: uuid.UUID):
    """Start picking: the wave's orders can no longer be cancelled and pick tasks appear on the scanners."""
    principal = principal_of(request)
    require_roles(principal, *SUPERVISORS)
    wave = get_scoped(_waves(principal), wave_id)
    return picking.release_wave(wave.pk)


@router.get("/pick-tasks", response=PickTaskPage)
def list_pick_tasks(
    request,
    wave_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    mine: bool = False,
    open_only: bool = True,
    cursor: uuid.UUID | None = None,
    limit: int = 100,
):
    principal = principal_of(request)
    require_roles(principal, *OPERATORS)
    qs = _pick_tasks(principal)
    if open_only:
        qs = qs.filter(status__in=[PickTaskStatus.OPEN, PickTaskStatus.CLAIMED])
    if wave_id:
        qs = qs.filter(wave_id=wave_id)
    if site_id:
        qs = qs.filter(site_id=site_id)
    if mine:
        qs = qs.filter(assigned_to=principal.id)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


def _pick_task_for_operator(principal: Principal, task_id: uuid.UUID) -> PickTask:
    require_roles(principal, *OPERATORS)
    task = get_scoped(_pick_tasks(principal), task_id)
    if task.assigned_to and task.assigned_to != principal.id:
        raise Problem(409, "Task claimed", "Another operator has this task.")
    return task


@router.post("/pick-tasks/{task_id}/confirm", response=PickTaskOut)
def confirm_pick(request, task_id: uuid.UUID, body: PickConfirmIn):
    principal = principal_of(request)
    task = _pick_task_for_operator(principal, task_id)
    picking.confirm(
        task.pk, location_code=body.location_code, item_code=body.item_code, qty=body.qty, command_id=body.command_id
    )
    return _pick_tasks(principal).get(pk=task.pk)


@router.post("/pick-tasks/{task_id}/short", response=PickTaskOut)
def short_pick(request, task_id: uuid.UUID, body: PickShortIn):
    """Report that the bin held less than the task asked for. What was found is picked; the rest is looked for
    in other bins and added to the wave, or reported short."""
    principal = principal_of(request)
    task = _pick_task_for_operator(principal, task_id)
    picking.short(
        task.pk,
        location_code=body.location_code,
        qty_found=body.qty_found,
        reason=body.reason,
        command_id=body.command_id,
    )
    return _pick_tasks(principal).get(pk=task.pk)


# --- Packing ------------------------------------------------------------------------------------------------------


class HandlingUnitIn(Schema):
    sales_order_id: uuid.UUID
    kind: HandlingUnitKind


class PackIn(Schema):
    item_code: str = Field(description="Item barcode or SKU, as scanned")
    qty: Decimal = Field(gt=0, description="Base units")
    lot: str = ""


class CloseUnitIn(Schema):
    weight_kg: Decimal = Field(gt=0, max_digits=10, decimal_places=3)
    cartons: int | None = Field(None, gt=0, description="Defaults to 1 for a carton; required for a pallet")
    volume_m3: Decimal | None = Field(None, gt=0, max_digits=10, decimal_places=4)


class ContentOut(Schema):
    id: uuid.UUID
    order_line_id: uuid.UUID
    item_id: uuid.UUID
    sku: str
    lot: str
    expiry: date | None
    qty: Decimal

    @staticmethod
    def resolve_sku(obj):
        return obj.item.sku


class HandlingUnitOut(Schema):
    id: uuid.UUID
    sscc: str
    kind: str
    status: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    sales_order_id: uuid.UUID
    order_number: str
    weight_kg: Decimal | None
    volume_m3: Decimal | None
    cartons: int | None
    closed_at: datetime | None
    contents: list[ContentOut]

    @staticmethod
    def resolve_contents(obj: HandlingUnit):
        return list(obj.contents.select_related("item").order_by("created_at"))


class HandlingUnitPage(Schema):
    items: list[HandlingUnitOut]
    next_cursor: uuid.UUID | None


class PackLineOut(Schema):
    order_line_id: uuid.UUID
    item_id: uuid.UUID
    sku: str
    lot: str
    picked: Decimal
    packed: Decimal
    remaining: Decimal


class PackOrderOut(Schema):
    sales_order_id: uuid.UUID
    number: str
    client_id: uuid.UUID
    picking_done: bool
    lines: list[PackLineOut]
    open_units: list[HandlingUnitOut]


def _units(principal: Principal) -> QuerySet:
    return _staff_scoped(principal, HandlingUnit.objects.all())


def _unit_for_operator(principal: Principal, unit_id: uuid.UUID) -> HandlingUnit:
    require_roles(principal, *OPERATORS)
    return get_scoped(_units(principal), unit_id)


@router.get("/pack-orders", response=list[PackOrderOut])
def list_pack_orders(request, site_id: uuid.UUID):
    """The packing bench: orders at a site with picked stock not yet in a closed handling unit."""
    principal = principal_of(request)
    require_roles(principal, *OPERATORS)
    site = _site(principal, site_id)
    client_ids = None if principal.all_clients else principal.client_ids
    result = []
    for wave_order in packing.orders_to_pack(site.pk, client_ids):
        rows = packing.packable(wave_order.sales_order_id)
        skus = dict(Item.objects.filter(pk__in={r.item_id for r in rows}).values_list("pk", "sku"))
        result.append(
            {
                "sales_order_id": wave_order.sales_order_id,
                "number": wave_order.number,
                "client_id": wave_order.client_id,
                "picking_done": not PickTask.objects.filter(
                    sales_order_id=wave_order.sales_order_id,
                    status__in=[PickTaskStatus.OPEN, PickTaskStatus.CLAIMED],
                ).exists(),
                "lines": [
                    {
                        "order_line_id": r.order_line_id,
                        "item_id": r.item_id,
                        "sku": skus.get(r.item_id, ""),
                        "lot": r.lot,
                        "picked": r.picked,
                        "packed": r.packed,
                        "remaining": r.remaining,
                    }
                    for r in rows
                ],
                "open_units": list(
                    _units(principal).filter(sales_order_id=wave_order.sales_order_id, status=HandlingUnitStatus.OPEN)
                ),
            }
        )
    return result


@router.post("/handling-units", response={201: HandlingUnitOut})
def open_handling_unit(request, body: HandlingUnitIn):
    """Start a pallet or carton for an order in picking. It gets its SSCC now."""
    principal = principal_of(request)
    require_staff(principal)
    require_roles(principal, *OPERATORS)
    order = oms.order_summary(body.sales_order_id)
    if order is None or not principal.can_see_client(order.client_id) or not principal.can_see_site(order.site_id):
        raise Problem(404, "Not found")
    unit = packing.open_unit(order_id=order.sales_order_id, kind=body.kind, created_by=principal.id)
    return Status(201, unit)


@router.get("/handling-units", response=HandlingUnitPage)
def list_handling_units(
    request,
    sales_order_id: uuid.UUID | None = None,
    sscc: str | None = None,
    status: HandlingUnitStatus | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = 50,
):
    qs = _units(principal_of(request))
    if sales_order_id:
        qs = qs.filter(sales_order_id=sales_order_id)
    if sscc:
        digits = "".join(ch for ch in sscc if ch.isdigit())  # accepts "(00) …" and a scanned "00…" too
        qs = qs.filter(sscc=digits[2:] if len(digits) == 20 and digits.startswith("00") else digits)
    if status:
        qs = qs.filter(status=status)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/handling-units/{unit_id}", response=HandlingUnitOut)
def get_handling_unit(request, unit_id: uuid.UUID):
    return get_scoped(_units(principal_of(request)), unit_id)


@router.post("/handling-units/{unit_id}/contents", response={201: HandlingUnitOut})
def pack_into_unit(request, unit_id: uuid.UUID, body: PackIn):
    unit = _unit_for_operator(principal_of(request), unit_id)
    return Status(201, packing.add(unit.pk, item_code=body.item_code, qty=body.qty, lot=body.lot))


@router.delete("/handling-units/{unit_id}/contents/{content_id}", response=HandlingUnitOut)
def unpack_from_unit(request, unit_id: uuid.UUID, content_id: uuid.UUID):
    """Take a line back out of an open unit (a packing mistake). Closed units can't change."""
    unit = _unit_for_operator(principal_of(request), unit_id)
    return packing.remove(unit.pk, content_id)


@router.post("/handling-units/{unit_id}/close", response=HandlingUnitOut)
def close_handling_unit(request, unit_id: uuid.UUID, body: CloseUnitIn):
    principal = principal_of(request)
    unit = _unit_for_operator(principal, unit_id)
    return packing.close(
        unit.pk, weight_kg=body.weight_kg, cartons=body.cartons, volume_m3=body.volume_m3, closed_by=principal.id
    )


@router.get("/handling-units/{unit_id}/label")
def handling_unit_label(request, unit_id: uuid.UUID, format: Literal["pdf", "zpl"] = "pdf"):
    """The SSCC label: a 4x6 inch PDF for any printer, or ZPL for a Zebra printer."""
    unit = get_scoped(_units(principal_of(request)), unit_id)
    data = packing.label_for(unit)
    if format == "zpl":
        response = HttpResponse(labels.zpl(data), content_type="text/plain; charset=utf-8")
        response["Content-Disposition"] = f'inline; filename="{unit.sscc}.zpl"'
    else:
        response = HttpResponse(labels.pdf(data), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{unit.sscc}.pdf"'
    response["Cache-Control"] = "private, no-store"
    return response
