"""Outbox consumers owned by the warehouse module."""

from decimal import Decimal

from apps.platform import exceptions_queue
from apps.platform.models import Severity
from apps.platform.outbox import Event, subscribe

from .adjustments import needs_approval


@subscribe("wms.inventory.adjusted", consumer="wms.adjustment_review")
def review_large_adjustment(event: Event) -> None:
    """Direct corrections skip approval, so large ones go to the exception queue for review."""
    payload = event.payload
    if payload.get("approved_by"):
        return
    if not needs_approval(Decimal(payload["qty_before"]), Decimal(payload["qty_delta"])):
        return
    exceptions_queue.raise_case(
        kind="inventory.adjustment_review",
        severity=Severity.MEDIUM,
        summary=f"Stock corrected by {payload['qty_delta']} (was {payload['qty_before']}): {payload['reason']}",
        detail=payload,
        client_id=event.client_id,
        site_id=event.site_id,
        entity_type="wms.inventorymovement",
        entity_id=payload["movement_id"],
        dedupe_key=f"inventory.adjustment_review:{payload['movement_id']}",
    )
