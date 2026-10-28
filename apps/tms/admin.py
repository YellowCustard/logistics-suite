from django.contrib import admin

from .models import Shipment, ShipmentLine, ShipmentUnit, Stop, StopShipment, Trip


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ReadOnlyAdmin(admin.ModelAdmin):
    """Transport records change only through the API, so the outbox and audit stay consistent."""

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ShipmentLineInline(ReadOnlyInline):
    model = ShipmentLine
    fields = ["order_line_id", "item", "qty_ordered", "qty_packed"]


class ShipmentUnitInline(ReadOnlyInline):
    model = ShipmentUnit
    fk_name = "shipment"
    fields = ["sscc", "kind", "weight_kg", "cartons", "loaded_trip", "loaded_at"]


@admin.register(Shipment)
class ShipmentAdmin(ReadOnlyAdmin):
    list_display = ["number", "order_number", "client", "site", "ship_to", "status", "packed_at", "required_by"]
    list_filter = ["status", "site"]
    search_fields = ["number", "order_number", "client_ref", "client__code"]
    inlines = [ShipmentLineInline, ShipmentUnitInline]


class StopInline(ReadOnlyInline):
    model = Stop
    fields = ["seq", "site", "window_from", "window_to", "status", "arrived_at", "departed_at"]


class StopShipmentInline(ReadOnlyInline):
    model = StopShipment
    fields = ["stop", "shipment", "action"]


@admin.register(Trip)
class TripAdmin(ReadOnlyAdmin):
    list_display = ["number", "site", "planned_start", "status", "vehicle", "driver", "carrier", "agreed_cost"]
    list_filter = ["status", "site"]
    search_fields = ["number", "vehicle__registration", "carrier__code"]
    inlines = [StopInline, StopShipmentInline]
