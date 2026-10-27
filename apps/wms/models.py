"""Warehouse module: bins, receiving, putaway, counts, allocation, and the inventory ledger."""

from decimal import Decimal

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Q

from apps.masterdata.models import Item, Party, Site
from apps.platform.ids import uuid7
from apps.platform.models import Audited, BaseModel

QTY = {"max_digits": 14, "decimal_places": 3}
QTY_STEP = Decimal("0.001")  # matches QTY decimal_places


class LocationKind(models.TextChoices):
    STORAGE = "storage"
    PICK_FACE = "pick_face"
    DOCK = "dock"
    STAGING = "staging"
    QUARANTINE = "quarantine"


class Location(Audited, BaseModel):
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+")
    code = models.CharField(max_length=30, help_text="e.g. A-01-03-B (zone-aisle-level-position)")
    zone = models.CharField(max_length=20, blank=True)
    kind = models.CharField(max_length=20, choices=LocationKind.choices, default=LocationKind.STORAGE)
    pallet_capacity = models.PositiveSmallIntegerField(default=1)
    pick_sequence = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["site", "code"], name="location_site_code_uq"),
            models.CheckConstraint(condition=Q(pallet_capacity__gte=0), name="location_capacity_non_negative"),
        ]
        indexes = [models.Index(fields=["site", "pick_sequence"], name="location_pick_path_idx")]

    def __str__(self) -> str:
        return self.code


# --- Inventory ledger -------------------------------------------------------------------------------------


class StockStatus(models.TextChoices):
    AVAILABLE = "available"
    QUARANTINE = "quarantine"
    DAMAGED = "damaged"


class InventoryBalance(models.Model):
    """Stock of one client's item at one location, per lot, expiry and status, in base units.

    Changed only by `wms.ledger.post` in the same transaction as the InventoryMovement that explains it.
    Not `Audited`: the movement ledger is its audit trail.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="balances")
    lot = models.CharField(max_length=40, blank=True)
    expiry = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=StockStatus.choices, default=StockStatus.AVAILABLE)
    qty_on_hand = models.DecimalField(**QTY, default=0)
    qty_allocated = models.DecimalField(**QTY, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "item", "location", "lot", "expiry", "status"],
                nulls_distinct=False,
                name="inventory_balance_dimension_uq",
            ),
            models.CheckConstraint(condition=Q(qty_on_hand__gte=0), name="inventory_on_hand_non_negative"),
            models.CheckConstraint(
                condition=Q(qty_allocated__gte=0) & Q(qty_allocated__lte=models.F("qty_on_hand")),
                name="inventory_allocated_within_on_hand",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "item"], name="inventory_client_item_idx"),
            models.Index(fields=["location"], name="inventory_location_idx", condition=Q(qty_on_hand__gt=0)),
        ]

    def __str__(self) -> str:
        return f"{self.item_id}@{self.location_id} {self.qty_on_hand}"


class MovementKind(models.TextChoices):
    RECEIVE = "receive"
    PUTAWAY = "putaway"
    MOVE = "move"
    PICK = "pick"
    ADJUST = "adjust"
    OPENING = "opening"


class InventoryMovement(models.Model):
    """Append-only stock ledger (enforced by a database trigger). A transfer is two rows sharing a
    command_id; a replayed command_id is detected before any row is written."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    balance = models.ForeignKey(InventoryBalance, on_delete=models.PROTECT, related_name="movements")
    kind = models.CharField(max_length=10, choices=MovementKind.choices)
    qty_delta = models.DecimalField(**QTY)
    reason = models.CharField(max_length=200, blank=True)
    source_type = models.CharField(max_length=40, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    command_id = models.UUIDField(null=True, blank=True)
    actor_id = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(qty_delta=0), name="inventory_movement_non_zero"),
            models.UniqueConstraint(fields=["command_id", "balance"], name="inventory_movement_command_uq"),
        ]
        indexes = [
            models.Index(fields=["source_type", "source_id"], name="inventory_movement_source_idx"),
            models.Index(fields=["command_id"], name="inventory_movement_command_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.qty_delta}"


# --- Receiving ---------------------------------------------------------------------------------------------


class ReceiptStatus(models.TextChoices):
    OPEN = "open"
    COMPLETED = "completed"


class Receipt(Audited, BaseModel):
    """Goods received at a dock against one inbound notice."""

    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+")
    number = models.CharField(max_length=20, unique=True)
    inbound_notice_id = models.UUIDField(help_text="oms.InboundNotice (cross-module id, no foreign key)")
    dock = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=ReceiptStatus.choices, default=ReceiptStatus.OPEN)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            # Phase 1: one receipt per inbound notice. Split deliveries arrive as separate notices.
            models.UniqueConstraint(fields=["inbound_notice_id"], name="receipt_one_per_notice"),
            models.CheckConstraint(
                condition=Q(status="open") | Q(completed_at__isnull=False), name="receipt_completed_has_time"
            ),
        ]

    def __str__(self) -> str:
        return self.number


class Condition(models.TextChoices):
    GOOD = "good"
    DAMAGED = "damaged"


