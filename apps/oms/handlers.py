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
