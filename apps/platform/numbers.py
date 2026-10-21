from django.db import transaction
from django.utils import timezone

from .models import NumberSequence


def next_number(name: str, prefix: str) -> str:
    """Gap-free per year within committed transactions, e.g. ORD-2027-000123.

    The row lock serialises callers for the same sequence; call it late in the transaction to keep the
    lock short. A rolled-back transaction leaves no gap because the increment rolls back too.
    """
    year = timezone.localdate().year
    with transaction.atomic():
        seq, _ = NumberSequence.objects.select_for_update().get_or_create(
            name=name, defaults={"prefix": prefix, "year": year, "next_value": 1}
        )
        if seq.year != year:
            seq.year, seq.next_value = year, 1
        value = seq.next_value
        seq.next_value += 1
        seq.save()
    return f"{seq.prefix}-{year}-{value:06d}"
