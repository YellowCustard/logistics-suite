"""Public interface of master data. Other modules call these functions or read the models; they never
write master data tables directly."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.platform import audit, outbox
from apps.platform.apiutils import Problem
from apps.platform.context import current_actor

from .models import (
    Contract,
    Item,
    ItemUom,
    Party,
    PartyRole,
    PartyRoleType,
    RateCard,
    RateCardStatus,
    RateLine,
)

# --- Parties -------------------------------------------------------------------------------------------


def is_client(party_id: uuid.UUID) -> bool:
    return PartyRole.objects.filter(party_id=party_id, role=PartyRoleType.CLIENT, party__is_active=True).exists()


def require_client(party_id: uuid.UUID) -> Party:
    party = Party.objects.filter(pk=party_id, is_active=True, roles__role=PartyRoleType.CLIENT).first()
    if party is None:
        raise Problem(404, "Not found", "Unknown client.")
    return party


def party_by_code(code: str, role: str | None = None) -> Party:
    qs = Party.objects.filter(code=code, is_active=True)
    if role:
        qs = qs.filter(roles__role=role)
    party = qs.first()
    if party is None:
        raise Problem(422, "Unknown party", f"No active {role or 'party'} with code {code!r}.")
    return party


@transaction.atomic
def create_party(
    *, code: str, legal_name: str, roles: list[str], vat_number: str = "", vat_exempt: bool = False, **fields
) -> Party:
    code = code.strip().upper()
    if PartyRoleType.CLIENT in roles and not (vat_number or vat_exempt):
        raise Problem(422, "VAT number required", "A client needs a VAT number or the VAT-exempt flag.")
    if vat_number and not (vat_number.isdigit() and len(vat_number) == 10 and vat_number.startswith("4")):
        # SA VAT numbers are 10 digits starting with 4.
        raise Problem(422, "Invalid VAT number", "Expected 10 digits starting with 4.")
    party = Party.objects.create(
        code=code, legal_name=legal_name, vat_number=vat_number, vat_exempt=vat_exempt, **fields
    )
    for role in sorted(set(roles)):
        PartyRole.objects.create(party=party, role=role)
    if PartyRoleType.CLIENT in roles:
        outbox.emit(
            "masterdata.client.created",
            aggregate_type="masterdata.party",
            aggregate_id=party.pk,
            client_id=party.pk,
            payload={"party_id": str(party.pk), "code": party.code, "legal_name": party.legal_name},
        )
    return party


def update_with_version(instance, expected_version: int, changes: dict) -> None:
    """Optimistic locking: apply `changes` only if the row is still at `expected_version`."""
    with transaction.atomic():
        current = type(instance).objects.select_for_update().get(pk=instance.pk)
        if current.version != expected_version:
            raise Problem(
                412, "Precondition failed", f"Record changed (now version {current.version}); reload and retry."
            )
        for name, value in changes.items():
            setattr(current, name, value)
        current.version += 1
        current.full_clean(validate_unique=False, validate_constraints=False)
        current.save()
        instance.refresh_from_db()


# --- Rate cards ---------------------------------------------------------------------------------------------


@transaction.atomic
def create_rate_card(
    *, contract: Contract, valid_from: date, valid_to: date | None = None, notes: str = ""
) -> RateCard:
    Contract.objects.select_for_update().get(pk=contract.pk)  # serialise version numbering
    number = (RateCard.objects.filter(contract=contract).aggregate(n=Max("number"))["n"] or 0) + 1
    return RateCard.objects.create(
        contract=contract, number=number, valid_from=valid_from, valid_to=valid_to, notes=notes
    )


def require_draft(card: RateCard) -> None:
    if card.status != RateCardStatus.DRAFT:
        raise Problem(409, "Rate card is not a draft", "Published cards are immutable; create a new version.")


@transaction.atomic
def publish_rate_card(card: RateCard) -> RateCard:
    """Publish a draft. An open-ended earlier card is closed the day before; any other overlap is refused."""
    Contract.objects.select_for_update().get(pk=card.contract_id)
    card = RateCard.objects.select_for_update().get(pk=card.pk)
    require_draft(card)
    if not card.lines.exists():
        raise Problem(422, "Empty rate card", "Add at least one rate line before publishing.")
    contract = card.contract
    if card.valid_from < contract.valid_from or (
        contract.valid_to and (card.valid_to is None or card.valid_to > contract.valid_to)
    ):
        raise Problem(422, "Outside contract window", "The rate card must sit inside the contract dates.")

    published = RateCard.objects.filter(contract=card.contract_id, status=RateCardStatus.PUBLISHED)
    new_end = card.valid_to or date.max
    for other in published.select_for_update():
        other_end = other.valid_to or date.max
        overlaps = other.valid_from <= new_end and card.valid_from <= other_end
        if not overlaps:
            continue
        if other.valid_to is None and other.valid_from < card.valid_from:
            before = audit.snapshot(other)
            other.valid_to = card.valid_from - timedelta(days=1)
            other.save(update_fields=["valid_to", "updated_at"])
            audit.record(
                action="rate_card.superseded",
                entity_type="masterdata.ratecard",
                entity_id=other.pk,
                before=before,
                after=audit.snapshot(other),
                client_id=card.contract.client_id,
            )
            continue
        raise Problem(
            409,
            "Overlapping rate card",
            f"Version {other.number} already covers {other.valid_from}–{other.valid_to or 'open'}.",
        )

    before = audit.snapshot(card)
    card.status = RateCardStatus.PUBLISHED
    card.published_at = timezone.now()
    card.published_by = current_actor().id
    card.save()
    audit.record(
        action="rate_card.publish",
        entity_type="masterdata.ratecard",
        entity_id=card.pk,
        before=before,
        after=audit.snapshot(card),
        client_id=contract.client_id,
    )
    outbox.emit(
        "masterdata.rate_card.published",
        aggregate_type="masterdata.ratecard",
        aggregate_id=card.pk,
        client_id=contract.client_id,
        payload={
            "rate_card_id": str(card.pk),
            "contract_id": str(contract.pk),
            "number": card.number,
            "valid_from": card.valid_from.isoformat(),
            "valid_to": card.valid_to.isoformat() if card.valid_to else None,
        },
    )
    return card


def price_for(*, client_id: uuid.UUID, charge_type: str, on: date, site_id: uuid.UUID | None = None) -> RateLine | None:
    """The applicable rate line: a site-specific price beats the all-sites price."""
    lines = RateLine.objects.filter(
        rate_card__status=RateCardStatus.PUBLISHED,
        rate_card__contract__client_id=client_id,
        rate_card__valid_from__lte=on,
        charge_type=charge_type,
    ).filter(Q(rate_card__valid_to__isnull=True) | Q(rate_card__valid_to__gte=on))
    lines = lines.filter(Q(site_id=site_id) | Q(site__isnull=True)) if site_id else lines.filter(site__isnull=True)
    return lines.order_by("site_id").first()  # NULLS LAST in Postgres ascending order


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


# --- Units of measure ------------------------------------------------------------------------------------


def uom_factor(item: Item, uom: str) -> int:
    """Base units per `uom` for this item. The base UoM always has factor 1."""
    uom = uom.strip().upper()
    if uom == item.base_uom:
        return 1
    factor = ItemUom.objects.filter(item=item, uom=uom).values_list("factor", flat=True).first()
    if factor is None:
        raise Problem(422, "Unknown unit of measure", f"{item.sku} has no unit {uom!r}.")
    return factor


def to_base_qty(item: Item, qty: Decimal, uom: str) -> Decimal:
    return qty * uom_factor(item, uom)


def client_item(client_id: uuid.UUID, *, sku: str | None = None, barcode: str | None = None) -> Item:
    """An active item of this client, found by SKU or by any of its barcodes."""
    qs = Item.objects.filter(client_id=client_id, is_active=True)
    if sku:
        item = qs.filter(sku=sku.strip()).first()
    elif barcode:
        item = qs.filter(barcodes__barcode=barcode.strip()).first()
    else:
        raise Problem(422, "Item required", "Give a SKU or a barcode.")
    if item is None:
        raise Problem(422, "Unknown item", f"No active item {sku or barcode!r} for this client.")
    return item
