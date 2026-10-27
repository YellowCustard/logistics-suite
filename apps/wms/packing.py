"""Packing: put picked stock into handling units (pallets or cartons), each with its own SSCC, then close them.

What can be packed for an order is what was picked for it, line by line and lot by lot, less what is already in
its handling units. Closing a unit fixes its contents and weight (a database trigger keeps it that way), emits
`wms.handling_unit.closed`, and tells OMS when the order is completely packed.

Packing for one order is serialised on the order's wave row, so two packers can't put the same picked units into
two handling units.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.masterdata.models import Item, Party, Site
from apps.oms import services as oms
from apps.platform import outbox
from apps.platform.apiutils import Problem

from . import labels, sscc
from .models import (
    OPEN_PICK,
    QTY_STEP,
    Allocation,
    AllocationStatus,
    HandlingUnit,
    HandlingUnitContent,
    HandlingUnitKind,
    HandlingUnitStatus,
    PickTask,
    WaveOrder,
)


@dataclass(slots=True)
class Packable:
    order_line_id: uuid.UUID
    item_id: uuid.UUID
    lot: str
    expiry: date | None
    picked: Decimal
    packed: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.picked - self.packed


def packable(order_id: uuid.UUID, *, closed_only: bool = False) -> list[Packable]:
    """Picked versus packed for each (order line, lot) of an order."""
    picked = (
        Allocation.objects.filter(
            sales_order_id=order_id,
            status__in=[AllocationStatus.PICKED, AllocationStatus.SHORT],
            qty_picked__gt=0,
        )
        .values("order_line_id", "balance__item_id", "balance__lot", "balance__expiry")
        .annotate(total=Sum("qty_picked"))
        .order_by("order_line_id", "balance__expiry", "balance__lot")
    )
    contents = HandlingUnitContent.objects.filter(handling_unit__sales_order_id=order_id)
    if closed_only:
        contents = contents.filter(handling_unit__status=HandlingUnitStatus.CLOSED)
    packed: dict[tuple, Decimal] = defaultdict(Decimal)
    for row in contents.values("order_line_id", "lot").annotate(total=Sum("qty")):
        packed[(row["order_line_id"], row["lot"])] += row["total"]
    rows: dict[tuple, Packable] = {}
    for row in picked:
        key = (row["order_line_id"], row["balance__lot"])
        if key in rows:  # same lot picked from two bins
            rows[key].picked += row["total"]
            continue
        rows[key] = Packable(
            row["order_line_id"],
            row["balance__item_id"],
            row["balance__lot"],
            row["balance__expiry"],
            row["total"],
            Decimal(0),
        )
    for key, row in rows.items():
        row.packed = packed.get(key, Decimal(0))
    return list(rows.values())


def _lock_order(order_id: uuid.UUID) -> WaveOrder:
    wave_order = WaveOrder.objects.select_for_update().filter(sales_order_id=order_id).first()
    if wave_order is None:
        raise Problem(409, "Not picked", "This order hasn't been released to a wave, so nothing is picked yet.")
    return wave_order


def _lock_unit(unit_id: uuid.UUID) -> HandlingUnit:
    order_id = HandlingUnit.objects.filter(pk=unit_id).values_list("sales_order_id", flat=True).first()
    if order_id is None:
        raise Problem(404, "Not found")
    _lock_order(order_id)
    unit = HandlingUnit.objects.select_for_update().get(pk=unit_id)
    if unit.status != HandlingUnitStatus.OPEN:
        raise Problem(409, "Handling unit closed", f"{unit.sscc} is closed; open a new one.")
    return unit


@transaction.atomic
def open_unit(*, order_id: uuid.UUID, kind: str, created_by: uuid.UUID | None) -> HandlingUnit:
    order = oms.order_summary(order_id)
    if order is None:
        raise Problem(404, "Not found")
    _lock_order(order_id)
    if order.status != oms.OrderStatus.PICKING:
        raise Problem(409, "Order not in picking", f"{order.number} is {order.status}.")
    return HandlingUnit.objects.create(
        client_id=order.client_id,
        site_id=order.site_id,
        sales_order_id=order.sales_order_id,
        order_number=order.number,
        sscc=sscc.next_sscc(),
        kind=kind,
        created_by=created_by,
    )


def _matches(item: Item, code: str) -> bool:
    return code.upper() == item.sku.upper() or item.barcodes.filter(barcode=code).exists()


@transaction.atomic
def add(unit_id: uuid.UUID, *, item_code: str, qty: Decimal, lot: str = "") -> HandlingUnit:
    """Put `qty` base units of the scanned item into the unit, taken from what is picked and not yet packed."""
    qty, code, lot = qty.quantize(QTY_STEP), item_code.strip(), lot.strip()
    if qty <= 0:
        raise Problem(422, "Invalid quantity", "Quantity must be more than zero.")
    unit = _lock_unit(unit_id)
    rows = [row for row in packable(unit.sales_order_id) if row.remaining > 0]
    items = {i.pk: i for i in Item.objects.filter(pk__in={r.item_id for r in rows})}
    rows = [r for r in rows if _matches(items[r.item_id], code)]
    if not rows:
        raise Problem(422, "Nothing to pack", f"{code!r} isn't picked for {unit.order_number}, or is all packed.")
    if lot:
        rows = [r for r in rows if r.lot == lot]
        if not rows:
            raise Problem(422, "Wrong lot", f"No picked stock of lot {lot!r} left to pack.")
    elif len({r.lot for r in rows}) > 1:
        raise Problem(422, "Lot required", "This item was picked from more than one lot; scan the lot.")
    available = sum(r.remaining for r in rows)
    if qty > available:
        raise Problem(422, "Too many", f"Only {available} left to pack for this item.")
    left = qty
    for row in rows:
        take = min(left, row.remaining)
        if take <= 0:
            break
        content, created = HandlingUnitContent.objects.get_or_create(
            handling_unit=unit,
            order_line_id=row.order_line_id,
            lot=row.lot,
            defaults={"item_id": row.item_id, "expiry": row.expiry, "qty": take},
        )
        if not created:
            content.qty += take
            content.save(update_fields=["qty", "updated_at"])
        left -= take
    unit.save(update_fields=["updated_at"])  # the unit's list position follows its last change
    return unit


@transaction.atomic
def remove(unit_id: uuid.UUID, content_id: uuid.UUID) -> HandlingUnit:
    unit = _lock_unit(unit_id)
    deleted, _ = HandlingUnitContent.objects.filter(pk=content_id, handling_unit=unit).delete()
    if not deleted:
        raise Problem(404, "Not found")
    return unit


@transaction.atomic
def close(
    unit_id: uuid.UUID,
    *,
    weight_kg: Decimal,
    cartons: int | None = None,
    volume_m3: Decimal | None = None,
    closed_by: uuid.UUID | None,
) -> HandlingUnit:
    unit = _lock_unit(unit_id)
    contents = list(unit.contents.all())
    if not contents:
        raise Problem(422, "Empty handling unit", "Pack something before closing it.")
    if weight_kg <= 0:
        raise Problem(422, "Weight required", "Weigh the unit; the weight goes on the label and the invoice.")
    if cartons is None:
        if unit.kind != HandlingUnitKind.CARTON:
            raise Problem(422, "Cartons required", "Say how many cartons are on the pallet.")
        cartons = 1
    if cartons <= 0:
        raise Problem(422, "Cartons required", "A handling unit holds at least one carton.")
    unit.weight_kg = weight_kg
    unit.cartons = cartons
    unit.volume_m3 = volume_m3
    unit.status = HandlingUnitStatus.CLOSED
    unit.closed_at = timezone.now()
    unit.closed_by = closed_by
    unit.save()
    fully_packed = _fully_packed(unit.sales_order_id)
    outbox.emit(
        "wms.handling_unit.closed",
        aggregate_type="wms.handling_unit",
        aggregate_id=unit.pk,
        client_id=unit.client_id,
        site_id=unit.site_id,
        payload={
            "handling_unit_id": str(unit.pk),
            "sscc": unit.sscc,
            "kind": unit.kind,
            "sales_order_id": str(unit.sales_order_id),
            "contents": [
                {"order_line_id": str(c.order_line_id), "item_id": str(c.item_id), "qty": str(c.qty), "lot": c.lot}
                for c in contents
            ],
            "weight_kg": str(unit.weight_kg),
            "volume_m3": str(unit.volume_m3) if unit.volume_m3 is not None else None,
            "cartons": unit.cartons,
            "order_fully_packed": fully_packed,
        },
    )
    return unit


def _fully_packed(order_id: uuid.UUID) -> bool:
    """Every pick for the order is finished and everything picked is in a closed unit."""
    if PickTask.objects.filter(sales_order_id=order_id, status__in=OPEN_PICK).exists():
        return False
    rows = packable(order_id, closed_only=True)
    return bool(rows) and all(row.remaining == 0 for row in rows)


def orders_to_pack(site_id: uuid.UUID, client_ids: frozenset[uuid.UUID] | None) -> list[WaveOrder]:
    """Orders at a site with picked stock not yet in a closed unit. `client_ids=None` means every client."""
    qs = WaveOrder.objects.filter(wave__site_id=site_id).filter(
        Q(sales_order_id__in=PickTask.objects.filter(status__in=OPEN_PICK).values("sales_order_id"))
        | Q(
            sales_order_id__in=Allocation.objects.filter(
                status__in=[AllocationStatus.PICKED, AllocationStatus.SHORT], qty_picked__gt=0
            ).values("sales_order_id")
        )
    )
    if client_ids is not None:
        qs = qs.filter(client_id__in=client_ids)
    result = []
    for wave_order in qs.order_by("number")[:200]:
        summary = oms.order_summary(wave_order.sales_order_id)
        if summary is not None and summary.status == oms.OrderStatus.PICKING:
            result.append(wave_order)
    return result


def label_for(unit: HandlingUnit) -> labels.LabelData:
    if unit.status != HandlingUnitStatus.CLOSED:
        raise Problem(409, "Not closed yet", "Close the unit (with its weight) before printing its label.")
    order = oms.order_summary(unit.sales_order_id)
    ship_to = Site.objects.filter(pk=order.ship_to_id).first() if order else None
    site = Site.objects.get(pk=unit.site_id)
    client = Party.objects.get(pk=unit.client_id)
    contents = list(unit.contents.all())
    units = sum(c.qty for c in contents).normalize()
    reference = f"{unit.order_number} / {order.client_ref}" if order and order.client_ref else unit.order_number
    return labels.LabelData(
        sscc=unit.sscc,
        client=client.code,
        order_number=reference,
        ship_from=f"{site.code} {site.name}",
        ship_to=ship_to.name if ship_to else "",
        contents=f"{len({c.order_line_id for c in contents})} line(s), {units:f} unit(s), {unit.cartons} carton(s)",
        weight_kg=f"{unit.weight_kg.normalize():f}",
    )
