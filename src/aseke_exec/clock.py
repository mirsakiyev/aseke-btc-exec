"""Injected clocks. Business logic never calls datetime.now() directly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

UTC = timezone.utc


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Deterministic clock for tests and scenarios."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware start time")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0, **kwargs: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds, **kwargs)
        return self._now

    def set(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            raise ValueError("aware datetime required")
        self._now = dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: object) -> datetime:
    """Parse an ISO-8601 timestamp that MUST carry a zero UTC offset."""
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("timestamp must be an ISO-8601 string with date and time")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("timestamp must be UTC-aware (use 'Z' or '+00:00')")
    if dt.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be expressed in UTC (offset +00:00)")
    return dt.astimezone(UTC)


def parse_utc_opt(value: object) -> datetime | None:
    return None if value is None else parse_utc(value)
