"""Protocols the core depends on (Delivery Specification, section 17)."""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Position:
    telematics_id: str
    recorded_at: datetime  # timezone-aware UTC
    lat: float
    lon: float
    speed_kmh: float | None = None
    heading: float | None = None
    ignition: bool | None = None


@dataclass(frozen=True, slots=True)
class TelematicsVehicle:
    telematics_id: str
    registration: str


class TelematicsPort(Protocol):
    def list_vehicles(self) -> Sequence[TelematicsVehicle]: ...

    def fetch_positions(self, since: datetime, until: datetime) -> Iterator[Position]: ...

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    def parse_webhook(self, body: bytes) -> Sequence[Position]: ...
