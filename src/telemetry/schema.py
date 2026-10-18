"""Message schema, topics and strict validation for test-station telemetry."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Optional

STATUSES = ("pass", "fail", "error", "skip")
TOPIC_ROOT = "teststation"
RESULTS_PATTERN = f"{TOPIC_ROOT}/+/results"
HEARTBEAT_PATTERN = f"{TOPIC_ROOT}/+/heartbeat"


class ValidationError(ValueError):
    """Raised when a payload does not match the telemetry schema."""


def results_topic(station_id: str) -> str:
    return f"{TOPIC_ROOT}/{station_id}/results"


def heartbeat_topic(station_id: str) -> str:
    return f"{TOPIC_ROOT}/{station_id}/heartbeat"


def station_from_topic(topic: str) -> str:
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != TOPIC_ROOT or not parts[1]:
        raise ValidationError(f"unexpected topic: {topic!r}")
    return parts[1]


@dataclass(frozen=True)
class Result:
    station_id: str
    seq: int
    timestamp: float
    test_name: str
    suite: str
    build: str
    status: str
    duration_ms: float
    measurement: Optional[float] = None
    lower_limit: Optional[float] = None
    upper_limit: Optional[float] = None
    unit: Optional[str] = None

    def out_of_limits(self) -> bool:
        m = self.measurement
        if m is None:
            return False
        if self.lower_limit is not None and m < self.lower_limit:
            return True
        return self.upper_limit is not None and m > self.upper_limit

    def to_payload(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":")).encode()


@dataclass(frozen=True)
class Heartbeat:
    station_id: str
    seq: int
    timestamp: float
    build: str

    def to_payload(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":")).encode()


def _load(payload: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValidationError("payload must be a JSON object")
    return obj


def _text(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{key!r} must be a non-empty string")
    return value


def _number(obj: dict[str, Any], key: str, required: bool = True,
            minimum: Optional[float] = None) -> Optional[float]:
    value = obj.get(key)
    if value is None:
        if required:
            raise ValidationError(f"{key!r} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{key!r} must be a number")
    if not math.isfinite(value):
        raise ValidationError(f"{key!r} must be finite")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{key!r} must be >= {minimum}")
    return float(value)


def _integer(obj: dict[str, Any], key: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{key!r} must be a non-negative integer")
    return value


def parse_result(payload: bytes, topic: Optional[str] = None) -> Result:
    obj = _load(payload)
    station_id = _text(obj, "station_id")
    if topic is not None and station_from_topic(topic) != station_id:
        raise ValidationError("station_id does not match topic")
    status = _text(obj, "status")
    if status not in STATUSES:
        raise ValidationError(f"status must be one of {STATUSES}, got {status!r}")
    lower = _number(obj, "lower_limit", required=False)
    upper = _number(obj, "upper_limit", required=False)
    if lower is not None and upper is not None and lower > upper:
        raise ValidationError("lower_limit must not exceed upper_limit")
    unit = obj.get("unit")
    if unit is not None and not isinstance(unit, str):
        raise ValidationError("'unit' must be a string")
    return Result(
        station_id=station_id,
        seq=_integer(obj, "seq"),
        timestamp=_number(obj, "timestamp", minimum=0),
        test_name=_text(obj, "test_name"),
        suite=_text(obj, "suite"),
        build=_text(obj, "build"),
        status=status,
        duration_ms=_number(obj, "duration_ms", minimum=0),
        measurement=_number(obj, "measurement", required=False),
        lower_limit=lower,
        upper_limit=upper,
        unit=unit,
    )


def parse_heartbeat(payload: bytes, topic: Optional[str] = None) -> Heartbeat:
    obj = _load(payload)
    station_id = _text(obj, "station_id")
    if topic is not None and station_from_topic(topic) != station_id:
        raise ValidationError("station_id does not match topic")
    return Heartbeat(
        station_id=station_id,
        seq=_integer(obj, "seq"),
        timestamp=_number(obj, "timestamp", minimum=0),
        build=_text(obj, "build"),
    )
