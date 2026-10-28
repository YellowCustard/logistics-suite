"""GS1 identifiers shared by master data (item barcodes) and the warehouse (SSCC labels)."""

import re

_GTIN_RE = re.compile(r"^(\d{8}|\d{12}|\d{13}|\d{14})$")


def check_digit(digits: str) -> str:
    """GS1 mod-10: weights 3,1,3,1… from the rightmost data digit."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return str((10 - total % 10) % 10)


def looks_like_gtin(code: str) -> bool:
    """All digits and the length of a GTIN-8, UPC-A (GTIN-12), EAN-13 or GTIN-14."""
    return bool(_GTIN_RE.match(code))


def gtin_valid(code: str) -> bool:
    return looks_like_gtin(code) and check_digit(code[:-1]) == code[-1]
