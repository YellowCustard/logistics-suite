"""Serves the scanner PWA shell. All data goes through /api/v1 (session auth, CSRF) and /api/v1/sync."""

import hashlib
from functools import cache

from django.conf import settings
from django.contrib.staticfiles import finders
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

ASSETS = ("scanner/app.js", "scanner/app.css", "scanner/icon.svg")

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ]
)


@cache
def build_id() -> str:
    """Changes whenever a shell asset changes, so the service worker drops its old cache on the next visit."""
    digest = hashlib.sha256()
    for asset in ASSETS:
        path = finders.find(asset)
        if path is None:  # collected-only deployments: fall back to the manifest-hashed names
            from django.templatetags.static import static

            digest.update(static(asset).encode())
            continue
        with open(path, "rb") as fh:
            digest.update(fh.read())
    return digest.hexdigest()[:12]


def _context() -> dict:
    return {"build_id": build_id(), "csrf_cookie_name": settings.CSRF_COOKIE_NAME}


@require_GET
@ensure_csrf_cookie
def index(request):
    response = render(request, "scanner/index.html", _context())
    response["Content-Security-Policy"] = CSP
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def service_worker(request):
    response = render(request, "scanner/sw.js", _context(), content_type="text/javascript")
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def manifest(request):
    return render(request, "scanner/manifest.webmanifest", _context(), content_type="application/manifest+json")
