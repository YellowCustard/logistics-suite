"""Order counts for the admin home (see apps.platform.admin_dashboard)."""

from django.db.models import Count, Q

from apps.platform.admin_dashboard import Metric, changelist, register_chips, register_kpi

from .models import OrderStatus, SalesOrder

ACTIVE = [OrderStatus.ACCEPTED, OrderStatus.ALLOCATED, OrderStatus.BACKORDERED, OrderStatus.PICKING, OrderStatus.PACKED]


def _counts() -> dict[str, int]:
    return SalesOrder.objects.filter(status__in=ACTIVE).aggregate(
        active=Count("pk"), backordered=Count("pk", filter=Q(status=OrderStatus.BACKORDERED))
    )


def active_orders() -> list[Metric]:
    c = _counts()
    return [
        Metric(
            "Active orders",
            c["active"],
            changelist("oms.salesorder", status__in=",".join(ACTIVE)),
            detail=f"{c['backordered']} backordered",
            tone="warning" if c["backordered"] else "neutral",
            icon="orders",
        )
    ]


def backordered_chip() -> list[Metric]:
    n = _counts()["backordered"]
    return [Metric(f"{n} backordered", n, tone="warning")]


def register() -> None:
    register_kpi(10, "oms.view_salesorder", active_orders)
    register_chips("oms", "oms.view_salesorder", backordered_chip)
