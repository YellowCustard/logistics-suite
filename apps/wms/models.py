"""Warehouse module: bins, receiving, putaway, and the inventory ledger."""

from django.db import models
from django.db.models import Q

from apps.masterdata.models import Item, Party, Site
from apps.platform.ids import uuid7
from apps.platform.models import Audited, BaseModel

QTY = {"max_digits": 14, "decimal_places": 3}


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
