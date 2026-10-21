import uuid
from datetime import date, datetime
from decimal import Decimal

from django.db.models import QuerySet
from ninja import File, Header, Router, Schema, Status
from ninja.files import UploadedFile

from apps.platform import csvimport
from apps.platform.apiutils import Problem, get_scoped, paginate, principal_of, require_roles, require_staff
from apps.platform.auth import Principal
from apps.platform.models import Role

from . import imports, services
from .models import (
    Contract,
    Item,
    Party,
    PartyRoleType,
    Person,
    RateCard,
    RateCardStatus,
    Site,
    Vehicle,
)

router = Router(tags=["master data"])


class ImportOut(Schema):
    created: int
    document_id: str


# --- Clients ----------------------------------------------------------------------------------------------


class ClientIn(Schema):
    code: str
    legal_name: str
    trading_name: str = ""
    registration_no: str = ""
    vat_number: str = ""
    vat_exempt: bool = False
    sage_customer_code: str = ""
    billing_email: str = ""
    address: str = ""


class ClientPatch(Schema):
    legal_name: str | None = None
    trading_name: str | None = None
    vat_number: str | None = None
    sage_customer_code: str | None = None
    billing_email: str | None = None
    address: str | None = None
    is_active: bool | None = None


class ClientOut(Schema):
    id: uuid.UUID
    code: str
    legal_name: str
    trading_name: str
    vat_number: str
    vat_exempt: bool
    sage_customer_code: str
    billing_email: str
    is_active: bool
    version: int
    created_at: datetime


class ClientPage(Schema):
    items: list[ClientOut]
    next_cursor: uuid.UUID | None


def _clients(principal: Principal) -> QuerySet:
    qs = Party.objects.filter(roles__role=PartyRoleType.CLIENT)
    return qs if principal.all_clients else qs.filter(pk__in=principal.client_ids)


