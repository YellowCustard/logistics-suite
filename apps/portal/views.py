"""Serves the client portal: a React app (source in /portal) whose build lands in static/portal. Every data call goes
through /api/v1 with session auth and CSRF, so client isolation is enforced by the API, never by this page."""

import json
from functools import cache
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

MANIFEST = "portal/.vite/manifest.json"

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ]
)


def _read_entry() -> dict | None:
    """The built entry script and stylesheets, from the Vite manifest; None when the portal hasn't been built."""
    path = finders.find(MANIFEST) or Path(settings.STATIC_ROOT) / MANIFEST
    try:
        with open(path, encoding="utf-8") as fh:
            entry = json.load(fh)["index.html"]
    except (OSError, KeyError, ValueError):
        return None
    return {"js": f"portal/{entry['file']}", "css": [f"portal/{css}" for css in entry.get("css", [])]}


_cached_entry = cache(_read_entry)


@require_GET
@ensure_csrf_cookie
def index(request):
    entry = _read_entry() if settings.DEBUG else _cached_entry()  # pick up rebuilds while developing
    context = {"entry": entry, "csrf_cookie_name": settings.CSRF_COOKIE_NAME}
    response = render(request, "portal/index.html", context, status=200 if entry else 503)
    response["Content-Security-Policy"] = CSP
    response["Cache-Control"] = "no-cache"
    return response
