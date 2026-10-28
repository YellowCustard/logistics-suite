"""What a document is evidence of, in words a client recognises.

A document stores only `linked_type` and `linked_id`; platform cannot read the business tables behind them. Each
module that links documents registers a describer and a search for its link type in a `document_links` module:

    document_links.register("tms.stop", describe=_describe, search=_search)

`describe(docs)` returns a short reference per document id ("SH-000012 · SO-000034 · PO-100"). `search(q,
client_ids)` returns the linked ids whose references start with `q`, limited to those clients (None: all).
"""

import uuid
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass

from django.utils.module_loading import autodiscover_modules

from .models import Document

Describe = Callable[[list[Document]], dict[uuid.UUID, str]]
Search = Callable[[str, Collection[uuid.UUID] | None], Iterable[str]]

# Enough for any one reference; a broader prefix than that is not a search a person means.
SEARCH_LIMIT = 500


@dataclass(frozen=True, slots=True)
class _Registered:
    describe: Describe
    search: Search


_registry: dict[str, _Registered] = {}
_discovered = False


def register(linked_type: str, *, describe: Describe, search: Search) -> None:
    existing = _registry.get(linked_type)
    if existing is not None and (existing.describe, existing.search) != (describe, search):
        raise ValueError(f"Document link {linked_type!r} is already registered")
    _registry[linked_type] = _Registered(describe, search)


def linked_uuids(docs: list[Document]) -> list[uuid.UUID]:
    """The documents' linked ids that are UUIDs (link types keyed by a row id always are)."""
    out = []
    for doc in docs:
        try:
            out.append(uuid.UUID(doc.linked_id))
        except ValueError:
            continue
    return out


def _discover() -> None:
    global _discovered
    if not _discovered:
        autodiscover_modules("document_links")
        _discovered = True


def describe(docs: list[Document]) -> dict[uuid.UUID, str]:
    _discover()
    by_type: dict[str, list[Document]] = defaultdict(list)
    for doc in docs:
        if doc.linked_type in _registry and doc.linked_id:
            by_type[doc.linked_type].append(doc)
    labels: dict[uuid.UUID, str] = {}
    for linked_type, group in by_type.items():
        labels.update(_registry[linked_type].describe(group))
    return labels


def search(q: str, client_ids: Collection[uuid.UUID] | None) -> dict[str, list[str]]:
    """Linked ids per link type whose business references start with `q`."""
    _discover()
    found: dict[str, list[str]] = {}
    for linked_type, entry in _registry.items():
        ids = list(entry.search(q, client_ids))[:SEARCH_LIMIT]
        if ids:
            found[linked_type] = ids
    return found
