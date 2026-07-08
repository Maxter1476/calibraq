"""UTC time handling helpers.

Convention: everything in CalibraQ is UTC. SQLite stores timestamps tz-naive,
so the ORM layer stores UTC-naive datetimes and the Pydantic layer re-attaches
UTC on the way out.
"""

from __future__ import annotations

from datetime import datetime, timezone


def to_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` as a tz-aware UTC datetime.

    Naive datetimes are assumed to already be UTC (the storage convention).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_utc_naive(value: datetime | None) -> datetime | None:
    """Return ``value`` converted to UTC with tzinfo stripped, for SQLite storage."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.replace(tzinfo=None)


def utcnow() -> datetime:
    """Current time as a tz-aware UTC datetime."""
    return datetime.now(timezone.utc)