@router.get("/clients", response=ClientPage)
def list_clients(request, cursor: uuid.UUID | None = None, limit: int = 50):
    items, next_cursor = paginate(_clients(principal_of(request)), cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.post("/clients", response={201: ClientOut})
def create_client(request, body: ClientIn):
    require_roles(principal_of(request), Role.SYSTEM_ADMIN)
    data = body.dict()
    party = services.create_party(roles=[PartyRoleType.CLIENT], **data)
    return Status(201, party)


@router.post("/clients:import", response={201: ImportOut})
def import_clients(request, file: File[UploadedFile]):
    require_roles(principal_of(request), Role.SYSTEM_ADMIN)
    result = csvimport.run(file, imports.ClientRow, imports.create_client, linked_type="masterdata.party")
    return Status(201, result)


@router.get("/clients/{client_id}", response=ClientOut)
def get_client(request, client_id: uuid.UUID):
    return get_scoped(_clients(principal_of(request)), client_id)


@router.patch("/clients/{client_id}", response=ClientOut)
def patch_client(request, client_id: uuid.UUID, body: ClientPatch, if_match: Header[int]):
    principal = principal_of(request)
    require_roles(principal, Role.SYSTEM_ADMIN)
    party = get_scoped(_clients(principal), client_id)
    services.update_with_version(party, if_match, body.dict(exclude_unset=True))
    return party


# --- Sites -----------------------------------------------------------------------------------------------------


class SiteIn(Schema):
    code: str
    name: str
    kind: str
    address: str = ""
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    pallet_capacity: int | None = None


class SiteOut(SiteIn):
    id: uuid.UUID
    is_active: bool


class SitePage(Schema):
    items: list[SiteOut]
    next_cursor: uuid.UUID | None


def _sites(principal: Principal) -> QuerySet:
    require_staff(principal)
    qs = Site.objects.all()
    return qs if principal.all_sites else qs.filter(pk__in=principal.site_ids)


@router.get("/sites", response=SitePage)
def list_sites(request, cursor: uuid.UUID | None = None, limit: int = 50):
    items, next_cursor = paginate(_sites(principal_of(request)), cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.post("/sites", response={201: SiteOut})
def create_site(request, body: SiteIn):
    require_roles(principal_of(request), Role.SYSTEM_ADMIN)
    return Status(201, imports.create_site(imports.SiteRow.model_validate(body.dict())))


@router.post("/sites:import", response={201: ImportOut})
def import_sites(request, file: File[UploadedFile]):
    require_roles(principal_of(request), Role.SYSTEM_ADMIN)
    return Status(201, csvimport.run(file, imports.SiteRow, imports.create_site, linked_type="masterdata.site"))


# --- Items -----------------------------------------------------------------------------------------------------


class ItemOut(Schema):
    id: uuid.UUID
    client_id: uuid.UUID
    sku: str
    description: str
    base_uom: str
    weight_kg: Decimal | None
    volume_m3: Decimal | None
    units_per_pallet: int | None
    lot_tracked: bool
    expiry_tracked: bool
    is_active: bool


class ItemPage(Schema):
    items: list[ItemOut]
    next_cursor: uuid.UUID | None


def _target_client(principal: Principal, client_id: uuid.UUID | None) -> Party:
    """Client principals act on their own client; staff must name one they can see."""
    if principal.is_client_scoped:
        if principal.kind == "user":
            require_roles(principal, Role.CLIENT_ADMIN)
        return services.require_client(next(iter(principal.client_ids)))
    require_roles(principal, Role.SYSTEM_ADMIN)
    if client_id is None or not principal.can_see_client(client_id):
        raise Problem(422, "client_id required", "Name the client this item belongs to.")
    return services.require_client(client_id)


@router.get("/items", response=ItemPage)
def list_items(request, sku: str | None = None, cursor: uuid.UUID | None = None, limit: int = 50):
    qs = principal_of(request).scope(Item.objects.all())
    if sku:
        qs = qs.filter(sku=sku)
    items, next_cursor = paginate(qs, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.post("/items", response={201: ItemOut})
def create_item(request, body: imports.ItemRow, client_id: uuid.UUID | None = None):
    client = _target_client(principal_of(request), client_id)
    return Status(201, imports.item_writer(client)(body))


@router.post("/items:import", response={201: ImportOut})
def import_items(request, file: File[UploadedFile], client_id: uuid.UUID | None = None):
    client = _target_client(principal_of(request), client_id)
    result = csvimport.run(
        file,
        imports.ItemRow,
        imports.item_writer(client),
        client_id=client.pk,
        linked_type="masterdata.party",
        linked_id=str(client.pk),
    )
    return Status(201, result)


@router.get("/items/{item_id}", response=ItemOut)
def get_item(request, item_id: uuid.UUID):
    return get_scoped(principal_of(request).scope(Item.objects.all()), item_id)


# --- Vehicles and drivers ----------------------------------------------------------------------------------------


class VehicleOut(Schema):
    id: uuid.UUID
    registration: str
    fleet_number: str
    telematics_id: str | None
    carrier_id: uuid.UUID | None
    capacity_kg: int | None
    pallet_slots: int | None
    licence_disc_expiry: date | None
    roadworthy_expiry: date | None
    is_active: bool


class VehiclePage(Schema):
    items: list[VehicleOut]
    next_cursor: uuid.UUID | None


class DriverOut(Schema):
    """Personal fields (ID number, phone) are never returned by the API."""

    id: uuid.UUID
    first_name: str
    last_name: str
    employee_code: str | None
    carrier_id: uuid.UUID | None
    is_driver: bool
    licence_expiry: date | None
    prdp_expiry: date | None
    is_active: bool


class DriverPage(Schema):
    items: list[DriverOut]
    next_cursor: uuid.UUID | None


@router.get("/vehicles", response=VehiclePage)
def list_vehicles(request, cursor: uuid.UUID | None = None, limit: int = 50):
    require_staff(principal_of(request))
    items, next_cursor = paginate(Vehicle.objects.all(), cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.post("/vehicles:import", response={201: ImportOut})
def import_vehicles(request, file: File[UploadedFile]):
    require_roles(principal_of(request), Role.SYSTEM_ADMIN)
    return Status(
        201, csvimport.run(file, imports.VehicleRow, imports.create_vehicle, linked_type="masterdata.vehicle")
    )


@router.get("/drivers", response=DriverPage)
def list_drivers(request, cursor: uuid.UUID | None = None, limit: int = 50):
    principal = principal_of(request)
    require_roles(principal, Role.DISPATCHER, Role.SITE_MANAGER)
    items, next_cursor = paginate(Person.objects.filter(is_driver=True), cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.post("/drivers:import", response={201: ImportOut})
def import_drivers(request, file: File[UploadedFile]):
    require_roles(principal_of(request), Role.SYSTEM_ADMIN)
    return Status(201, csvimport.run(file, imports.PersonRow, imports.create_person, linked_type="masterdata.person"))


# --- Contracts and rate cards ---------------------------------------------------------------------------------------


class ContractIn(Schema):
    client_id: uuid.UUID
    reference: str
    valid_from: date
    valid_to: date | None = None
    payment_terms_days: int = 30


class ContractOut(ContractIn):
    id: uuid.UUID


class RateCardIn(Schema):
    valid_from: date
    valid_to: date | None = None
    notes: str = ""


class RateLineOut(Schema):
    id: uuid.UUID
    charge_type: str
    uom: str
    unit_price: Decimal
    currency: str
    min_charge: Decimal | None
    site_id: uuid.UUID | None
    tax_code: str


class RateCardOut(Schema):
    id: uuid.UUID
    contract_id: uuid.UUID
    number: int
    status: str
    valid_from: date
    valid_to: date | None
    published_at: datetime | None
    lines: list[RateLineOut]

    @staticmethod
    def resolve_lines(obj: RateCard):
        return list(obj.lines.order_by("charge_type", "site_id"))


def _contracts(principal: Principal) -> QuerySet:
    return principal.scope(Contract.objects.all())


def _rate_cards(principal: Principal) -> QuerySet:
    qs = RateCard.objects.select_related("contract")
    if principal.is_client_scoped:  # clients only see what they are billed against
        qs = qs.filter(status=RateCardStatus.PUBLISHED)
    return principal.scope(qs, client_field="contract__client_id")


@router.post("/contracts", response={201: ContractOut})
def create_contract(request, body: ContractIn):
    require_roles(principal_of(request), Role.SYSTEM_ADMIN)
    client = services.require_client(body.client_id)
    data = body.dict(exclude={"client_id"})
    return Status(201, Contract.objects.create(client=client, **data))


@router.post("/contracts/{contract_id}/rate-cards", response={201: RateCardOut})
def create_rate_card(request, contract_id: uuid.UUID, body: RateCardIn):
    principal = principal_of(request)
    require_roles(principal, Role.SYSTEM_ADMIN)
    contract = get_scoped(_contracts(principal), contract_id)
    return Status(201, services.create_rate_card(contract=contract, **body.dict()))


@router.post("/rate-cards/{card_id}/lines:import", response={201: ImportOut})
def import_rate_lines(request, card_id: uuid.UUID, file: File[UploadedFile]):
    principal = principal_of(request)
    require_roles(principal, Role.SYSTEM_ADMIN)
    card = get_scoped(_rate_cards(principal), card_id)
    result = csvimport.run(
        file,
        imports.RateLineRow,
        imports.rate_line_writer(card),
        client_id=card.contract.client_id,
        linked_type="masterdata.ratecard",
        linked_id=str(card.pk),
    )
    return Status(201, result)


@router.post("/rate-cards/{card_id}/publish", response=RateCardOut)
def publish_rate_card(request, card_id: uuid.UUID):
    principal = principal_of(request)
    require_roles(principal, Role.SYSTEM_ADMIN)
    return services.publish_rate_card(get_scoped(_rate_cards(principal), card_id))


@router.get("/rate-cards/{card_id}", response=RateCardOut)
def get_rate_card(request, card_id: uuid.UUID):
    return get_scoped(_rate_cards(principal_of(request)), card_id)
