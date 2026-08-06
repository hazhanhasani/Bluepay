from __future__ import annotations

from datetime import datetime, timezone


def as_utc(value: datetime | None) -> datetime | None:
    """Normalize a database datetime to timezone-aware UTC.

    SQLite may return a naive value even for ``DateTime(timezone=True)``.
    BluePay stores these values as UTC, so a missing offset must be restored
    as UTC rather than interpreted in the browser/device time zone.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc_z(value: datetime | None) -> str | None:
    normalized = as_utc(value)
    if normalized is None:
        return None
    return normalized.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def remaining_seconds(value: datetime | None, *, now: datetime | None = None) -> int:
    expires_at = as_utc(value)
    current = as_utc(now) or datetime.now(timezone.utc)
    if expires_at is None:
        return 0
    return max(0, int((expires_at - current).total_seconds()))
