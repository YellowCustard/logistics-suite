import uuid
from datetime import datetime
from decimal import Decimal

from django.db import connection
from django.http import FileResponse
from django.utils import timezone
from ninja import File, Form, Router, Schema, Status
from ninja.files import UploadedFile

from . import documents, exceptions_queue
from .apiutils import Problem, get_scoped, paginate, principal_of, require_roles, require_staff
from .auth import issue_token
from .models import Document, DocumentKind, ExceptionCase, OutboxEvent, Role

auth_router = Router(tags=["auth"])
platform_router = Router(tags=["platform"])
health_router = Router(tags=["operations"])


# --- OAuth2 client credentials ------------------------------------------------------------------------


class TokenOut(Schema):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int


@auth_router.post("/token", auth=None, response=TokenOut)
def token(request, grant_type: Form[str], client_id: Form[str], client_secret: Form[str]):
    if grant_type != "client_credentials":
        raise Problem(400, "unsupported_grant_type", "Only client_credentials is supported.")
    issued = issue_token(client_id, client_secret)
    if issued is None:
        raise Problem(401, "invalid_client", "Unknown client or wrong secret.")
    access_token, ttl = issued
    return TokenOut(access_token=access_token, expires_in=ttl)


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
