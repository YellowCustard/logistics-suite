"""Serial Shipping Container Codes (GS1 AI 00): extension digit + company prefix + serial reference, padded to
17 digits, then a mod-10 check digit. Serials come from one counter that never resets, so an SSCC is never
reissued (GS1 asks for at least a year)."""

import re

from django.conf import settings
from django.db import transaction

from apps.platform.apiutils import Problem
from apps.platform.models import NumberSequence

SSCC_RE = re.compile(r"^\d{18}$")


def check_digit(digits: str) -> str:
    """GS1 mod-10: weights 3,1,3,1… from the rightmost data digit."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return str((10 - total % 10) % 10)


def is_valid(sscc: str) -> bool:
    return bool(SSCC_RE.match(sscc)) and check_digit(sscc[:17]) == sscc[17]


def build(extension: str, company_prefix: str, serial: int) -> str:
    if not (len(extension) == 1 and extension.isdigit()):
        raise ValueError("extension digit must be one digit")
    if not (company_prefix.isdigit() and 7 <= len(company_prefix) <= 10):
        raise ValueError("GS1 company prefix must be 7 to 10 digits")
    width = 16 - len(company_prefix)
    if not 0 <= serial < 10**width:
        raise ValueError(f"serial {serial} does not fit {width} digits; a longer prefix is exhausted")
    body = f"{extension}{company_prefix}{serial:0{width}d}"
    return body + check_digit(body)


def next_sscc() -> str:
    prefix = settings.GS1_COMPANY_PREFIX.strip()
    if not prefix:
        raise Problem(503, "SSCC not configured", "Set GS1_COMPANY_PREFIX before packing.")
    with transaction.atomic():
        seq, _ = NumberSequence.objects.select_for_update().get_or_create(
            name="sscc", defaults={"prefix": "SSCC", "year": 0, "next_value": 1}
        )
        serial = seq.next_value
        seq.next_value += 1
        seq.save()
    try:
        return build(settings.SSCC_EXTENSION_DIGIT.strip(), prefix, serial)
    except ValueError as error:
        raise Problem(503, "SSCC not configured", str(error)) from error


def human(sscc: str) -> str:
    """The text line under the barcode: (00) and the 18 digits."""
    return f"(00) {sscc}"
