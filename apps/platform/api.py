import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db import connection
from django.http import FileResponse
from django.utils import timezone
from ninja import Field, File, Form, Router, Schema, Status
from ninja.files import UploadedFile
from ninja.throttling import AnonRateThrottle
from ninja.utils import check_csrf

from . import documents, exceptions_queue, sync, webhooks
from .apiutils import Problem, acting_client_id, get_scoped, paginate, principal_of, require_roles, require_staff
from .auth import issue_token, principal_for_user
from .models import Document, DocumentKind, ExceptionCase, OutboxEvent, Role, WebhookDelivery, WebhookSubscription

auth_router = Router(tags=["auth"])
platform_router = Router(tags=["platform"])
health_router = Router(tags=["operations"])


# --- OAuth2 client credentials ------------------------------------------------------------------------


class TokenOut(Schema):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int


# Per worker process (local-memory cache), so the effective limit is this times the worker count.
LOGIN_THROTTLE = [AnonRateThrottle(settings.LOGIN_RATE_LIMIT)]


@auth_router.post("/token", auth=None, response=TokenOut, throttle=LOGIN_THROTTLE)
def token(request, grant_type: Form[str], client_id: Form[str], client_secret: Form[str]):
    if grant_type != "client_credentials":
        raise Problem(400, "unsupported_grant_type", "Only client_credentials is supported.")
    issued = issue_token(client_id, client_secret)
    if issued is None:
        raise Problem(401, "invalid_client", "Unknown client or wrong secret.")
    access_token, ttl = issued
    return TokenOut(access_token=access_token, expires_in=ttl)


# --- Browser sessions (scanner PWA) ---------------------------------------------------------------------


class LoginIn(Schema):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=256)


class SessionOut(Schema):
    user_id: uuid.UUID
    email: str
    roles: list[str]
    all_sites: bool
    site_ids: list[uuid.UUID]


def _session_out(request) -> SessionOut:
    principal = principal_of(request)
    return SessionOut(
        user_id=principal.id,
        email=principal.label,
        roles=sorted(principal.roles),
        all_sites=principal.all_sites,
        site_ids=sorted(principal.site_ids),
    )


@auth_router.post("/session", auth=None, response=SessionOut, throttle=LOGIN_THROTTLE)
def create_session(request, body: LoginIn):
    # Ninja skips CSRF on unauthenticated operations; check it here so another site can't log a device in.
    if check_csrf(request) is not None:
        raise Problem(403, "CSRF check failed", "Reload the page and sign in again.")
    user = authenticate(request, username=body.email.strip(), password=body.password)
    if user is None:
        raise Problem(401, "Sign-in failed", "Wrong email or password.")
    login(request, user)  # rotates the session key and the CSRF token
    request.auth = principal_for_user(user)
    if request.auth.is_client_scoped:
        logout(request)
        raise Problem(403, "Forbidden", "Client users sign in to the client portal, not the scanner.")
    return _session_out(request)


@auth_router.get("/session", response=SessionOut)
def get_session(request):
    return _session_out(request)


@auth_router.delete("/session", response={204: None})
def delete_session(request):
    logout(request)
    return Status(204, None)


# --- Offline command sync ----------------------------------------------------------------------------------


class SyncCommandIn(Schema):
    command_id: uuid.UUID
    type: str = Field(..., max_length=60)
    payload: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime | None = None


class SyncPushIn(Schema):
    device_id: str = Field(..., min_length=1, max_length=64)
    commands: list[SyncCommandIn] = Field(..., max_length=100)


class SyncOutcomeOut(Schema):
    command_id: uuid.UUID
    status: str
    replayed: bool
    result: dict[str, Any] | None
    problem: dict[str, Any] | None


class SyncPushOut(Schema):
    results: list[SyncOutcomeOut]


@platform_router.post("/sync/push", response=SyncPushOut)
def sync_push(request, body: SyncPushIn):
    """Apply queued scanner commands in order. Results come back in the same order; processing stops at the
    first `retry` (later commands may depend on it), and the device resends everything without a result."""
    principal = principal_of(request)
    require_staff(principal)
    results = []
    for cmd in body.commands:
        outcome = sync.apply(
            request,
            principal,
            device_id=body.device_id,
            command_id=cmd.command_id,
            type_=cmd.type,
            payload=cmd.payload,
            recorded_at=cmd.recorded_at,
        )
        results.append(outcome)
        if outcome.status == sync.RETRY:
            break
    return {"results": results}


