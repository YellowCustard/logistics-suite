from django.contrib import admin

from .models import InventoryBalance, InventoryMovement, Location, PutawayTask, Receipt, ReceiptLine


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ["code", "site", "zone", "kind", "pallet_capacity", "is_active"]
    list_filter = ["site", "kind", "zone"]
    search_fields = ["code"]


class ReadOnlyAdmin(admin.ModelAdmin):
    """Stock and receiving records change only through services, so the ledger and outbox stay consistent."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReceiptLineInline(admin.TabularInline):
    model = ReceiptLine
    extra = 0
    fields = ["item", "qty", "uom", "base_qty", "lot", "expiry", "condition", "lpn", "inbound_notice_line_id"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Receipt)
class ReceiptAdmin(ReadOnlyAdmin):
    list_display = ["number", "client", "site", "status", "completed_at"]
    list_filter = ["status", "site"]
    search_fields = ["number"]
    inlines = [ReceiptLineInline]


@admin.register(InventoryBalance)
class InventoryBalanceAdmin(ReadOnlyAdmin):
    list_display = ["item", "client", "location", "lot", "expiry", "status", "qty_on_hand", "qty_allocated"]
    list_filter = ["status", "location__site"]
    search_fields = ["item__sku", "location__code", "lot"]


@admin.register(InventoryMovement)
class InventoryMovementAdmin(ReadOnlyAdmin):
    list_display = ["occurred_at", "kind", "balance", "qty_delta", "source_type", "source_id", "reason"]
    list_filter = ["kind"]
    search_fields = ["source_id", "balance__item__sku"]


@admin.register(PutawayTask)
class PutawayTaskAdmin(ReadOnlyAdmin):
    list_display = ["receipt_line", "site", "qty", "suggested_location", "to_location", "status", "override_reason"]
    list_filter = ["status", "site"]
