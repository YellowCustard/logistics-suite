"""References for receipt photos in the client portal's document search (see apps.platform.document_links)."""

import uuid

from django.db.models import Q

from apps.oms import services as oms
from apps.platform import document_links
from apps.platform.models import Document

from .models import Receipt, ReceiptLine

LINK = "wms.receiptline"


def _describe(docs: list[Document]) -> dict[uuid.UUID, str]:
    lines = {
        str(line.pk): line
        for line in ReceiptLine.objects.filter(pk__in=document_links.linked_uuids(docs)).select_related(
            "receipt", "item"
        )
    }
    notices = oms.notice_references(list({line.receipt.inbound_notice_id for line in lines.values()}))
    labels = {}
    for doc in docs:
        line = lines.get(doc.linked_id)
        if line is None or line.receipt.client_id != doc.client_id:
            continue
        number, client_ref = notices.get(line.receipt.inbound_notice_id, ("", ""))
        notice = f"{number} ({client_ref})" if client_ref else number
        labels[doc.pk] = " · ".join(part for part in (line.receipt.number, notice, line.item.sku) if part)
    return labels


def _search(q: str, client_ids) -> list[str]:
    limit = document_links.SEARCH_LIMIT
    receipts = Receipt.objects.filter(
        Q(number__istartswith=q) | Q(inbound_notice_id__in=oms.notices_matching(q, client_ids=client_ids, limit=limit))
    )
    if client_ids is not None:
        receipts = receipts.filter(client_id__in=client_ids)
    lines = ReceiptLine.objects.filter(receipt__in=receipts).order_by("-pk").values_list("pk", flat=True)[:limit]
    return [str(pk) for pk in lines]


document_links.register(LINK, describe=_describe, search=_search)
