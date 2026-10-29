"""One sign-in per surface: the admin, the scanner and the client portal each keep their own session cookie.

All three are served from one origin, so with Django's single session cookie a sign-in on one replaced the user on
the others (signing in to the scanner as an operator signed the superuser out of the admin). Here every request is
assigned a surface and reads and writes only that surface's cookie. Page requests are assigned by their path; API
calls by the `X-Nuronteq-App` header the scanner and portal send, else by the page they came from (the same-origin
Referer, which also covers image and download links), else the default cookie. Choosing a cookie grants nothing:
the session it names must still be valid, and Ninja still checks CSRF.
"""

import time
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import DisallowedHost
from django.utils.cache import patch_vary_headers
from django.utils.http import http_date

COOKIES = {
    "admin": "nq_admin_session",
    "scanner": "nq_scanner_session",
    "portal": "nq_portal_session",
}
PAGE_PREFIXES = (("/admin/", "admin"), ("/scanner/", "scanner"), ("/portal/", "portal"))
# The API docs are for staff signed in to the admin (docs_decorator=staff_member_required).
ADMIN_API_PATHS = ("/api/v1/docs", "/api/v1/openapi.json")
APP_HEADER = "X-Nuronteq-App"


def _by_path(path: str) -> str | None:
    return next((surface for prefix, surface in PAGE_PREFIXES if path.startswith(prefix)), None)


def surface_of(request) -> str | None:
    path = request.path_info
    if surface := _by_path(path):
        return surface
    if not path.startswith("/api/"):
        return None
    if path in ADMIN_API_PATHS:
        return "admin"
    app = request.headers.get(APP_HEADER, "").strip().lower()
    if app in ("scanner", "portal"):
        return app
    referer = urlsplit(request.headers.get("Referer", ""))
    try:
        same_origin = referer.netloc == request.get_host()
    except DisallowedHost:
        return None
    return _by_path(referer.path) if same_origin else None


def cookie_name(request) -> str:
    return COOKIES.get(surface_of(request) or "", settings.SESSION_COOKIE_NAME)


class SurfaceSessionMiddleware(SessionMiddleware):
    """Django's SessionMiddleware with the cookie name chosen per request; otherwise the same behaviour."""

    def process_request(self, request):
        request.session_cookie_name = cookie_name(request)
        request.session = self.SessionStore(request.COOKIES.get(request.session_cookie_name))

    def process_response(self, request, response):
        try:
            accessed = request.session.accessed
            modified = request.session.modified
            empty = request.session.is_empty()
            name = request.session_cookie_name
        except AttributeError:
            return response
        if name in request.COOKIES and empty:
            response.delete_cookie(
                name,
                path=settings.SESSION_COOKIE_PATH,
                domain=settings.SESSION_COOKIE_DOMAIN,
                samesite=settings.SESSION_COOKIE_SAMESITE,
            )
            need_vary_cookie = True
        else:
            need_vary_cookie = accessed
            if (modified or settings.SESSION_SAVE_EVERY_REQUEST) and not empty:
                if request.session.get_expire_at_browser_close():
                    max_age = expires = None
                else:
                    max_age = request.session.get_expiry_age()
                    expires = http_date(time.time() + max_age)
                if response.status_code < 500:
                    try:
                        request.session.save()
                    except UpdateError:
                        raise SessionInterrupted(
                            "The request's session was deleted before the request completed. The user may have "
                            "logged out in a concurrent request, for example."
                        ) from None
                    response.set_cookie(
                        name,
                        request.session.session_key,
                        max_age=max_age,
                        expires=expires,
                        domain=settings.SESSION_COOKIE_DOMAIN,
                        path=settings.SESSION_COOKIE_PATH,
                        secure=settings.SESSION_COOKIE_SECURE or None,
                        httponly=settings.SESSION_COOKIE_HTTPONLY or None,
                        samesite=settings.SESSION_COOKIE_SAMESITE,
                    )
                    need_vary_cookie = True
        if need_vary_cookie:
            patch_vary_headers(response, ("Cookie",))
        return response
