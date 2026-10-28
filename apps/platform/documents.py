"""Evidence storage. Bytes go to the default storage backend under a new key; never overwritten."""

import hashlib
import mimetypes
import uuid
from datetime import datetime
from decimal import Decimal

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from .apiutils import Problem
from .ids import uuid7
from .models import Document

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "application/pdf", "text/csv"}
MAX_BYTES = 10 * 1024 * 1024


def sha256_of(upload: UploadedFile) -> str:
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


def store(
    upload: UploadedFile,
    *,
    kind: str,
    client_id: uuid.UUID | None,
    captured_by: uuid.UUID | None,
    captured_at: datetime | None = None,
    linked_type: str = "",
    linked_id: str = "",
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
) -> Document:
    mime = (upload.content_type or mimetypes.guess_type(upload.name or "")[0] or "").split(";")[0]
    if mime not in ALLOWED_MIME:
        raise Problem(415, "Unsupported media type", f"{mime or 'unknown'} is not accepted.")
    if upload.size is None or upload.size > MAX_BYTES:
        raise Problem(413, "File too large", f"Maximum {MAX_BYTES // (1024 * 1024)} MB.")

    digest = sha256_of(upload)
    now = timezone.now()
    ext = mimetypes.guess_extension(mime) or ""
    key = f"documents/{now:%Y/%m}/{uuid7()}{ext}"
    saved_key = default_storage.save(key, upload)
    try:
        with transaction.atomic():
            return Document.objects.create(
                client_id=client_id,
                kind=kind,
                storage_key=saved_key,
                original_name=(upload.name or "")[:255],
                sha256=digest,
                mime=mime,
                size_bytes=upload.size,
                captured_at=captured_at or now,
                captured_by=captured_by,
                latitude=latitude,
                longitude=longitude,
                linked_type=linked_type,
                linked_id=linked_id,
            )
    except Exception:
        default_storage.delete(saved_key)  # no orphaned bytes when the metadata insert fails
        raise


def open_bytes(document: Document):
    return default_storage.open(document.storage_key, "rb")
