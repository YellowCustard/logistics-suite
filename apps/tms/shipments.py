"""Shipments, kept in step with orders and packing through events.

One accepted sales order becomes one draft shipment. Each closed handling unit is attached to it, and when the last
unit closes the shipment is packed: a draft becomes ready to plan, and one already on a trip stays planned. A
cancelled order takes its shipment off any trip still being planned.

Every handler is idempotent on its natural key (sales order, handling unit) as well as through the outbox inbox.
"""

import uuid
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.platform.numbers import next_number

from .models import (
    PLANNABLE,
    Shipment,
    ShipmentLine,
    ShipmentStatus,
    ShipmentUnit,
    Stop,
    StopAction,
    StopShipment,
    TripStatus,
)


class ShipmentNotReady(Exception):
    """The event arrived before the one that creates the shipment; raising makes the outbox retry it later."""


@transaction.atomic
def create_from_order(*, client_id: uuid.UUID, site_id: uuid.UUID, payload: dict) -> Shipment:
    sales_order_id = uuid.UUID(payload["sales_order_id"])
    existing = Shipment.objects.filter(sales_order_id=sales_order_id).first()
    if existing is not None:
        return existing
    required_by = payload.get("required_by")
    shipment = Shipment.objects.create(
        client_id=client_id,
        site_id=site_id,
        ship_to_id=uuid.UUID(payload["ship_to_site_id"]),
        sales_order_id=sales_order_id,
        order_number=payload["number"],
        client_ref=payload.get("client_ref") or "",
        required_by=date.fromisoformat(required_by) if required_by else None,
        number=next_number("shipment", "SHP"),
    )
    for line in payload["lines"]:  # one by one so each row is audited
        ShipmentLine.objects.create(
            shipment=shipment,
            client_id=client_id,
            order_line_id=uuid.UUID(line["line_id"]),
            item_id=uuid.UUID(line["item_id"]),
            qty_ordered=Decimal(line["base_qty"]),
        )
    return shipment


@transaction.atomic
def attach_unit(payload: dict) -> ShipmentUnit:
    handling_unit_id = uuid.UUID(payload["handling_unit_id"])
    shipment = Shipment.objects.select_for_update().filter(sales_order_id=payload["sales_order_id"]).first()
    if shipment is None:
        raise ShipmentNotReady(f"No shipment yet for sales order {payload['sales_order_id']}")
    unit = ShipmentUnit.objects.filter(handling_unit_id=handling_unit_id).first()
    if unit is None:
        weight = Decimal(payload["weight_kg"])
        volume = Decimal(payload["volume_m3"]) if payload.get("volume_m3") is not None else None
        unit = ShipmentUnit.objects.create(
            shipment=shipment,
            client_id=shipment.client_id,
            handling_unit_id=handling_unit_id,
            sscc=payload["sscc"],
            kind=payload["kind"],
            weight_kg=weight,
            volume_m3=volume,
            cartons=int(payload.get("cartons") or 1),
        )
        lines = {line.order_line_id: line for line in shipment.lines.select_for_update()}
        for content in payload["contents"]:
            line = lines.get(uuid.UUID(content["order_line_id"]))
            if line is None:
                raise ValueError(f"{shipment.number}: unknown order line {content['order_line_id']}")
            line.qty_packed += Decimal(content["qty"])
            line.save()
        shipment.weight_kg += weight
        shipment.volume_m3 += volume or 0
    if payload.get("order_fully_packed"):
        _mark_packed(shipment)
    shipment.save()
    return unit


def _mark_packed(shipment: Shipment) -> None:
    if shipment.packed_at is None:
        shipment.packed_at = timezone.now()
    if shipment.status == ShipmentStatus.DRAFT:
        shipment.status = ShipmentStatus.READY


@transaction.atomic
def cancel_for_order(sales_order_id: uuid.UUID) -> Shipment | None:
    shipment = Shipment.objects.select_for_update().filter(sales_order_id=sales_order_id).first()
    if shipment is None or shipment.status == ShipmentStatus.CANCELLED:
        return shipment
    if shipment.status not in (*PLANNABLE, ShipmentStatus.PLANNED):
        # Orders can only be cancelled before picking, so this means the events disagree; keep the shipment.
        raise ValueError(f"{shipment.number} is {shipment.status}; its order can't have been cancelled")
    for trip_id in set(
        StopShipment.objects.filter(shipment=shipment, trip__status=TripStatus.PLANNING).values_list(
            "trip_id", flat=True
        )
    ):
        remove_from_trip(trip_id, [shipment.pk])
    shipment.status = ShipmentStatus.CANCELLED
    shipment.cancelled_at = timezone.now()
    shipment.save()
    return shipment


def remove_from_trip(trip_id: uuid.UUID, shipment_ids: list[uuid.UUID]) -> None:
    """Drop shipments from a planning trip, delete stops left empty, and close the gaps in the sequence."""
    StopShipment.objects.filter(trip_id=trip_id, shipment_id__in=shipment_ids).delete()
    stops = list(Stop.objects.filter(trip_id=trip_id).order_by("seq"))
    has_drops = StopShipment.objects.filter(trip_id=trip_id, action=StopAction.DROP).exists()
    keep = []
    for stop in stops:
        if not stop.shipments.exists() or not has_drops:
            stop.delete()
        else:
            keep.append(stop)
    for seq, stop in enumerate(keep, start=1):  # ascending, so each new seq is already free
        if stop.seq != seq:
            stop.seq = seq
            stop.save(update_fields=["seq", "updated_at"])