# --- Documents -------------------------------------------------------------------------------------------


class DocumentOut(Schema):
    id: uuid.UUID
    client_id: uuid.UUID | None
    kind: str
    original_name: str
    sha256: str
    mime: str
    size_bytes: int
    captured_at: datetime
    linked_type: str
    linked_id: str


@platform_router.post("/documents", response={201: DocumentOut})
def upload_document(
    request,
    file: File[UploadedFile],
    kind: Form[DocumentKind],
    client_id: Form[uuid.UUID | None] = None,
    linked_type: Form[str] = "",
    linked_id: Form[str] = "",
    captured_at: Form[datetime | None] = None,
    latitude: Form[Decimal | None] = None,
    longitude: Form[Decimal | None] = None,
):
    principal = principal_of(request)
    if principal.is_client_scoped:
        client_id = next(iter(principal.client_ids))
    elif client_id is not None and not principal.can_see_client(client_id):
        raise Problem(404, "Not found", "Unknown client.")
    doc = documents.store(
        file,
        kind=kind,
        client_id=client_id,
        captured_by=principal.id,
        captured_at=captured_at,
        linked_type=linked_type,
        linked_id=linked_id,
        latitude=latitude,
        longitude=longitude,
    )
    return Status(201, doc)


def _documents_for(principal):
    qs = Document.objects.all()
    if principal.all_clients:
        return qs
    return principal.scope(qs)


@platform_router.get("/documents/{document_id}", response=DocumentOut)
def get_document(request, document_id: uuid.UUID):
    return get_scoped(_documents_for(principal_of(request)), document_id)


@platform_router.get("/documents/{document_id}/download")
def download_document(request, document_id: uuid.UUID):
    doc = get_scoped(_documents_for(principal_of(request)), document_id)
    return FileResponse(
        documents.open_bytes(doc), content_type=doc.mime, as_attachment=True, filename=doc.original_name or f"{doc.id}"
    )


# --- Exception queue -----------------------------------------------------------------------------------------


class ExceptionOut(Schema):
    id: uuid.UUID
    kind: str
    severity: str
    status: str
    summary: str
    detail: dict
    client_id: uuid.UUID | None
    site_id: uuid.UUID | None
    entity_type: str
    entity_id: str
    assigned_to_id: uuid.UUID | None
    resolution: str
    created_at: datetime
    resolved_at: datetime | None


class ExceptionPage(Schema):
    items: list[ExceptionOut]
    next_cursor: uuid.UUID | None


class ResolveIn(Schema):
    resolution: str


def _exceptions_for(principal):
    require_staff(principal)
    qs = ExceptionCase.objects.all()
    if not principal.all_clients:
        # Staff limited to some clients see those clients' cases plus cases tied to no client.
        qs = qs.filter(client_id__in=principal.client_ids) | qs.filter(client_id__isnull=True)
    return qs


@platform_router.get("/exceptions", response=ExceptionPage)
def list_exceptions(
    request, status: str | None = None, kind: str | None = None, cursor: uuid.UUID | None = None, limit: int = 50
):
    qs = _exceptions_for(principal_of(request))
    if status:
        qs = qs.filter(status=status)
    if kind:
        qs = qs.filter(kind=kind)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@platform_router.post("/exceptions/{case_id}/resolve", response=ExceptionOut)
def resolve_exception(request, case_id: uuid.UUID, body: ResolveIn):
    principal = principal_of(request)
    require_roles(principal, Role.SITE_MANAGER, Role.WAREHOUSE_SUPERVISOR, Role.BILLING_CLERK, Role.DISPATCHER)
    case = get_scoped(_exceptions_for(principal), case_id)
    return exceptions_queue.resolve(case, body.resolution)


# --- Health ----------------------------------------------------------------------------------------------------


@health_router.get("/healthz", auth=None)
def healthz(request):
    return {"status": "ok"}


