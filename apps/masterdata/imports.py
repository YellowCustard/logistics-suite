"""CSV row schemas and row writers for master data onboarding (spec section 21)."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from apps.platform.apiutils import Problem

from . import services
from .models import (
    ChargeType,
    Currency,
    Item,
    ItemBarcode,
    ItemUom,
    Party,
    PartyRoleType,
    Person,
    RateCard,
    RateLine,
    Site,
    SiteKind,
    Vehicle,
)

Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
Email = Annotated[str, StringConstraints(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)]


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClientRow(_Row):
    code: Code
    legal_name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    trading_name: str = ""
    registration_no: str = ""
    vat_number: str = ""
    vat_exempt: bool = False
    sage_customer_code: str = ""
    billing_email: Email | None = None


def create_client(row: ClientRow) -> Party:
    return services.create_party(
        code=row.code,
        legal_name=row.legal_name,
        roles=[PartyRoleType.CLIENT],
        vat_number=row.vat_number or "",
        vat_exempt=row.vat_exempt,
        trading_name=row.trading_name or "",
        registration_no=row.registration_no or "",
        sage_customer_code=row.sage_customer_code or "",
        billing_email=row.billing_email or "",
    )


class SiteRow(_Row):
    code: Code
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    kind: SiteKind
    address: str = ""
    latitude: Annotated[Decimal, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[Decimal, Field(ge=-180, le=180)] | None = None
    pallet_capacity: Annotated[int, Field(ge=0)] | None = None


def create_site(row: SiteRow) -> Site:
    return Site.objects.create(
        code=row.code.upper(),
        name=row.name,
        kind=row.kind,
        address=row.address or "",
        latitude=row.latitude,
        longitude=row.longitude,
        pallet_capacity=row.pallet_capacity,
    )


class ItemRow(_Row):
    sku: Annotated[str, StringConstraints(min_length=1, max_length=60)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    base_uom: Annotated[str, StringConstraints(min_length=1, max_length=10)] = "EA"
    weight_kg: Annotated[Decimal, Field(ge=0)] | None = None
    volume_m3: Annotated[Decimal, Field(ge=0)] | None = None
    units_per_pallet: Annotated[int, Field(ge=1)] | None = None
    lot_tracked: bool = False
    expiry_tracked: bool = False
    case_uom: str | None = None
    case_factor: Annotated[int, Field(ge=2)] | None = None
    barcode: Annotated[str, StringConstraints(max_length=64)] | None = None


def item_writer(client: Party):
    def create_item(row: ItemRow) -> Item:
        if bool(row.case_uom) != bool(row.case_factor):
            raise Problem(422, "Incomplete pack size", "case_uom and case_factor go together.")
        item = Item.objects.create(
            client=client,
            sku=row.sku,
            description=row.description,
            base_uom=row.base_uom.upper(),
            weight_kg=row.weight_kg,
            volume_m3=row.volume_m3,
            units_per_pallet=row.units_per_pallet,
            lot_tracked=row.lot_tracked,
            expiry_tracked=row.expiry_tracked,
        )
        ItemUom.objects.create(item=item, uom=item.base_uom, factor=1)
        if row.case_uom and row.case_factor:
            ItemUom.objects.create(item=item, uom=row.case_uom.upper(), factor=row.case_factor)
        if row.barcode:
            ItemBarcode.objects.create(item=item, client=client, barcode=row.barcode, uom=item.base_uom)
        return item

    return create_item


class RateLineRow(_Row):
    charge_type: ChargeType
    uom: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    unit_price: Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
    currency: Annotated[str, StringConstraints(min_length=3, max_length=3, to_upper=True)] = "ZAR"
    min_charge: Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)] | None = None
    site_code: str | None = None
    tax_code: Literal["STD", "ZERO", "EXEMPT"] = "STD"


def rate_line_writer(card: RateCard):
    services.require_draft(card)

    def create_line(row: RateLineRow) -> RateLine:
        if not Currency.objects.filter(code=row.currency).exists():
            raise Problem(422, "Unknown currency", row.currency)
        site = None
        if row.site_code:
            site = Site.objects.filter(code=row.site_code.upper()).first()
            if site is None:
                raise Problem(422, "Unknown site", row.site_code)
        return RateLine.objects.create(
            rate_card=card,
            charge_type=row.charge_type,
            uom=row.uom.upper(),
            unit_price=row.unit_price,
            currency=row.currency,
            min_charge=row.min_charge,
            site=site,
            tax_code=row.tax_code,
        )

    return create_line


class VehicleRow(_Row):
    registration: Annotated[str, StringConstraints(min_length=1, max_length=20, to_upper=True)]
    fleet_number: str = ""
    telematics_id: str | None = None
    carrier_code: str | None = None
    capacity_kg: Annotated[int, Field(ge=0)] | None = None
    pallet_slots: Annotated[int, Field(ge=0)] | None = None
    licence_disc_expiry: date | None = None
    roadworthy_expiry: date | None = None
    home_site_code: str | None = None


def create_vehicle(row: VehicleRow) -> Vehicle:
    carrier = services.party_by_code(row.carrier_code, PartyRoleType.CARRIER) if row.carrier_code else None
    home = Site.objects.filter(code=row.home_site_code.upper()).first() if row.home_site_code else None
    if row.home_site_code and home is None:
        raise Problem(422, "Unknown site", row.home_site_code)
    return Vehicle.objects.create(
        registration=row.registration.replace(" ", ""),
        fleet_number=row.fleet_number or "",
        telematics_id=row.telematics_id,
        carrier=carrier,
        capacity_kg=row.capacity_kg,
        pallet_slots=row.pallet_slots,
        licence_disc_expiry=row.licence_disc_expiry,
        roadworthy_expiry=row.roadworthy_expiry,
        home_site=home,
    )


class PersonRow(_Row):
    first_name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    last_name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    employee_code: str | None = None
    carrier_code: str | None = None
    is_driver: bool = True
    id_number: Annotated[str, StringConstraints(pattern=r"^\d{13}$")] | None = None
    phone: Annotated[str, StringConstraints(max_length=20)] | None = None
    licence_expiry: date | None = None
    prdp_expiry: date | None = None


def create_person(row: PersonRow) -> Person:
    carrier = services.party_by_code(row.carrier_code, PartyRoleType.CARRIER) if row.carrier_code else None
    return Person.objects.create(
        first_name=row.first_name,
        last_name=row.last_name,
        employee_code=row.employee_code,
        carrier=carrier,
        is_driver=row.is_driver,
        id_number=row.id_number or "",
        phone=row.phone or "",
        licence_expiry=row.licence_expiry,
        prdp_expiry=row.prdp_expiry,
    )