class ReceiptLine(Audited, BaseModel):
    """One scan at the dock: usually one pallet of one item, lot and expiry."""

    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="lines")
    inbound_notice_line_id = models.UUIDField(null=True, blank=True, help_text="Null for an unexpected item")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    qty = models.DecimalField(**QTY, help_text="In `uom`")
    uom = models.CharField(max_length=10)
    base_qty = models.DecimalField(**QTY)
    lot = models.CharField(max_length=40, blank=True)
    expiry = models.DateField(null=True, blank=True)
    condition = models.CharField(max_length=10, choices=Condition.choices, default=Condition.GOOD)
    lpn = models.CharField(max_length=40, blank=True, help_text="Pallet label scanned or printed at the dock")
    command_id = models.UUIDField(null=True, blank=True, unique=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(qty__gt=0) & Q(base_qty__gt=0), name="receipt_line_qty_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.receipt.number} {self.item_id} {self.qty} {self.uom}"


# --- Putaway -------------------------------------------------------------------------------------------------


class TaskStatus(models.TextChoices):
    OPEN = "open"
    CLAIMED = "claimed"
    DONE = "done"


class PutawayTask(Audited, BaseModel):
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+")
    receipt_line = models.OneToOneField(ReceiptLine, on_delete=models.PROTECT, related_name="putaway_task")
    from_balance = models.ForeignKey(InventoryBalance, on_delete=models.PROTECT, related_name="+")
    qty = models.DecimalField(**QTY, help_text="Base units")
    suggested_location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    to_location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    override_reason = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=TaskStatus.choices, default=TaskStatus.OPEN)
    assigned_to = models.UUIDField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(qty__gt=0), name="putaway_qty_positive"),
            models.CheckConstraint(
                condition=~Q(status="done") | (Q(to_location__isnull=False) & Q(completed_at__isnull=False)),
                name="putaway_done_has_target",
            ),
        ]
        indexes = [models.Index(fields=["site", "status"], name="putaway_site_status_idx")]

    def __str__(self) -> str:
        return f"Putaway {self.receipt_line_id}"


# --- Cycle counts ---------------------------------------------------------------------------------------------


class CountStatus(models.TextChoices):
    OPEN = "open"
    AWAITING_APPROVAL = "awaiting_approval"
    POSTED = "posted"
    REJECTED = "rejected"


class CycleCount(Audited, BaseModel):
    """A blind count of a set of bins. Counters never see system quantities."""

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+")
    number = models.CharField(max_length=20, unique=True)
    locations = models.ManyToManyField(Location, related_name="+")
    status = models.CharField(max_length=20, choices=CountStatus.choices, default=CountStatus.OPEN)
    created_by = models.UUIDField(null=True, blank=True)
    submitted_by = models.UUIDField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.UUIDField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["site", "status"], name="cycle_count_site_status_idx")]

    def __str__(self) -> str:
        return self.number


class CycleCountLine(Audited, BaseModel):
    """What was found in one bin for one stock key. `system_qty` is the on-hand quantity when the line was
    counted, so stock that moves later does not distort the variance; the adjustment posts the variance."""

    count = models.ForeignKey(CycleCount, on_delete=models.CASCADE, related_name="lines")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+")
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    lot = models.CharField(max_length=40, blank=True)
    expiry = models.DateField(null=True, blank=True)
    stock_status = models.CharField(max_length=12, choices=StockStatus.choices, default=StockStatus.AVAILABLE)
    counted_qty = models.DecimalField(**QTY, help_text="Base units")
    system_qty = models.DecimalField(**QTY, help_text="Base units on hand when counted")
    counted_by = models.UUIDField(null=True, blank=True, help_text="Null for bins confirmed empty at submit")
    # Every offline command that has touched this line, so a late retry is a no-op rather than an overwrite.
    command_ids = ArrayField(models.UUIDField(), default=list, blank=True)

    class Meta:
        indexes = [GinIndex(fields=["command_ids"], name="wms_countline_commands")]
        constraints = [
            models.UniqueConstraint(
                fields=["count", "location", "client", "item", "lot", "expiry", "stock_status"],
                nulls_distinct=False,
                name="cycle_count_line_key_uq",
            ),
            models.CheckConstraint(condition=Q(counted_qty__gte=0), name="cycle_count_line_non_negative"),
        ]

    @property
    def variance(self):
        return self.counted_qty - self.system_qty

    def __str__(self) -> str:
        return f"{self.count.number} {self.location_id} {self.item_id}"


# --- Allocation ---------------------------------------------------------------------------------------------------


class AllocationStatus(models.TextChoices):
    RESERVED = "reserved"
    RELEASED = "released", "Released to a pick wave"
    PICKED = "picked"
    SHORT = "short", "Shorted at the pick face"
    CANCELLED = "cancelled"


# Allocations that still hold (or held and consumed) stock for their order line.
ACTIVE_ALLOCATION = (AllocationStatus.RESERVED, AllocationStatus.RELEASED, AllocationStatus.PICKED)


