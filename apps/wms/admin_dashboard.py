"""Warehouse counts for the admin home (see apps.platform.admin_dashboard)."""

from apps.platform.admin_dashboard import Metric, plural, register_chips

from .models import CountStatus, CycleCount, PickTask, PickTaskStatus, PutawayTask, Receipt, ReceiptStatus, TaskStatus


def putaway_chip() -> list[Metric]:
    n = PutawayTask.objects.filter(status__in=[TaskStatus.OPEN, TaskStatus.CLAIMED]).count()
    return [Metric(f"{n} putaway open", n)]


def short_pick_chip() -> list[Metric]:
    n = PickTask.objects.filter(status=PickTaskStatus.SHORT).count()
    return [Metric(plural(n, "short pick", "short picks"), n, tone="warning")]


def receipt_chip() -> list[Metric]:
    n = Receipt.objects.filter(status=ReceiptStatus.OPEN).count()
    return [Metric(plural(n, "receipt open", "receipts open"), n)]


def count_approval_chip() -> list[Metric]:
    n = CycleCount.objects.filter(status=CountStatus.AWAITING_APPROVAL).count()
    return [Metric(plural(n, "count to approve", "counts to approve"), n, tone="warning")]


def register() -> None:
    register_chips("wms", "wms.view_putawaytask", putaway_chip)
    register_chips("wms", "wms.view_picktask", short_pick_chip)
    register_chips("wms", "wms.view_receipt", receipt_chip)
    register_chips("wms", "wms.view_cyclecount", count_approval_chip)
