from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q

from .ids import uuid7


class BaseModel(models.Model):
    """Standard columns for every table: UUIDv7 key, timestamps and an optimistic-lock version."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = True


class Audited:
    """Marker mixin: every create, update and delete of a subclass writes an AuditEntry (see audit.py)."""

    audit_exclude_fields: tuple[str, ...] = ("updated_at", "version")


# --- Identity ------------------------------------------------------------------------------------


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def create_user(self, email: str, password: str | None = None, **extra) -> "User":
        if not email:
            raise ValueError("email is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra) -> "User":
        extra.update(is_staff=True, is_superuser=True)
        return self.create_user(email, password, **extra)


class User(Audited, AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    audit_exclude_fields = ("password", "last_login")

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    def __str__(self) -> str:
        return self.email


class Role(models.TextChoices):
    SYSTEM_ADMIN = "system_admin", "System admin"
    SITE_MANAGER = "site_manager", "Site manager"
    WAREHOUSE_SUPERVISOR = "warehouse_supervisor", "Warehouse supervisor"
    WAREHOUSE_OPERATOR = "warehouse_operator", "Warehouse operator"
    DISPATCHER = "dispatcher", "Dispatcher"
    DRIVER = "driver", "Driver"
    CARRIER_DRIVER = "carrier_driver", "Carrier driver"
    BILLING_CLERK = "billing_clerk", "Billing clerk"
    CLIENT_ADMIN = "client_admin", "Client admin"
    CLIENT_USER = "client_user", "Client user"


CLIENT_ROLES = {Role.CLIENT_ADMIN, Role.CLIENT_USER}


class RoleGrant(Audited, BaseModel):
    """A role scoped to sites and/or clients. Empty scope lists mean none, never all."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="role_grants")
    role = models.CharField(max_length=40, choices=Role.choices)
    all_sites = models.BooleanField(default=False)
    all_clients = models.BooleanField(default=False)
    site_ids = ArrayField(models.UUIDField(), default=list, blank=True)
    client_ids = ArrayField(models.UUIDField(), default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="role_grant_user_role_uq"),
            # Client roles are always pinned to exactly one client.
            models.CheckConstraint(
                condition=~Q(role__in=["client_admin", "client_user"]) | (Q(all_clients=False) & Q(client_ids__len=1)),
                name="role_grant_client_role_single_client",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} · {self.role}"


class ApiClient(Audited, BaseModel):
    """Machine credentials for a client's systems (OAuth2 client-credentials grant)."""

    name = models.CharField(max_length=120)
    client_id = models.UUIDField(db_index=True, help_text="masterdata Party id of the owning client")
    key_id = models.CharField(max_length=40, unique=True)
    secret_hash = models.CharField(max_length=256)
    scopes = ArrayField(models.CharField(max_length=40), default=list, blank=True)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    audit_exclude_fields = ("secret_hash", "last_used_at", "updated_at", "version")

    def __str__(self) -> str:
        return f"{self.name} ({self.key_id})"


# --- Audit -----------------------------------------------------------------------------------------


class AuditEntry(models.Model):
    """Append-only. UPDATE and DELETE are rejected by a database trigger (migration 0002)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor_kind = models.CharField(max_length=20)
    actor_id = models.UUIDField(null=True, blank=True)
    actor_label = models.CharField(max_length=200, blank=True)
    action = models.CharField(max_length=80)
    entity_type = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=64)
    client_id = models.UUIDField(null=True, blank=True)
    changed_fields = ArrayField(models.CharField(max_length=80), default=list, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    request_id = models.UUIDField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["entity_type", "entity_id", "occurred_at"], name="audit_entity_idx"),
            models.Index(fields=["actor_id", "occurred_at"], name="audit_actor_idx"),
        ]
        ordering = ["-occurred_at"]

    def __str__(self) -> str:
        return f"{self.action} {self.entity_type}:{self.entity_id}"


# --- Outbox / inbox --------------------------------------------------------------------------------


class OutboxEvent(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    type = models.CharField(max_length=120)
    version = models.PositiveSmallIntegerField(default=1)
    aggregate_type = models.CharField(max_length=80)
    aggregate_id = models.CharField(max_length=64)
    client_id = models.UUIDField(null=True, blank=True)
    site_id = models.UUIDField(null=True, blank=True)
    correlation_id = models.UUIDField(null=True, blank=True)
    causation_id = models.UUIDField(null=True, blank=True)
    actor_id = models.UUIDField(null=True, blank=True)
    payload = models.JSONField()
    occurred_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["event_id"], name="outbox_unpublished_idx", condition=Q(published_at__isnull=True)),
            models.Index(fields=["aggregate_type", "aggregate_id"], name="outbox_aggregate_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.type} v{self.version} {self.event_id}"


class DeliveryStatus(models.TextChoices):
    PENDING = "pending"
    DELIVERED = "delivered"
    DEAD = "dead"


class EventDelivery(models.Model):
    """One row per (event, consumer). The relay fans out events into deliveries; workers run them."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(OutboxEvent, on_delete=models.PROTECT, related_name="deliveries")
    consumer = models.CharField(max_length=120)
    status = models.CharField(max_length=20, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    last_error = models.TextField(blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "consumer"], name="delivery_event_consumer_uq")]
        indexes = [models.Index(fields=["next_attempt_at"], name="delivery_pending_idx", condition=Q(status="pending"))]

    def __str__(self) -> str:
        return f"{self.consumer} ← {self.event_id} ({self.status})"


