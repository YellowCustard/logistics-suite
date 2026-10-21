"""Per-request context (request id, actor) carried through contextvars into audit and outbox rows."""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Actor:
    kind: str  # "user" | "api_client" | "system"
    id: uuid.UUID | None
    label: str


SYSTEM_ACTOR = Actor(kind="system", id=None, label="system")

request_id_var: ContextVar[uuid.UUID | None] = ContextVar("request_id", default=None)
actor_var: ContextVar[Actor] = ContextVar("actor", default=SYSTEM_ACTOR)
correlation_id_var: ContextVar[uuid.UUID | None] = ContextVar("correlation_id", default=None)
causation_id_var: ContextVar[uuid.UUID | None] = ContextVar("causation_id", default=None)


def current_actor() -> Actor:
    return actor_var.get()