class Allocation(Audited, BaseModel):
    """Stock earmarked for one order line from one balance. The sum of open allocations on a balance equals
    its qty_allocated (checked nightly by reconciliation)."""

    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+")
    sales_order_id = models.UUIDField(help_text="oms.SalesOrder (cross-module id, no foreign key)")
    order_line_id = models.UUIDField(help_text="oms.OrderLine (cross-module id, no foreign key)")
    balance = models.ForeignKey(InventoryBalance, on_delete=models.PROTECT, related_name="allocations")
    pick_wave = models.ForeignKey(
        "PickWave", null=True, blank=True, on_delete=models.PROTECT, related_name="allocations"
    )
    qty_allocated = models.DecimalField(**QTY, help_text="Base units")
    qty_picked = models.DecimalField(**QTY, default=0, help_text="Base units")
    status = models.CharField(max_length=10, choices=AllocationStatus.choices, default=AllocationStatus.RESERVED)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(qty_allocated__gt=0), name="allocation_qty_positive"),
            models.CheckConstraint(
                condition=Q(qty_picked__gte=0) & Q(qty_picked__lte=models.F("qty_allocated")),
                name="allocation_picked_within_allocated",
            ),
        ]
        indexes = [
            models.Index(fields=["sales_order_id"], name="allocation_order_idx"),
            models.Index(fields=["order_line_id"], name="allocation_line_idx"),
            models.Index(
                fields=["balance"], name="allocation_open_balance_idx", condition=Q(status__in=["reserved", "released"])
            ),
        ]

    def __str__(self) -> str:
        return f"{self.order_line_id} {self.qty_allocated} from {self.balance_id}"


# --- Waves and picking ------------------------------------------------------------------------------------------


class WaveStatus(models.TextChoices):
    PLANNED = "planned"
    RELEASED = "released"
    COMPLETED = "completed"


class PickWave(Audited, BaseModel):
    """A batch of allocated orders released to the floor together. Releasing starts picking on its orders
    (they can no longer be cancelled) and creates one pick task per allocation."""

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+")
    number = models.CharField(max_length=30, unique=True)
    status = models.CharField(max_length=10, choices=WaveStatus.choices, default=WaveStatus.PLANNED)
    created_by = models.UUIDField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status="planned") | Q(released_at__isnull=False), name="wave_released_has_time"
            ),
            models.CheckConstraint(
                condition=~Q(status="completed") | Q(completed_at__isnull=False), name="wave_completed_has_time"
            ),
        ]
        indexes = [models.Index(fields=["site", "status"], name="wave_site_status_idx")]

    def __str__(self) -> str:
        return self.number


class WaveOrder(BaseModel):
    """An order's place in a wave. An order is picked in exactly one wave."""

    wave = models.ForeignKey(PickWave, on_delete=models.CASCADE, related_name="orders")
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    sales_order_id = models.UUIDField(unique=True, help_text="oms.SalesOrder (cross-module id, no foreign key)")
    number = models.CharField(max_length=30, help_text="The order number, copied for display")

    def __str__(self) -> str:
        return f"{self.wave_id} {self.number}"


class PickTaskStatus(models.TextChoices):
    OPEN = "open"
    CLAIMED = "claimed"
    DONE = "done"
    SHORT = "short"


OPEN_PICK = (PickTaskStatus.OPEN, PickTaskStatus.CLAIMED)


class PickTask(Audited, BaseModel):
    """Pick one allocation: go to its bin, scan the bin and the item, take the quantity."""

    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+")
    wave = models.ForeignKey(PickWave, on_delete=models.PROTECT, related_name="tasks")
    allocation = models.OneToOneField(Allocation, on_delete=models.PROTECT, related_name="pick_task")
    sales_order_id = models.UUIDField(help_text="oms.SalesOrder (cross-module id, no foreign key)")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    from_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+")
    qty = models.DecimalField(**QTY, help_text="Base units")
    qty_picked = models.DecimalField(**QTY, default=0, help_text="Base units")
    status = models.CharField(max_length=10, choices=PickTaskStatus.choices, default=PickTaskStatus.OPEN)
    assigned_to = models.UUIDField(null=True, blank=True)
    short_reason = models.CharField(max_length=200, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(qty__gt=0), name="pick_qty_positive"),
            models.CheckConstraint(
                condition=Q(qty_picked__gte=0) & Q(qty_picked__lte=models.F("qty")), name="pick_picked_within_qty"
            ),
            models.CheckConstraint(
                condition=~Q(status="done") | (Q(qty_picked=models.F("qty")) & Q(completed_at__isnull=False)),
                name="pick_done_is_complete",
            ),
            models.CheckConstraint(
                condition=~Q(status="short")
                | (Q(qty_picked__lt=models.F("qty")) & Q(completed_at__isnull=False) & ~Q(short_reason="")),
                name="pick_short_has_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["wave", "status"], name="pick_wave_status_idx"),
            models.Index(fields=["site", "status"], name="pick_site_status_idx"),
            models.Index(fields=["sales_order_id"], name="pick_order_idx"),
        ]

    def __str__(self) -> str:
        return f"Pick {self.qty} {self.item_id} from {self.from_location_id}"