class InboxRecord(models.Model):
    consumer = models.CharField(max_length=120)
    event_id = models.UUIDField()
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["consumer", "event_id"], name="inbox_consumer_event_uq")]

    def __str__(self) -> str:
        return f"{self.consumer} {self.event_id}"


# --- Idempotency -----------------------------------------------------------------------------------


class IdempotencyRecord(models.Model):
    id = models.BigAutoField(primary_key=True)
    scope = models.CharField(max_length=80, help_text="principal the key belongs to, e.g. api_client:<id>")
    key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["scope", "key"], name="idempotency_scope_key_uq")]

    def __str__(self) -> str:
        return f"{self.scope}:{self.key}"


class SyncStatus(models.TextChoices):
    APPLIED = "applied"
    REJECTED = "rejected"


class SyncCommand(models.Model):
    """One offline command from a scanner, recorded once under its device-generated id (see sync.py)."""

    id = models.UUIDField(primary_key=True, help_text="command_id generated on the device")
    type = models.CharField(max_length=60)
    actor = models.CharField(max_length=80, help_text="principal that sent it, e.g. user:<id>")
    device_id = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=SyncStatus.choices)
    result = models.JSONField(null=True, blank=True)
    problem = models.JSONField(null=True, blank=True)
    recorded_at = models.DateTimeField(null=True, blank=True, help_text="device clock when the operator acted")
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["actor", "received_at"], name="sync_command_actor_idx")]

    def __str__(self) -> str:
        return f"{self.type} {self.id} ({self.status})"


# --- Documents (evidence) ----------------------------------------------------------------------------


class DocumentKind(models.TextChoices):
    POD_SIGNATURE = "pod_signature"
    POD_PHOTO = "pod_photo"
    RECEIPT_PHOTO = "receipt_photo"
    CUSTOMS = "customs"
    PERMIT = "permit"
    INSURANCE = "insurance"
    INVOICE = "invoice"
    EXPORT_PROOF = "export_proof"
    IMPORT_FILE = "import_file"
    OTHER = "other"


class Document(Audited, BaseModel):
    """Evidence metadata. Bytes live in object storage under `storage_key`; never overwritten."""

    client_id = models.UUIDField(null=True, blank=True, db_index=True)
    kind = models.CharField(max_length=30, choices=DocumentKind.choices)
    storage_key = models.CharField(max_length=300, unique=True)
    original_name = models.CharField(max_length=255, blank=True)
    sha256 = models.CharField(max_length=64)
    mime = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    captured_at = models.DateTimeField()
    captured_by = models.UUIDField(null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    linked_type = models.CharField(max_length=80, blank=True)
    linked_id = models.CharField(max_length=64, blank=True)
    expires_on = models.DateField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["linked_type", "linked_id"], name="document_link_idx")]

    def __str__(self) -> str:
        return f"{self.kind} {self.original_name or self.id}"


# --- Exception queue ---------------------------------------------------------------------------------


class Severity(models.TextChoices):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ExceptionStatus(models.TextChoices):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


class ExceptionCase(Audited, BaseModel):
    """One queue for operational and reconciliation exceptions."""

    kind = models.CharField(max_length=80, db_index=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.MEDIUM)
    status = models.CharField(max_length=20, choices=ExceptionStatus.choices, default=ExceptionStatus.OPEN)
    summary = models.CharField(max_length=300)
    detail = models.JSONField(default=dict, blank=True)
    client_id = models.UUIDField(null=True, blank=True, db_index=True)
    site_id = models.UUIDField(null=True, blank=True)
    entity_type = models.CharField(max_length=80, blank=True)
    entity_id = models.CharField(max_length=64, blank=True)
    dedupe_key = models.CharField(max_length=200, blank=True)
    assigned_to = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    due_at = models.DateTimeField(null=True, blank=True)
    resolution = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # A recurring detection (e.g. a nightly reconciliation) does not open duplicates while open.
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=~Q(status="resolved") & ~Q(dedupe_key=""),
                name="exception_open_dedupe_uq",
            ),
            models.CheckConstraint(
                condition=~Q(status="resolved") | ~Q(resolution=""),
                name="exception_resolution_required",
            ),
        ]
        indexes = [models.Index(fields=["status", "severity", "created_at"], name="exception_queue_idx")]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.kind}: {self.summary}"


# --- Business numbers ----------------------------------------------------------------------------------


class NumberSequence(models.Model):
    """Human-readable business numbers (e.g. ORD-2027-000123), separate from primary keys."""

    name = models.CharField(max_length=40, primary_key=True)
    prefix = models.CharField(max_length=10)
    year = models.PositiveSmallIntegerField()
    next_value = models.PositiveBigIntegerField(default=1)

    def __str__(self) -> str:
        return f"{self.prefix} {self.year} → {self.next_value}"
