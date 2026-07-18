"""
Timezone Utilities — Centralised helpers for consistent UTC handling.

All DB timestamps are stored as naive UTC.  These helpers ensure every
value sent to the frontend carries a 'Z' suffix so JavaScript correctly
interprets them as UTC.
"""

import datetime


def utc_iso(dt) -> str | None:
    """Convert a datetime/date to an ISO-8601 string with UTC marker.

    - Naive datetime → append 'Z' (assumed UTC)
    - Aware datetime → convert to UTC, then append 'Z'
    - date objects → return date-only ISO (no time component)
    - str passthrough → append 'Z' if missing timezone indicator
    - None → None
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        s = dt.strip()
        # Already has timezone info
        if s.endswith("Z") or "+" in s[10:] or s[10:].count("-") > 0:
            return s
        return s + "Z"
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is not None:
            # Convert to UTC first
            utc_dt = dt.astimezone(datetime.timezone.utc)
            return utc_dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        # Naive → assumed UTC
        return dt.isoformat() + "Z"
    if isinstance(dt, datetime.date):
        return dt.isoformat()
    # Fallback for anything else
    return str(dt)


def ensure_aware(val) -> datetime.datetime | None:
    """Coerce a timestamp (datetime | ISO string | None) to a tz-aware UTC datetime.

    The inbound counterpart to utc_iso(). This exact idiom — fromisoformat +
    "if tzinfo is None: replace(tzinfo=utc)" — was hand-rolled at ~30 call
    sites (watch_desk, pipeline_service, retriever, collectors, ...), several
    of which forgot one of the shapes (psycopg returns TIMESTAMPTZ columns as
    datetime, JSON payloads carry strings, some strings end in 'Z' which
    fromisoformat < 3.11 rejects).

    - aware datetime  → returned as-is
    - naive datetime  → assumed UTC, tz attached
    - ISO-8601 string → parsed ('Z' suffix normalised), same rules
    - None / unparseable / other types → None
    """
    if val is None:
        return None
    if isinstance(val, datetime.datetime):
        dt = val
    elif isinstance(val, str):
        s = val[:-1] + "+00:00" if val.endswith("Z") else val
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt


def utc_now() -> datetime.datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.datetime.now(datetime.timezone.utc)


def utc_now_iso() -> str:
    """Return the current time as an ISO-8601 UTC string with 'Z'."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%S") + "Z"
