"""References for proof-of-delivery evidence in the client portal's document search
(see apps.platform.document_links)."""

import uuid
from collections import defaultdict

from django.db.models import Q

from apps.platform import document_links
from apps.platform.models import Document

from .delivery import LINK
from .models import Shipment, StopAction, StopShipment


def _describe(docs: list[Document]) -> dict[uuid.UUID, str]:
    drops = StopShipment.objects.filter(
        stop_id__in=document_links.linked_uuids(docs), action=StopAction.DROP
    ).select_related("shipment")
    at_stop: dict[tuple[str, uuid.UUID], list[Shipment]] = defaultdict(list)
    for drop in drops.order_by("shipment__number"):
        at_stop[(str(drop.stop_id), drop.client_id)].append(drop.shipment)
    labels = {}
    for doc in docs:
        # Only the document's own client's shipments: a stop never mixes clients today, but a label must not
        # be the place that would leak another client's order numbers if it ever did.
        shipments = at_stop.get((doc.linked_id, doc.client_id))
        if shipments:
            labels[doc.pk] = ", ".join(
                " · ".join(part for part in (s.number, s.order_number, s.client_ref) if part) for s in shipments
            )
    return labels


def _search(q: str, client_ids) -> list[str]:
    shipments = Shipment.objects.filter(
        Q(number__istartswith=q) | Q(order_number__istartswith=q) | Q(client_ref__istartswith=q)
    )
    if client_ids is not None:
        shipments = shipments.filter(client_id__in=client_ids)
    stops = (
        StopShipment.objects.filter(shipment__in=shipments, action=StopAction.DROP)
        .order_by("-stop_id")
        .values_list("stop_id", flat=True)
        .distinct()[: document_links.SEARCH_LIMIT]
    )
    return [str(pk) for pk in stops]


document_links.register(LINK, describe=_describe, search=_search)
