"""Transport: shipments built from packed orders, trips with stops, and the resource that drives them.

A shipment is the transport view of one sales order: what has to go from the warehouse to one delivery address.
TMS learns about orders and handling units only from events (oms.sales_order.*, wms.handling_unit.closed) and
keeps its own copy of what it needs, so it never reads another module's tables.
"""

from django.db import models
from django.db.models import Q

from apps.masterdata.models import Item, Party, Person, Site, Vehicle
from apps.platform.models import Audited, BaseModel

QTY = {"max_digits": 14, "decimal_places": 3}


class ShipmentStatus(models.TextChoices):
    DRAFT = "draft", "Draft (order not packed yet)"
    READY = "ready", "Ready to plan"
    PLANNED = "planned", "On a trip being planned"
    IN_TRANSIT = "in_transit", "In transit"
    DELIVERED = "delivered"
    FAILED = "failed", "Delivery failed"
    CANCELLED = "cancelled"


# A shipment in one of these states can be put on a trip.
PLANNABLE = (ShipmentStatus.DRAFT, ShipmentStatus.READY)


class Shipment(Audited, BaseModel):
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+", help_text="Shipping warehouse")
    ship_to = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+", help_text="Delivery address")
    sales_order_id = models.UUIDField(unique=True, help_text="oms.SalesOrder (cross-module id, no foreign key)")
    order_number = models.CharField(max_length=30)
    client_ref = models.CharField(max_length=60, blank=True)
    required_by = models.DateField(null=True, blank=True)
    number = models.CharField(max_length=30, unique=True)
    status = models.CharField(max_length=20, choices=ShipmentStatus.choices, default=ShipmentStatus.DRAFT)
    packed_at = models.DateTimeField(null=True, blank=True, help_text="Set when every unit for the order is closed")
    weight_kg = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    volume_m3 = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["site", "status"], name="shipment_site_status_idx")]

    def __str__(self) -> str:
        return self.number


class ShipmentLine(Audited, BaseModel):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="lines")
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    order_line_id = models.UUIDField(help_text="oms.OrderLine (cross-module id)")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    qty_ordered = models.DecimalField(**QTY)
    qty_packed = models.DecimalField(**QTY, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["shipment", "order_line_id"], name="shipment_line_uq"),
            models.CheckConstraint(condition=Q(qty_ordered__gt=0), name="shipment_line_qty_pos"),
            models.CheckConstraint(condition=Q(qty_packed__gte=0), name="shipment_line_packed_nonneg"),
        ]


class ShipmentUnit(Audited, BaseModel):
    """A closed handling unit (pallet or carton) belonging to a shipment, as reported by WMS."""

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="units")
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    handling_unit_id = models.UUIDField(unique=True, help_text="wms.HandlingUnit (cross-module id)")
    sscc = models.CharField(max_length=18, unique=True)
    kind = models.CharField(max_length=10)
    weight_kg = models.DecimalField(max_digits=10, decimal_places=3)
    volume_m3 = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    cartons = models.PositiveIntegerField(default=1)
    loaded_trip = models.ForeignKey("Trip", null=True, blank=True, on_delete=models.PROTECT, related_name="loaded")
    loaded_at = models.DateTimeField(null=True, blank=True)
    loaded_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(loaded_trip__isnull=True, loaded_at__isnull=True)
                | Q(loaded_trip__isnull=False, loaded_at__isnull=False),
                name="shipment_unit_loaded_consistent",
            )
        ]


class TripStatus(models.TextChoices):
    PLANNING = "planning"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Trip(Audited, BaseModel):
    """One run from a warehouse: either an own vehicle with its driver, or a subcontracted carrier."""

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+", help_text="Departure warehouse")
    number = models.CharField(max_length=30, unique=True)
    planned_start = models.DateTimeField()
    status = models.CharField(max_length=20, choices=TripStatus.choices, default=TripStatus.PLANNING)
    vehicle = models.ForeignKey(Vehicle, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    driver = models.ForeignKey(Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    carrier = models.ForeignKey(Party, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    agreed_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="ZAR")
    created_by = models.UUIDField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    dispatched_by = models.UUIDField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # Exactly one kind of resource: an own vehicle with a driver, or a carrier with its agreed cost.
            models.CheckConstraint(
                condition=Q(carrier__isnull=True, agreed_cost__isnull=True)
                | Q(carrier__isnull=False, agreed_cost__gt=0, vehicle__isnull=True, driver__isnull=True),
                name="trip_one_kind_of_resource",
            ),
            models.CheckConstraint(
                condition=Q(vehicle__isnull=True, driver__isnull=True) | Q(vehicle__isnull=False, driver__isnull=False),
                name="trip_vehicle_with_driver",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["planning", "cancelled"])
                | Q(carrier__isnull=False)
                | Q(vehicle__isnull=False, driver__isnull=False),
                name="trip_dispatched_has_resource",
            ),
        ]
        indexes = [models.Index(fields=["site", "status", "planned_start"], name="trip_site_status_idx")]

    def __str__(self) -> str:
        return self.number


class StopStatus(models.TextChoices):
    PENDING = "pending"
    ARRIVED = "arrived"
    DONE = "done"


class Stop(Audited, BaseModel):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="stops")
    seq = models.PositiveSmallIntegerField()
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+")
    window_from = models.DateTimeField(null=True, blank=True)
    window_to = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=StopStatus.choices, default=StopStatus.PENDING)
    arrived_at = models.DateTimeField(null=True, blank=True)
    departed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["trip", "seq"], name="stop_trip_seq_uq"),
            models.CheckConstraint(
                condition=Q(window_from__isnull=True)
                | Q(window_to__isnull=True)
                | Q(window_to__gte=models.F("window_from")),
                name="stop_window_ordered",
            ),
        ]
        ordering = ["trip", "seq"]


class StopAction(models.TextChoices):
    PICKUP = "pickup"
    DROP = "drop"


class StopShipment(Audited, BaseModel):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="+")
    stop = models.ForeignKey(Stop, on_delete=models.CASCADE, related_name="shipments")
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, related_name="stops")
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=10, choices=StopAction.choices)

    class Meta:
        constraints = [
            # A shipment has exactly one pickup and one drop per trip.
            models.UniqueConstraint(fields=["trip", "shipment", "action"], name="stop_shipment_once_per_trip"),
        ]
