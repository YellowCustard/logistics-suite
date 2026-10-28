"""A stand-in for the telematics vendor, used until MiX Integrate credentials are available and in tests.

Positions are fed in by tests or the fleet simulator (`feed`) and handed out by `fetch_positions`, as a vendor's
polling API would. Webhook bodies use a simple JSON shape signed with HMAC-SHA256, so the webhook endpoint and its
signature check are exercised end to end:

    X-Telematics-Signature: <hex HMAC-SHA256 of the raw body, keyed with TELEMATICS_WEBHOOK_SECRET>
    {"positions": [{"unit_id": "...", "time": "2026-10-28T08:00:00Z", "lat": -26.1, "lon": 28.0,
                    "speed_kmh": 42.0, "heading": 180, "ignition": true}]}
"""

import hashlib
import hmac
import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime

from django.conf import settings

from ..ports import Position, TelematicsVehicle

SIGNATURE_HEADER = "X-Telematics-Signature"
MAX_WEBHOOK_POSITIONS = 5000


class FakeTelematics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._vehicles: list[TelematicsVehicle] = []
        self._positions: list[Position] = []

    # --- test and simulator controls ---

    def set_vehicles(self, vehicles: Sequence[TelematicsVehicle]) -> None:
        with self._lock:
            self._vehicles = list(vehicles)

    def feed(self, positions: Sequence[Position]) -> None:
        with self._lock:
            self._positions.extend(positions)

    def reset(self) -> None:
        with self._lock:
            self._vehicles, self._positions = [], []

    # --- TelematicsPort ---

    def list_vehicles(self) -> Sequence[TelematicsVehicle]:
        with self._lock:
            return list(self._vehicles)

    def fetch_positions(self, since: datetime, until: datetime) -> Iterator[Position]:
        with self._lock:
            found = sorted((p for p in self._positions if since < p.recorded_at <= until), key=lambda p: p.recorded_at)
        yield from found

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        secret = settings.TELEMATICS_WEBHOOK_SECRET
        given = headers.get(SIGNATURE_HEADER, "")
        if not secret or not given:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, given)

    def parse_webhook(self, body: bytes) -> Sequence[Position]:
        data = json.loads(body)
        rows = data.get("positions") if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_WEBHOOK_POSITIONS:
            raise ValueError("expected {'positions': [...]} with at most 5000 entries")
        positions = []
        for row in rows:
            recorded_at = datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00"))
            if recorded_at.tzinfo is None:
                raise ValueError("position times must carry a time zone")
            positions.append(
                Position(
                    telematics_id=str(row["unit_id"]),
                    recorded_at=recorded_at,
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                    speed_kmh=None if row.get("speed_kmh") is None else float(row["speed_kmh"]),
                    heading=None if row.get("heading") is None else float(row["heading"]),
                    ignition=None if row.get("ignition") is None else bool(row["ignition"]),
                )
            )
        return positions


def sign(body: bytes, secret: str | None = None) -> str:
    """The signature header value for a body, for tests and the load generator."""
    key = (secret or settings.TELEMATICS_WEBHOOK_SECRET).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()
