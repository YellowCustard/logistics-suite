"""Master data expiries for the admin home (see apps.platform.admin_dashboard)."""

from datetime import timedelta

from django.utils import timezone

from apps.platform.admin_dashboard import Metric, plural, register_chips

from .models import Person, RateCard, RateCardStatus, Vehicle

EXPIRY_WINDOW = timedelta(days=30)


def roadworthy_chip() -> list[Metric]:
    limit = timezone.localdate() + EXPIRY_WINDOW
    n = Vehicle.objects.filter(is_active=True, roadworthy_expiry__lte=limit).count()
    return [Metric(f"{n} roadworthy expiring", n, tone="warning")]


def prdp_chip() -> list[Metric]:
    limit = timezone.localdate() + EXPIRY_WINDOW
    n = Person.objects.filter(is_active=True, is_driver=True, prdp_expiry__lte=limit).count()
    return [Metric(f"{n} PrDP expiring", n, tone="warning")]


def draft_rate_card_chip() -> list[Metric]:
    n = RateCard.objects.filter(status=RateCardStatus.DRAFT).count()
    return [Metric(plural(n, "draft rate card", "draft rate cards"), n)]


def register() -> None:
    register_chips("masterdata", "masterdata.view_vehicle", roadworthy_chip)
    register_chips("masterdata", "masterdata.view_person", prdp_chip)
    register_chips("masterdata", "masterdata.view_ratecard", draft_rate_card_chip)
