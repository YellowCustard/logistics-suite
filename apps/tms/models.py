"""Transport: shipments built from packed orders, trips with stops, and the resource that drives them.

A shipment is the transport view of one sales order: what has to go from the warehouse to one delivery address.
TMS learns about orders and handling units only from events (oms.sales_order.*, wms.handling_unit.closed) and
keeps its own copy of what it needs, so it never reads another module's tables.
"""

from django.db import models
from django.db.models import Q

from apps.masterdata.models import Item, Party, Person, Site, Vehicle
from apps.platform.models import Audited, BaseModel, Document

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
    delivered_at = models.DateTimeField(null=True, blank=True, help_text="When the recipient signed (device time)")

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


class UnitDeliveryStatus(models.TextChoices):
    DELIVERED = "delivered"
    DAMAGED = "damaged", "Delivered damaged"
    REFUSED = "refused", "Refused by the recipient"
    MISSING = "missing", "Not found on the vehicle"


# Units the recipient kept; refused and missing units are not delivered.
RECEIVED = (UnitDeliveryStatus.DELIVERED, UnitDeliveryStatus.DAMAGED)


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
    contents = models.JSONField(default=list, blank=True, help_text="[{order_line_id, item_id, qty, lot}] from WMS")
    delivery_status = models.CharField(max_length=10, choices=UnitDeliveryStatus.choices, blank=True)
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


class ArrivalSource(models.TextChoices):
    MANUAL = "manual", "Driver tapped arrive"
    GEOFENCE = "geofence", "Vehicle entered the geofence"
    POD = "pod", "Implied by the proof of delivery"


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
    arrival_source = models.CharField(max_length=10, choices=ArrivalSource.choices, blank=True)
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


class DeliveryOutcome(models.TextChoices):
    DELIVERED = "delivered"
    FAILED = "failed"


class FailureReason(models.TextChoices):
    CLOSED = "closed", "Premises closed"
    REFUSED = "refused", "Refused by the recipient"
    NO_ACCESS = "no_access", "No access"
    WRONG_ADDRESS = "wrong_address", "Wrong or unknown address"
    DAMAGED = "damaged", "Goods damaged"
    OUT_OF_TIME = "out_of_time", "Ran out of time"
    OTHER = "other", "Other (see note)"


class DeliveryAttempt(Audited, BaseModel):
    """What happened at a drop stop: a proof of delivery, or a failed delivery with its reason."""

    stop = models.OneToOneField(Stop, on_delete=models.PROTECT, related_name="attempt")
    trip = models.ForeignKey(Trip, on_delete=models.PROTECT, related_name="attempts")
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    outcome = models.CharField(max_length=10, choices=DeliveryOutcome.choices)
    recipient_name = models.CharField(max_length=120, blank=True)
    reason = models.CharField(max_length=20, choices=FailureReason.choices, blank=True)
    note = models.TextField(blank=True)
    captured_at = models.DateTimeField(help_text="Device time of the signature or the failure")
    captured_by = models.UUIDField(null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    signature = models.ForeignKey(Document, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(outcome="delivered", reason="") & ~Q(recipient_name="")
                | Q(outcome="failed", signature__isnull=True) & ~Q(reason=""),
                name="delivery_attempt_outcome_fields",
            ),
        ]


class DeliveryPhoto(Audited, BaseModel):
    attempt = models.ForeignKey(DeliveryAttempt, on_delete=models.PROTECT, related_name="photos")
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["attempt", "document"], name="delivery_photo_uq")]
