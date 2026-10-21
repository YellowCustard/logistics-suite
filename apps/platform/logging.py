import json
import logging
from datetime import UTC, datetime

from .context import actor_var, request_id_var


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with request id and actor for correlation."""

    def format(self, record: logging.LogRecord) -> str:
        actor = actor_var.get()
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": str(request_id_var.get() or ""),
            "actor": f"{actor.kind}:{actor.id or ''}",
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload["data"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
