from django.contrib import admin, messages

from apps.platform.apiutils import Problem

from . import services
from .models import (
    Contract,
    Currency,
    FxRate,
    Item,
    ItemBarcode,
    ItemUom,
    Party,
    PartyRole,
    Person,
    RateCard,
    RateCardStatus,
    RateLine,
    Site,
    TaxCode,
    Vehicle,
)


class PartyRoleInline(admin.TabularInline):
    model = PartyRole
    extra = 0


@admin.register(Party)
class PartyAdmin(admin.ModelAdmin):
    list_display = ["code", "legal_name", "vat_number", "is_active"]
    search_fields = ["code", "legal_name", "vat_number"]
    list_filter = ["roles__role", "is_active"]
    inlines = [PartyRoleInline]


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "kind", "is_active"]
    list_filter = ["kind"]
    search_fields = ["code", "name"]


class ItemUomInline(admin.TabularInline):
    model = ItemUom
    extra = 0


class ItemBarcodeInline(admin.TabularInline):
    model = ItemBarcode
    extra = 0
    exclude = ["client"]


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ["sku", "description", "client", "base_uom", "is_active"]
    list_filter = ["client"]
    search_fields = ["sku", "description"]
    inlines = [ItemUomInline, ItemBarcodeInline]


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ["registration", "fleet_number", "carrier", "telematics_id", "roadworthy_expiry"]
    search_fields = ["registration", "fleet_number", "telematics_id"]


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ["first_name", "last_name", "employee_code", "is_driver", "prdp_expiry"]
    search_fields = ["first_name", "last_name", "employee_code"]
    # Personal fields are write-only in the admin: never displayed back (POPIA).
    exclude = ["id_number", "phone"]


class RateLineInline(admin.TabularInline):
    model = RateLine
    extra = 0

    def has_change_permission(self, request, obj=None):
        return obj is None or obj.status == RateCardStatus.DRAFT

    def has_add_permission(self, request, obj=None):
        return obj is None or obj.status == RateCardStatus.DRAFT

    def has_delete_permission(self, request, obj=None):
        return obj is None or obj.status == RateCardStatus.DRAFT


@admin.register(RateCard)
class RateCardAdmin(admin.ModelAdmin):
    list_display = ["contract", "number", "status", "valid_from", "valid_to", "published_at"]
    list_filter = ["status"]
    readonly_fields = ["number", "status", "published_at", "published_by"]
    inlines = [RateLineInline]
    actions = ["publish"]

    @admin.action(description="Publish selected draft rate cards")
    def publish(self, request, queryset):
        for card in queryset:
            try:
                services.publish_rate_card(card)
                self.message_user(request, f"Published {card}.", messages.SUCCESS)
            except Problem as exc:
                self.message_user(request, f"{card}: {exc.title}. {exc.detail}", messages.ERROR)

    def save_model(self, request, obj, form, change):
        if not change:
            created = services.create_rate_card(
                contract=obj.contract, valid_from=obj.valid_from, valid_to=obj.valid_to, notes=obj.notes
            )
            obj.pk, obj.number = created.pk, created.number
            obj._state.adding = False
            return
        super().save_model(request, obj, form, change)


admin.site.register(Contract)
admin.site.register(Currency)
admin.site.register(FxRate)
admin.site.register(TaxCode)
