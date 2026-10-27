"""Order management: inbound notices (ASNs and returns) and outbound sales orders."""

from decimal import Decimal

from django.db import models
from django.db.models import Q

from apps.masterdata.models import Item, Party, Site
from apps.platform.fields import EncryptedTextField
from apps.platform.models import Audited, BaseModel

QTY = {"max_digits": 14, "decimal_places": 3}
QTY_STEP = Decimal("0.001")  # matches QTY decimal_places


class InboundKind(models.TextChoices):
    PURCHASE = "purchase"
    RETURN = "return"


class InboundStatus(models.TextChoices):
    ACCEPTED = "accepted"
    RECEIVING = "receiving"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class InboundNotice(Audited, BaseModel):
    """An advance shipping notice from a client. No change once receiving starts, only cancellation
    before it does."""

    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+")
    number = models.CharField(max_length=20, unique=True)
    client_ref = models.CharField(max_length=60, blank=True, help_text="The client's own reference, e.g. PO number")
    kind = models.CharField(max_length=10, choices=InboundKind.choices, default=InboundKind.PURCHASE)
    status = models.CharField(max_length=10, choices=InboundStatus.choices, default=InboundStatus.ACCEPTED)
    expected_at = models.DateTimeField()
    notes = models.TextField(blank=True)
    cancelled_reason = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "client_ref"], condition=~Q(client_ref=""), name="inbound_notice_client_ref_uq"
            ),
            models.CheckConstraint(
                condition=~Q(status="cancelled") | ~Q(cancelled_reason=""), name="inbound_notice_cancel_reason"
            ),
        ]
        indexes = [models.Index(fields=["client", "status"], name="inbound_notice_client_status")]

    def __str__(self) -> str:
        return self.number


class InboundLineStatus(models.TextChoices):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class InboundNoticeLine(Audited, BaseModel):
    notice = models.ForeignKey(InboundNotice, on_delete=models.CASCADE, related_name="lines")
    line_no = models.PositiveSmallIntegerField()
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    qty = models.DecimalField(**QTY, help_text="In `uom`")
    uom = models.CharField(max_length=10)
    lot = models.CharField(max_length=40, blank=True)
    expiry = models.DateField(null=True, blank=True)
    qty_received = models.DecimalField(**QTY, default=0, help_text="In `uom`, set when the receipt completes")
    status = models.CharField(max_length=10, choices=InboundLineStatus.choices, default=InboundLineStatus.OPEN)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["notice", "line_no"], name="inbound_line_no_uq"),
            models.CheckConstraint(condition=Q(qty__gt=0), name="inbound_line_qty_positive"),
            models.CheckConstraint(condition=Q(qty_received__gte=0), name="inbound_line_received_non_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.notice.number}/{self.line_no}"


# --- Outbound sales orders ------------------------------------------------------------------------------------


class OrderStatus(models.TextChoices):
    ACCEPTED = "accepted"
    ALLOCATED = "allocated"
    BACKORDERED = "backordered", "Backordered (short of stock)"
    PICKING = "picking"
    PACKED = "packed"
    DISPATCHED = "dispatched"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


# Picking has not started, so the order can still be cancelled.
CANCELLABLE = (OrderStatus.ACCEPTED, OrderStatus.ALLOCATED, OrderStatus.BACKORDERED)


class SalesOrder(Audited, BaseModel):
    """An outbound order from a client: ship these items from this warehouse to this address."""

    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+", help_text="Shipping warehouse")
    number = models.CharField(max_length=20, unique=True)
    client_ref = models.CharField(max_length=60, blank=True, help_text="The client's own reference")
    ship_to = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+", help_text="Delivery address")
    required_by = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=OrderStatus.choices, default=OrderStatus.ACCEPTED)
    contact_name = models.CharField(max_length=120, blank=True)
    contact_phone = EncryptedTextField(blank=True)
    delivery_instructions = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    cancelled_reason = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "client_ref"], condition=~Q(client_ref=""), name="sales_order_client_ref_uq"
            ),
            models.CheckConstraint(
                condition=~Q(status="cancelled") | ~Q(cancelled_reason=""), name="sales_order_cancel_reason"
            ),
        ]
        indexes = [
            models.Index(fields=["client", "status"], name="sales_order_client_status"),
            models.Index(fields=["site", "status", "required_by"], name="sales_order_site_status"),
        ]

    def __str__(self) -> str:
        return self.number


class OrderLineStatus(models.TextChoices):
    OPEN = "open"
    CANCELLED = "cancelled"


class OrderLine(Audited, BaseModel):
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="lines")
    line_no = models.PositiveSmallIntegerField()
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    qty = models.DecimalField(**QTY, help_text="In `uom`")
    uom = models.CharField(max_length=10)
    base_qty = models.DecimalField(**QTY, help_text="`qty` in the item's base unit")
    status = models.CharField(max_length=10, choices=OrderLineStatus.choices, default=OrderLineStatus.OPEN)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "line_no"], name="order_line_no_uq"),
            models.CheckConstraint(condition=Q(qty__gt=0) & Q(base_qty__gt=0), name="order_line_qty_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.order.number}/{self.line_no}"
