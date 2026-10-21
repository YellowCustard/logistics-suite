"""All-or-nothing CSV imports with row-level error reporting.

Rows are validated with a pydantic model first; if every row is valid, they are written in one
transaction, each in a savepoint so database constraint violations are reported per row. Any error
rolls the whole file back. The uploaded file is kept as a Document for evidence.
"""

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction
from pydantic import BaseModel, ValidationError

from . import documents
from .apiutils import Problem
from .context import current_actor
from .models import DocumentKind

MAX_ROWS = 10_000


@dataclass(slots=True)
class ImportResult:
    created: int
    document_id: str


def _drop_blanks(raw: dict[str, Any]) -> dict[str, Any]:
    """Blank cells count as absent, so the schema default applies (or "Field required" is reported)."""
    cleaned = {k.strip(): v.strip() if isinstance(v, str) else v for k, v in raw.items() if k}
    return {k: v for k, v in cleaned.items() if v not in ("", None)}


def parse[Row: BaseModel](upload: UploadedFile, schema: type[Row]) -> list[Row]:
    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Problem(422, "Invalid CSV", "File must be UTF-8 encoded.") from exc
    finally:
        upload.seek(0)
    reader = csv.DictReader(io.StringIO(text))
    expected = set(schema.model_fields)
    required = {name for name, f in schema.model_fields.items() if f.is_required()}
    headers = {h.strip() for h in (reader.fieldnames or []) if h}
    missing = required - headers
    unknown = headers - expected
    if missing or unknown:
        errors = [{"row": 1, "field": h, "message": "required column missing"} for h in sorted(missing)]
        errors += [{"row": 1, "field": h, "message": "unknown column"} for h in sorted(unknown)]
        raise Problem(422, "Invalid CSV header", f"Expected columns: {', '.join(schema.model_fields)}", errors)

    rows: list[Row] = []
    errors: list[dict[str, Any]] = []
    for line_no, raw in enumerate(reader, start=2):  # row 1 is the header
        if line_no - 1 > MAX_ROWS:
            raise Problem(413, "Too many rows", f"Maximum {MAX_ROWS} rows per file.")
        try:
            rows.append(schema.model_validate(_drop_blanks(raw)))
        except ValidationError as exc:
            for err in exc.errors():
                field = ".".join(str(p) for p in err["loc"]) or "(row)"
                errors.append({"row": line_no, "field": field, "message": err["msg"]})
    if errors:
        raise Problem(422, "Invalid CSV rows", f"{len(errors)} error(s); nothing was imported.", errors)
    if not rows:
        raise Problem(422, "Empty CSV", "The file has a header but no rows.")
    return rows


def run[Row: BaseModel](
    upload: UploadedFile,
    schema: type[Row],
    create_row: Callable[[Row], Any],
    *,
    client_id=None,
    linked_type: str = "",
    linked_id: str = "",
) -> ImportResult:
    rows = parse(upload, schema)
    errors: list[dict[str, Any]] = []
    with transaction.atomic():
        for line_no, row in enumerate(rows, start=2):
            try:
                with transaction.atomic():
                    create_row(row)
            except Problem as exc:
                errors.append({"row": line_no, "field": "(row)", "message": exc.detail or exc.title})
            except (IntegrityError, DjangoValidationError) as exc:
                errors.append({"row": line_no, "field": "(row)", "message": _db_message(exc)})
        if errors:
            transaction.set_rollback(True)
            raise Problem(422, "CSV rows rejected", f"{len(errors)} row(s) failed; nothing was imported.", errors)
        doc = documents.store(
            upload,
            kind=DocumentKind.IMPORT_FILE,
            client_id=client_id,
            captured_by=current_actor().id,
            linked_type=linked_type,
            linked_id=linked_id,
        )
    return ImportResult(created=len(rows), document_id=str(doc.id))


def _db_message(exc: Exception) -> str:
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    if "duplicate key" in text:
        return "duplicate: a record with this key already exists"
    return text[:300]
