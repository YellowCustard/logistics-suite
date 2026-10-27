"""Outbox consumers owned by the platform (discovered by outbox.discover_handlers)."""

from . import webhooks  # noqa: F401 - importing registers the webhook fan-out consumers
