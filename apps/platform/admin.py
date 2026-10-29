from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from . import outbox, webhooks
from .models import (
    ApiClient,
    AuditEntry,
    DeliveryStatus,
    Document,
    EventDelivery,
    ExceptionCase,
    OutboxEvent,
    RoleGrant,
    SyncCommand,
    User,
    WebhookDelivery,
    WebhookSubscription,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class RoleGrantInline(admin.TabularInline):
    model = RoleGrant
    extra = 0


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ["email"]
    list_display = ["email", "full_name", "is_active", "is_staff"]
    search_fields = ["email", "full_name"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name",)}),
        ("Access", {"fields": ("is_active", "is_staff", "is_superuser")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)
    inlines = [RoleGrantInline]


@admin.register(ApiClient)
class ApiClientAdmin(admin.ModelAdmin):
    list_display = ["name", "key_id", "client_id", "is_active", "last_used_at"]
    readonly_fields = ["key_id", "secret_hash", "last_used_at"]


@admin.register(AuditEntry)
class AuditEntryAdmin(ReadOnlyAdmin):
    list_display = ["occurred_at", "actor_label", "action", "entity_type", "entity_id"]
    list_filter = ["action", "entity_type", "actor_kind"]
    search_fields = ["entity_id", "actor_label", "request_id"]
    date_hierarchy = "occurred_at"


class DeliveryInline(admin.TabularInline):
    model = EventDelivery
    extra = 0
    can_delete = False
    readonly_fields = ["consumer", "status", "attempts", "next_attempt_at", "last_error", "delivered_at"]


@admin.register(OutboxEvent)
class OutboxEventAdmin(ReadOnlyAdmin):
    list_display = ["occurred_at", "type", "version", "aggregate_type", "aggregate_id", "published_at"]
    list_filter = ["type"]
    search_fields = ["event_id", "aggregate_id", "correlation_id"]
    inlines = [DeliveryInline]


@admin.register(EventDelivery)
class EventDeliveryAdmin(ReadOnlyAdmin):
    list_display = ["event", "consumer", "status", "attempts", "next_attempt_at"]
    list_filter = ["status", "consumer"]
    actions = ["replay_selected"]

    @admin.action(description="Replay selected deliveries")
    def replay_selected(self, request, queryset):
        count = 0
        for delivery in queryset.exclude(status=DeliveryStatus.PENDING):
            outbox.replay(delivery.pk)
            count += 1
        self.message_user(request, f"Queued {count} deliveries for replay.", messages.SUCCESS)

    def has_change_permission(self, request, obj=None):  # needed for actions
        return request.user.is_superuser


@admin.register(Document)
class DocumentAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "kind", "original_name", "client_id", "size_bytes", "linked_type"]
    list_filter = ["kind"]
    search_fields = ["original_name", "sha256", "linked_id"]


@admin.register(ExceptionCase)
class ExceptionCaseAdmin(admin.ModelAdmin):
    list_display = ["created_at", "severity", "status", "kind", "summary", "assigned_to"]
    list_filter = ["status", "severity", "kind"]
    search_fields = ["summary", "entity_id"]
    readonly_fields = ["kind", "detail", "entity_type", "entity_id", "dedupe_key", "resolved_at"]


@admin.register(WebhookSubscription)
class WebhookSubscriptionAdmin(ReadOnlyAdmin):
    list_display = ["url", "client_id", "is_active", "created_at", "created_by"]
    list_filter = ["is_active"]
    exclude = ["secret"]


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(ReadOnlyAdmin):
    list_display = ["event", "subscription", "status", "attempts", "last_status_code", "next_attempt_at"]
    list_filter = ["status"]
    actions = ["requeue_selected"]

    @admin.action(description="Send selected dead deliveries again")
    def requeue_selected(self, request, queryset):
        count = sum(webhooks.requeue(pk) for pk in queryset.values_list("pk", flat=True))
        self.message_user(request, f"Queued {count} dead deliveries.", messages.SUCCESS)

    def has_change_permission(self, request, obj=None):  # needed for actions
        return request.user.is_superuser


@admin.register(SyncCommand)
class SyncCommandAdmin(ReadOnlyAdmin):
    list_display = ["received_at", "type", "status", "actor", "device_id", "recorded_at"]
    list_filter = ["status", "type"]
    search_fields = ["id", "actor", "device_id"]
    date_hierarchy = "received_at"
