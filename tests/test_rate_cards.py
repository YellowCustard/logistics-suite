import datetime as dt
from decimal import Decimal

import pytest
from django.db import DatabaseError, transaction

from apps.masterdata import services
from apps.masterdata.models import ChargeType, Contract, RateCard, RateCardStatus, RateLine, Site
from apps.platform.apiutils import Problem
from apps.platform.models import AuditEntry, OutboxEvent

pytestmark = pytest.mark.django_db

D = dt.date


@pytest.fixture
def contract(make_client):
    return Contract.objects.create(client=make_client(), reference="SDB-2026", valid_from=D(2026, 1, 1))


def _card(contract, valid_from, valid_to=None, price="12.50", site=None):
    card = services.create_rate_card(contract=contract, valid_from=valid_from, valid_to=valid_to)
    RateLine.objects.create(
        rate_card=card,
        charge_type=ChargeType.STORAGE_PALLET_DAY,
        uom="PALLET_DAY",
        unit_price=Decimal(price),
        site=site,
    )
    return card


def test_versions_are_numbered_per_contract(contract):
    assert [services.create_rate_card(contract=contract, valid_from=D(2026, 1, 1)).number for _ in range(3)] == [
        1,
        2,
        3,
    ]


def test_publish_records_audit_and_emits_event(contract):
    card = services.publish_rate_card(_card(contract, D(2026, 1, 1)))
    assert card.status == RateCardStatus.PUBLISHED and card.published_at is not None
    assert AuditEntry.objects.filter(action="rate_card.publish", entity_id=str(card.pk)).exists()
    event = OutboxEvent.objects.get(type="masterdata.rate_card.published")
    assert event.payload["rate_card_id"] == str(card.pk) and event.client_id == contract.client_id


def test_empty_or_out_of_window_cards_cannot_be_published(contract):
    with pytest.raises(Problem, match="Empty rate card"):
        services.publish_rate_card(services.create_rate_card(contract=contract, valid_from=D(2026, 1, 1)))
    with pytest.raises(Problem, match="Outside contract window"):
        services.publish_rate_card(_card(contract, D(2025, 6, 1)))


def test_published_card_and_lines_are_immutable_in_the_database(contract):
    card = services.publish_rate_card(_card(contract, D(2026, 1, 1)))
    line = card.lines.get()
    attempts = [
        lambda: RateLine.objects.filter(pk=line.pk).update(unit_price=Decimal("1")),
        lambda: RateLine.objects.filter(pk=line.pk).delete(),
        lambda: RateLine.objects.create(
            rate_card=card, charge_type=ChargeType.PICK_UNIT, uom="UNIT", unit_price=Decimal("1")
        ),
        lambda: RateCard.objects.filter(pk=card.pk).update(valid_from=D(2026, 2, 1)),
        lambda: RateCard.objects.filter(pk=card.pk).update(status=RateCardStatus.DRAFT),
        lambda: RateCard.objects.filter(pk=card.pk).delete(),
    ]
    for attempt in attempts:
        with pytest.raises(DatabaseError), transaction.atomic():
            attempt()
    with pytest.raises(Problem, match="not a draft"):
        services.require_draft(card)


def test_published_card_may_only_be_shortened_or_retired(contract):
    card = services.publish_rate_card(_card(contract, D(2026, 1, 1), D(2026, 12, 31)))
    RateCard.objects.filter(pk=card.pk).update(valid_to=D(2026, 6, 30))
    with pytest.raises(DatabaseError), transaction.atomic():
        RateCard.objects.filter(pk=card.pk).update(valid_to=D(2026, 9, 30))
    RateCard.objects.filter(pk=card.pk).update(status=RateCardStatus.RETIRED)
    with pytest.raises(DatabaseError), transaction.atomic():
        RateCard.objects.filter(pk=card.pk).update(status=RateCardStatus.PUBLISHED)


def test_new_version_supersedes_open_ended_card_and_overlaps_are_refused(contract):
    v1 = services.publish_rate_card(_card(contract, D(2026, 1, 1), price="10"))
    v2 = services.publish_rate_card(_card(contract, D(2026, 7, 1), price="11"))
    v1.refresh_from_db()
    assert v1.valid_to == D(2026, 6, 30)
    assert AuditEntry.objects.filter(action="rate_card.superseded", entity_id=str(v1.pk)).exists()

    with pytest.raises(Problem, match="Overlapping"):
        services.publish_rate_card(_card(contract, D(2026, 3, 1), D(2026, 4, 30)))

    def price(on):
        line = services.price_for(client_id=contract.client_id, charge_type=ChargeType.STORAGE_PALLET_DAY, on=on)
        return line.unit_price if line else None

    assert price(D(2025, 12, 31)) is None
    assert price(D(2026, 6, 30)) == Decimal("10")
    assert price(D(2026, 7, 1)) == Decimal("11")
    assert v2.valid_to is None


def test_site_specific_price_beats_all_sites_price(contract):
    teak = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
    card = _card(contract, D(2026, 1, 1), price="10")
    RateLine.objects.create(
        rate_card=card, charge_type=ChargeType.STORAGE_PALLET_DAY, uom="PALLET_DAY", unit_price=Decimal("9"), site=teak
    )
    services.publish_rate_card(card)
    lookup = dict(client_id=contract.client_id, charge_type=ChargeType.STORAGE_PALLET_DAY, on=D(2026, 2, 1))
    assert services.price_for(**lookup, site_id=teak.pk).unit_price == Decimal("9")
    assert services.price_for(**lookup).unit_price == Decimal("10")
    other = Site.objects.create(code="DBN", name="Durban", kind="warehouse")
    assert services.price_for(**lookup, site_id=other.pk).unit_price == Decimal("10")
