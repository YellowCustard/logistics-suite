from django.contrib import admin

from .models import InboundNotice, InboundNoticeLine


class InboundNoticeLineInline(admin.TabularInline):
    model = InboundNoticeLine
    extra = 0
    can_delete = False
    readonly_fields = ["line_no", "item", "qty", "uom", "lot", "expiry", "qty_received", "status"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(InboundNotice)
class InboundNoticeAdmin(admin.ModelAdmin):
    """Read-only: notices change only through the API, so the outbox and audit stay consistent."""

    list_display = ["number", "client", "site", "client_ref", "kind", "status", "expected_at"]
    list_filter = ["status", "kind", "site"]
    search_fields = ["number", "client_ref", "client__code"]
    inlines = [InboundNoticeLineInline]

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
