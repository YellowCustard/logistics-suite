"""Transport counts for the admin home (see apps.platform.admin_dashboard)."""

from django.db.models import Count, Q

from apps.platform.admin_dashboard import Metric, changelist, plural, register_chips, register_kpi

from .models import Shipment, ShipmentStatus, Trip, TripStatus


def in_transit() -> list[Metric]:
    shipments = Shipment.objects.filter(status=ShipmentStatus.IN_TRANSIT).count()
    trips = Trip.objects.filter(status=TripStatus.DISPATCHED).count()
    return [
        Metric(
            "Shipments in transit",
            shipments,
            changelist("tms.shipment", status__exact=ShipmentStatus.IN_TRANSIT),
            detail=f"{plural(trips, 'trip', 'trips')} on the road",
            icon="truck",
        )
    ]


def shipment_chips() -> list[Metric]:
    c = Shipment.objects.aggregate(
        failed=Count("pk", filter=Q(status=ShipmentStatus.FAILED)),
        ready=Count("pk", filter=Q(status=ShipmentStatus.READY)),
    )
    return [
        Metric(plural(c["failed"], "failed delivery", "failed deliveries"), c["failed"], tone="critical"),
        Metric(f"{c['ready']} ready to plan", c["ready"]),
    ]


def register() -> None:
    register_kpi(30, "tms.view_shipment", in_transit)
    register_chips("tms", "tms.view_shipment", shipment_chips)
