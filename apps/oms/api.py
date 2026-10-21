import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from django.db.models import QuerySet
from ninja import Field, Router, Schema, Status

from apps.masterdata import services as masterdata
from apps.masterdata.models import Site
from apps.platform.apiutils import Problem, acting_client_id, get_scoped, paginate, principal_of, require_roles
from apps.platform.auth import Principal
from apps.platform.models import Role

from . import services
from .models import InboundKind, InboundNotice, InboundStatus

router = Router(tags=["orders"])

STAFF_WRITERS = (Role.SITE_MANAGER, Role.WAREHOUSE_SUPERVISOR)


class InboundLineIn(Schema):
    sku: str | None = None
    barcode: str | None = None
    qty: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=3)]
    uom: str = ""
    lot: str = ""
    expiry: date | None = None


class InboundNoticeIn(Schema):
    client_id: uuid.UUID | None = Field(None, description="Staff only; client systems always act for themselves")
    site_code: str
    expected_at: datetime
    kind: InboundKind = InboundKind.PURCHASE
    client_ref: str = Field("", max_length=60)
    notes: str = ""
    lines: list[InboundLineIn] = Field(..., min_length=1, max_length=500)


class InboundLineOut(Schema):
    id: uuid.UUID
    line_no: int
    item_id: uuid.UUID
    sku: str
    qty: Decimal
    uom: str
    lot: str
    expiry: date | None
    qty_received: Decimal
    status: str

    @staticmethod
    def resolve_sku(obj) -> str:
        return obj.item.sku


class InboundNoticeOut(Schema):
    id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    client_ref: str
    kind: str
    status: str
    expected_at: datetime
    notes: str
    cancelled_reason: str
    created_at: datetime
    lines: list[InboundLineOut]

    @staticmethod
    def resolve_lines(obj: InboundNotice):
        return list(obj.lines.select_related("item").order_by("line_no"))


class InboundNoticeSummary(Schema):
    id: uuid.UUID
    number: str
    client_id: uuid.UUID
    site_id: uuid.UUID
    client_ref: str
    kind: str
    status: str
    expected_at: datetime


class InboundNoticePage(Schema):
    items: list[InboundNoticeSummary]
    next_cursor: uuid.UUID | None


class CancelIn(Schema):
    reason: str


def _notices(principal: Principal) -> QuerySet:
    # Staff are limited to their sites as well as their clients; a client sees its notices at every site.
    return principal.scope(InboundNotice.objects.all(), site_field=None if principal.is_client_scoped else "site_id")


@router.post("/inbound-notices", response={201: InboundNoticeOut})
def create_inbound_notice(request, body: InboundNoticeIn):
    principal = principal_of(request)
    if principal.is_client_scoped and principal.kind == "user":
        require_roles(principal, Role.CLIENT_ADMIN, Role.CLIENT_USER)
    client = masterdata.require_client(acting_client_id(principal, body.client_id, *STAFF_WRITERS))
    site = Site.objects.filter(code=body.site_code.strip().upper(), is_active=True).first()
    if site is None or (not principal.is_client_scoped and not principal.can_see_site(site.pk)):
        raise Problem(422, "Unknown site", f"No site {body.site_code!r}.")
    notice = services.create_inbound_notice(
        client=client,
        site=site,
        expected_at=body.expected_at,
        kind=body.kind,
        client_ref=body.client_ref,
        notes=body.notes,
        lines=[services.NoticeLineIn(**line.dict()) for line in body.lines],
    )
    return Status(201, notice)


@router.get("/inbound-notices", response=InboundNoticePage)
def list_inbound_notices(
    request,
    status: InboundStatus | None = None,
    site_id: uuid.UUID | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = 50,
):
    qs = _notices(principal_of(request))
    if status:
        qs = qs.filter(status=status)
    if site_id:
        qs = qs.filter(site_id=site_id)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/inbound-notices/{notice_id}", response=InboundNoticeOut)
def get_inbound_notice(request, notice_id: uuid.UUID):
    return get_scoped(_notices(principal_of(request)), notice_id)


@router.post("/inbound-notices/{notice_id}/cancel", response=InboundNoticeOut)
def cancel_inbound_notice(request, notice_id: uuid.UUID, body: CancelIn):
    principal = principal_of(request)
    if not principal.is_client_scoped:
        require_roles(principal, *STAFF_WRITERS)
    elif principal.kind == "user":
        require_roles(principal, Role.CLIENT_ADMIN, Role.CLIENT_USER)
    notice = get_scoped(_notices(principal), notice_id)
    return services.cancel_inbound_notice(notice.pk, body.reason)
