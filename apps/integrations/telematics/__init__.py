"""Telematics adapters. `adapter()` returns the one named by settings.TELEMATICS_ADAPTER, or None when none is
configured (positions then come only from the webhook or not at all, and arrival falls back to the driver's tap)."""

from functools import cache

from django.conf import settings
from django.utils.module_loading import import_string

from ..ports import TelematicsPort


@cache
def _load(path: str) -> TelematicsPort:
    return import_string(path)()


def adapter() -> TelematicsPort | None:
    path = settings.TELEMATICS_ADAPTER
    return _load(path) if path else None
