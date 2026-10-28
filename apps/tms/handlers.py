"""Outbox consumers owned by the transport module."""

import uuid

from apps.platform.outbox import Event, subscribe

from . import shipments


@subscribe("oms.sales_order.accepted", consumer="tms.create_shipment")
def create_shipment(event: Event) -> None:
    shipments.create_from_order(client_id=event.client_id, site_id=event.site_id, payload=event.payload)


@subscribe("wms.handling_unit.closed", consumer="tms.attach_unit")
def attach_unit(event: Event) -> None:
    shipments.attach_unit(event.payload)


@subscribe("oms.sales_order.cancelled", consumer="tms.cancel_shipment")
def cancel_shipment(event: Event) -> None:
    shipments.cancel_for_order(uuid.UUID(event.payload["sales_order_id"]))