@health_router.get("/readyz", auth=None, response={200: dict, 503: dict})
def readyz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return Status(503, {"status": "unavailable", "database": "down"})
    oldest = (
        OutboxEvent.objects.filter(published_at__isnull=True)
        .order_by("event_id")
        .values_list("occurred_at", flat=True)
        .first()
    )
    lag = (timezone.now() - oldest).total_seconds() if oldest else 0.0
    return Status(200, {"status": "ok", "database": "up", "outbox_lag_seconds": round(lag, 1)})


# --- Webhooks ---------------------------------------------------------------------------------------------------

WEBHOOK_ADMINS = (Role.CLIENT_ADMIN, "client_system")


class WebhookIn(Schema):
    url: str = Field(max_length=500)
    event_types: list[str] = Field(min_length=1, max_length=len(webhooks.WEBHOOK_EVENTS))
    description: str = Field("", max_length=200)
    client_id: uuid.UUID | None = Field(
        None, description="Staff only; client users and systems act for their own client"
    )


class WebhookOut(Schema):
    id: uuid.UUID
    client_id: uuid.UUID
    url: str
    event_types: list[str]
    description: str
    is_active: bool
    created_at: datetime


class WebhookCreatedOut(WebhookOut):
    secret: str = Field(description="Signing secret. Shown once; store it now.")


class WebhookDeliveryOut(Schema):
    id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    status: str
    attempts: int
    last_status_code: int | None
    last_error: str
    created_at: datetime
    delivered_at: datetime | None

    @staticmethod
    def resolve_event_type(obj: WebhookDelivery):
        return obj.event.type


class WebhookEventsOut(Schema):
    event_types: list[str]


def _webhook_admin(principal) -> None:
    if principal.is_client_scoped:
        require_roles(principal, *WEBHOOK_ADMINS)
    else:
        require_roles(principal, Role.SYSTEM_ADMIN)


def _subscriptions(principal):
    _webhook_admin(principal)
    return principal.scope(WebhookSubscription.objects.all())


@platform_router.get("/webhooks/events", response=WebhookEventsOut)
def webhook_events(request):
    """Event types a webhook can subscribe to."""
    principal_of(request)
    return {"event_types": list(webhooks.WEBHOOK_EVENTS)}


@platform_router.post("/webhooks", response={201: WebhookCreatedOut})
def create_webhook(request, body: WebhookIn):
    """Subscribe an HTTPS endpoint to events about one client. The response carries the signing secret once."""
    principal = principal_of(request)
    _webhook_admin(principal)
    client_id = acting_client_id(principal, body.client_id, Role.SYSTEM_ADMIN)
    subscription, secret = webhooks.create_subscription(
        client_id=client_id,
        url=body.url,
        event_types=body.event_types,
        description=body.description,
        created_by=f"{principal.kind}:{principal.label}",
    )
    return Status(201, {**WebhookOut.from_orm(subscription).model_dump(), "secret": secret})


@platform_router.get("/webhooks", response=list[WebhookOut])
def list_webhooks(request, include_inactive: bool = False):
    qs = _subscriptions(principal_of(request))
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return list(qs.order_by("created_at"))


@platform_router.delete("/webhooks/{subscription_id}", response=WebhookOut)
def disable_webhook(request, subscription_id: uuid.UUID):
    """Stop sending. Deliveries still queued are dropped. Create a new subscription to start again."""
    subscription = get_scoped(_subscriptions(principal_of(request)), subscription_id)
    if subscription.is_active:
        subscription.is_active = False
        subscription.save()
    return subscription


@platform_router.post("/webhooks/{subscription_id}/ping", response={202: WebhookDeliveryOut})
def ping_webhook(request, subscription_id: uuid.UUID):
    """Queue a signed `platform.webhook.ping` delivery to check the endpoint and signature handling."""
    subscription = get_scoped(_subscriptions(principal_of(request)), subscription_id)
    if not subscription.is_active:
        raise Problem(409, "Webhook disabled")
    return Status(202, webhooks.ping(subscription))


@platform_router.get("/webhooks/{subscription_id}/deliveries", response=list[WebhookDeliveryOut])
def list_webhook_deliveries(request, subscription_id: uuid.UUID, limit: int = 50):
    subscription = get_scoped(_subscriptions(principal_of(request)), subscription_id)
    limit = max(1, min(limit, 200))
    return list(subscription.deliveries.select_related("event").order_by("-created_at")[:limit])
