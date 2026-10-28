"""Outbox consumers owned by the order module (discovered by platform.outbox.discover_handlers)."""

import uuid

from apps.platform.outbox import Event, subscribe

from . import services


@subscribe("wms.receipt.completed", consumer="oms.close_inbound_notice")
def close_inbound_notice(event: Event) -> None:
    notice_id = event.payload.get("inbound_notice_id")
    if notice_id:
        services.record_receipt(uuid.UUID(notice_id), event.payload["lines"])


@subscribe("wms.allocation.completed", consumer="oms.record_allocation")
def record_allocation(event: Event) -> None:
    services.record_allocation(
        uuid.UUID(event.payload["sales_order_id"]), fully_allocated=bool(event.payload["fully_allocated"])
    )


@subscribe("wms.handling_unit.closed", consumer="oms.record_packed")
def record_packed(event: Event) -> None:
    if event.payload.get("order_fully_packed"):
        services.record_packed(uuid.UUID(event.payload["sales_order_id"]))


@subscribe("tms.trip.dispatched", consumer="oms.record_dispatched")
def record_dispatched(event: Event) -> None:
    for shipment in event.payload["shipments"]:
        services.record_dispatched(
            uuid.UUID(shipment["sales_order_id"]),
            trip_number=event.payload["number"],
            shipment_number=shipment["number"],
        )


@subscribe("tms.shipment.delivered", consumer="oms.record_delivered")
def record_delivered(event: Event) -> None:
    services.record_delivered(uuid.UUID(event.payload["sales_order_id"]), payload=event.payload)
