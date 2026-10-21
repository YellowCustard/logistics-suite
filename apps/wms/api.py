import uuid
from typing import Annotated

from ninja import File, Router, Schema, Status
from ninja.files import UploadedFile
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from apps.masterdata.models import Site
from apps.platform import csvimport
from apps.platform.apiutils import Problem, paginate, principal_of, require_roles, require_staff
from apps.platform.models import Role

from .models import Location, LocationKind

router = Router(tags=["warehouse"])


class LocationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: Annotated[str, StringConstraints(min_length=1, max_length=30, to_upper=True)]
    zone: str = ""
    kind: LocationKind = LocationKind.STORAGE
    pallet_capacity: Annotated[int, Field(ge=0, le=100)] = 1
    pick_sequence: Annotated[int, Field(ge=0)] = 0


class LocationOut(Schema):
    id: uuid.UUID
    site_id: uuid.UUID
    code: str
    zone: str
    kind: str
    pallet_capacity: int
    pick_sequence: int
    is_active: bool


class LocationPage(Schema):
    items: list[LocationOut]
    next_cursor: uuid.UUID | None


class ImportOut(Schema):
    created: int
    document_id: str


def _site(principal, site_id: uuid.UUID) -> Site:
    require_staff(principal)
    site = Site.objects.filter(pk=site_id).first()
    if site is None or not principal.can_see_site(site.pk):
        raise Problem(404, "Not found", "Unknown site.")
    return site


@router.get("/sites/{site_id}/locations", response=LocationPage)
def list_locations(request, site_id: uuid.UUID, cursor: uuid.UUID | None = None, limit: int = 100):
    site = _site(principal_of(request), site_id)
    items, next_cursor = paginate(Location.objects.filter(site=site), cursor, limit, max_limit=500)
    return {"items": items, "next_cursor": next_cursor}


@router.post("/sites/{site_id}/locations:import", response={201: ImportOut})
def import_locations(request, site_id: uuid.UUID, file: File[UploadedFile]):
    principal = principal_of(request)
    require_roles(principal, Role.SITE_MANAGER)
    site = _site(principal, site_id)

    def create(row: LocationRow) -> Location:
        return Location.objects.create(
            site=site,
            code=row.code,
            zone=(row.zone or "").upper(),
            kind=row.kind,
            pallet_capacity=row.pallet_capacity,
            pick_sequence=row.pick_sequence,
        )

    return Status(201, csvimport.run(file, LocationRow, create, linked_type="masterdata.site", linked_id=str(site.pk)))
