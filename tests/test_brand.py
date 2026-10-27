"""Nuronteq brand assets: the scanner shell caches everything it renders, and the admin wears the skin."""

import re

import pytest
from django.contrib.staticfiles import finders
from django.test import Client

from apps.scanner.views import ASSETS

pytestmark = pytest.mark.django_db


def test_every_cached_shell_asset_exists():
    missing = [asset for asset in ASSETS if finders.find(asset) is None]
    assert missing == []


def test_tokens_reference_only_bundled_fonts():
    # The scanner CSP is default-src 'self' and the shell must render offline, so no font may come from elsewhere.
    css = open(finders.find("brand/tokens.css")).read()
    urls = re.findall(r'url\("([^"]+)"\)', css)
    assert urls and all(not u.startswith(("http:", "https:", "//")) for u in urls)
    assert {f"brand/{u}" for u in urls} <= set(ASSETS)


def test_scanner_shell_worker_and_manifest_use_the_brand():
    client = Client()
    page = client.get("/scanner/").content.decode()
    assert "brand/tokens.css" in page and "brand/favicon.svg" in page and "brand/mark-simple.svg" in page
    worker = client.get("/scanner/sw.js").content.decode()
    for asset in ASSETS:
        assert asset in worker
    manifest = client.get("/scanner/manifest.webmanifest").json()
    assert manifest["theme_color"] == "#0b1020"
    icons = {(icon["sizes"], icon.get("purpose", "any")) for icon in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= icons
    for icon in manifest["icons"]:
        assert finders.find(icon["src"].removeprefix("/static/")) is not None


def test_admin_login_wears_the_brand_skin():
    page = Client().get("/admin/login/").content.decode()
    assert "brand/admin.css" in page and "brand/mark-simple.svg" in page
