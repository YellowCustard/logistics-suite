import datetime
from decimal import Decimal

from django.db import migrations

GUARDS = """
CREATE OR REPLACE FUNCTION masterdata_rate_card_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'rate card % is % and cannot be deleted', OLD.id, OLD.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'draft' THEN
        RETURN NEW;
    END IF;
    -- Published or retired: only shortening valid_to and published -> retired are allowed.
    IF NEW.contract_id IS DISTINCT FROM OLD.contract_id
       OR NEW.number IS DISTINCT FROM OLD.number
       OR NEW.valid_from IS DISTINCT FROM OLD.valid_from
       OR NEW.published_at IS DISTINCT FROM OLD.published_at
       OR NEW.published_by IS DISTINCT FROM OLD.published_by
       OR NEW.notes IS DISTINCT FROM OLD.notes
       OR (NEW.status <> OLD.status AND NOT (OLD.status = 'published' AND NEW.status = 'retired'))
       OR (OLD.valid_to IS NOT NULL AND (NEW.valid_to IS NULL OR NEW.valid_to > OLD.valid_to)) THEN
        RAISE EXCEPTION 'rate card % is % and immutable', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER rate_card_guard
    BEFORE UPDATE OR DELETE ON masterdata_ratecard
    FOR EACH ROW EXECUTE FUNCTION masterdata_rate_card_guard();

CREATE OR REPLACE FUNCTION masterdata_rate_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    card_status text;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT status INTO card_status FROM masterdata_ratecard WHERE id = OLD.rate_card_id;
        IF card_status IS DISTINCT FROM 'draft' AND card_status IS NOT NULL THEN
            RAISE EXCEPTION 'rate lines of a % rate card are immutable', card_status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT status INTO card_status FROM masterdata_ratecard WHERE id = NEW.rate_card_id;
        IF card_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION 'rate lines can only be added to a draft rate card'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END $$;

CREATE TRIGGER rate_line_guard
    BEFORE INSERT OR UPDATE OR DELETE ON masterdata_rateline
    FOR EACH ROW EXECUTE FUNCTION masterdata_rate_line_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS rate_line_guard ON masterdata_rateline;
DROP FUNCTION IF EXISTS masterdata_rate_line_guard();
DROP TRIGGER IF EXISTS rate_card_guard ON masterdata_ratecard;
DROP FUNCTION IF EXISTS masterdata_rate_card_guard();
"""

CURRENCIES = [
    ("ZAR", "South African rand"),
    ("USD", "US dollar"),
    ("EUR", "Euro"),
    ("GBP", "Pound sterling"),
    ("BWP", "Botswana pula"),
    ("NAD", "Namibian dollar"),
    ("LSL", "Lesotho loti"),
    ("SZL", "Eswatini lilangeni"),
    ("MZN", "Mozambican metical"),
    ("ZMW", "Zambian kwacha"),
    ("ZWG", "Zimbabwe gold"),
    ("MWK", "Malawian kwacha"),
]


def seed(apps, schema_editor):
    from apps.platform.ids import uuid7

    Currency = apps.get_model("masterdata", "Currency")
    TaxCode = apps.get_model("masterdata", "TaxCode")
    for code, name in CURRENCIES:
        Currency.objects.get_or_create(code=code, defaults={"name": name})
    # SA VAT standard rate 15% since 2018-04-01. Confirm with the tax practitioner (spec section 18).
    for code, description, rate in [
        ("STD", "VAT standard rate", Decimal("0.1500")),
        ("ZERO", "VAT zero-rated", Decimal("0")),
        ("EXEMPT", "VAT exempt", Decimal("0")),
    ]:
        TaxCode.objects.get_or_create(
            code=code,
            valid_from=datetime.date(2018, 4, 1),
            defaults={"id": uuid7(), "description": description, "rate": rate},
        )


class Migration(migrations.Migration):
    dependencies = [("masterdata", "0001_initial")]
    operations = [
        migrations.RunSQL(GUARDS, REVERSE),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
