"""Idempotency-Key handling for every mutating API request.

The whole request runs in one transaction that first inserts the IdempotencyRecord. A concurrent
request with the same key blocks on the unique index until the first commits, then replays its
stored response. Only successful (< 400) responses are stored: any error response rolls everything
back, including the record and any partial writes, so the client may fix the request and retry with
the same key.
"""

import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from .auth import api_client_from_token
from .models import IdempotencyRecord

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
HEADER = "Idempotency-Key"
MAX_KEY_LENGTH = 200


def problem(status: int, title: str, detail: str) -> JsonResponse:
    return JsonResponse(
        {"type": "about:blank", "title": title, "status": status, "detail": detail},
        status=status,
        content_type="application/problem+json",
    )


def _scope(request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        api_client = api_client_from_token(auth[7:].strip())
        return f"api_client:{api_client.pk}" if api_client else None
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return f"user:{user.pk}"
    return None


def _request_hash(request) -> str:
    digest = hashlib.sha256()
    digest.update(request.method.encode())
    digest.update(b"\0")
    digest.update(request.get_full_path().encode())
    digest.update(b"\0")
    digest.update(request.body)
    return digest.hexdigest()


class IdempotencyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.prefix = "/api/"
        self.exempt = tuple(settings.IDEMPOTENCY_EXEMPT_PREFIXES)

    def __call__(self, request):
        if (
            request.method not in MUTATING
            or not request.path.startswith(self.prefix)
            or request.path.startswith(self.exempt)
        ):
            return self.get_response(request)
        scope = _scope(request)
        if scope is None:  # unauthenticated: let the API return 401 without touching the store
            return self.get_response(request)
        key = request.headers.get(HEADER, "").strip()
        if not key:
            return problem(
                400, "Idempotency-Key required", f"Send a unique {HEADER} header with every {request.method} request."
            )
        if len(key) > MAX_KEY_LENGTH:
            return problem(400, "Idempotency-Key too long", f"Maximum {MAX_KEY_LENGTH} characters.")
        request_hash = _request_hash(request)

        with transaction.atomic():
            try:
                with transaction.atomic():
                    record = IdempotencyRecord.objects.create(
                        scope=scope,
                        key=key,
                        request_hash=request_hash,
                        response_status=0,
                        expires_at=timezone.now() + timedelta(hours=settings.IDEMPOTENCY_TTL_HOURS),
                    )
            except IntegrityError:
                return self._replay(scope, key, request_hash)

            response = self.get_response(request)
            if response.status_code >= 400 or getattr(response, "streaming", False):
                transaction.set_rollback(True)
                return response
            record.response_status = response.status_code
            record.response_body = {
                "content_type": response.get("Content-Type", "application/json"),
                "text": response.content.decode("utf-8", errors="replace"),
            }
            record.save(update_fields=["response_status", "response_body"])
            return response

    @staticmethod
    def _replay(scope: str, key: str, request_hash: str) -> HttpResponse:
        record = IdempotencyRecord.objects.filter(scope=scope, key=key).first()
        if record is None:  # the original rolled back between our insert attempt and this read
            return problem(409, "Request in progress", "Retry this request.")
        if record.request_hash != request_hash:
            return problem(
                422, "Idempotency-Key reused", "This key was already used with a different request body or path."
            )
        body = record.response_body or {}
        response = HttpResponse(
            body.get("text", ""),
            status=record.response_status,
            content_type=body.get("content_type", "application/json"),
        )
        response["Idempotent-Replayed"] = "true"
        return response


def purge_expired() -> int:
    deleted, _ = IdempotencyRecord.objects.filter(expires_at__lt=timezone.now()).delete()
    return deleted
