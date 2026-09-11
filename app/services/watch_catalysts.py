"""watch_catalysts — cheap calendar enrichment for a watch. No LLM, no network.

The desk had no concept of a scheduled event. Measured 2026-09-06: the AGX trip
that woke a cycle was "Argan, Inc. (AGX) Q2 2027 Earnings Call Transcript" —
fired 645 hours (27 days) after the prior analysis, on a *transcript* headline,
i.e. after the numbers were already public and priced. The earnings date was
knowable in advance and nothing scheduled around it.

The data was already there. `fundamentals.earnings_date` is written by three
collectors (finnhub, yfinance, finviz) — 8,598 rows carry one, and 126 distinct
tickers had a FUTURE earnings date at the time of writing. `economic_calendar`
carried 78 future macro rows. Neither was ever read by the Watch Desk.

`event_timing.earnings_event_to_run_at` already maps a date + bmo/amc day-part
to a precise UTC snipe time, and its docstring reserves itself for exactly this
("Kept separate so a future Watch Desk `earnings_upcoming` trigger can reuse
it"). This module is that consumer.

FRESHNESS IS THE WHOLE PROBLEM WITH THIS TABLE
----------------------------------------------
`fundamentals` is a per-day snapshot, and a stale snapshot's `earnings_date` is
a date in the PAST — EXLS's newest row on 2026-09-06 still said 2026-07-28.
A past earnings date is not "no catalyst"; it is a *stale reading*, and the two
must not collapse, because "no upcoming earnings" suppresses urgency while
"stale reading" should raise a question. So every returned calendar carries
`earnings_confidence` and `sources`, and a past date is reported as
`confidence="stale"` with the event dropped, never as an empty calendar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.services.event_timing import earnings_event_to_run_at

logger = logging.getLogger(__name__)

# How far ahead a macro event still counts as "proximate" for scoring.
MACRO_HORIZON_DAYS = 7
# A fundamentals snapshot older than this cannot vouch for an earnings date.
_SNAPSHOT_MAX_AGE_DAYS = 14
# Macro events below this importance are noise for a single-ticker decision.
_MACRO_MIN_IMPORTANCE = {"high", "medium"}


def _aware(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return None


def _earnings_from_fundamentals(ticker: str, now: datetime) -> tuple[dict | None, str]:
    """(event, confidence) from the newest usable fundamentals snapshot.

    Reads the newest FEW snapshots rather than only the newest: the three
    collectors write independent rows and the newest one is not necessarily the
    one that carries an earnings date (finviz writes it, yfinance often does
    not). Taking only `limit=1` would report "no earnings" for a ticker whose
    date sat one row down — a confident wrong answer, which is worse than a
    blank.
    """
    from app.db import mongo_query

    try:
        rows = mongo_query.find_rows(
            "fundamentals",
            {"ticker": ticker, "earnings_date": {"$ne": None}},
            ["earnings_date", "snapshot_date", "source"],
            sort=[("snapshot_date", -1)], limit=5,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[WatchCatalysts] fundamentals read failed for %s: %s", ticker, e)
        return None, "unavailable"

    best: tuple[datetime, datetime, str] | None = None   # (earnings, snapshot, source)
    saw_any = False
    for ed, sd, src in rows:
        ed, sd = _aware(ed), _aware(sd)
        if ed is None:
            continue
        saw_any = True
        if sd is not None and (now - sd) > timedelta(days=_SNAPSHOT_MAX_AGE_DAYS):
            continue                      # the snapshot itself is too old to vouch
        if ed <= now:
            continue                      # a past date: stale reading, not "no catalyst"
        if best is None or ed < best[0]:
            best = (ed, sd or now, src or "fundamentals")

    if best is None:
        # Distinguish "the table had rows but all were past/stale" from "nothing
        # at all". The first is a stale instrument; the second is a genuine gap.
        return None, ("stale" if saw_any else "absent")

    ed, sd, src = best
    # finviz/yfinance give a DATE with no clock time; finnhub's bmo/amc hour is
    # not carried into `fundamentals`. So the day-part is unknown and
    # event_timing maps it to 16:15 ET (just after close) — deliberately the
    # planning placeholder only. After-hours releases can occur later than
    # 16:15 ET; this timestamp does NOT prove that results are available.
    at = earnings_event_to_run_at(ed.date().isoformat(), None) or ed
    return {
        "kind": "earnings",
        "at": at,
        "confidence": "date_known_hour_assumed",
        "release_time_verified": False,
        "requires_release_verification": True,
        "label": f"{ticker} earnings ({ed.date().isoformat()})",
        "source": str(src),
    }, "date_known_hour_assumed"


def _macro_events(now: datetime) -> list[dict]:
    """Upcoming high/medium-importance macro events inside the horizon."""
    from app.db import mongo_query

    try:
        rows = mongo_query.find_rows(
            "economic_calendar",
            {"event_date": {"$gte": now.replace(tzinfo=None),
                            "$lte": (now + timedelta(days=MACRO_HORIZON_DAYS)).replace(tzinfo=None)}},
            ["event_name", "event_date", "importance", "country", "source"],
            sort=[("event_date", 1)], limit=40,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[WatchCatalysts] economic_calendar read failed: %s", e)
        return []

    out = []
    for name, when, importance, country, source in rows:
        when = _aware(when)
        if when is None:
            continue
        imp = (importance or "").strip().lower()
        if imp not in _MACRO_MIN_IMPORTANCE:
            continue
        out.append({
            "kind": "macro",
            "at": when,
            "confidence": imp,
            "label": f"{name} ({country})" if country else str(name),
            "source": str(source or "economic_calendar"),
        })
    return out


def catalyst_calendar(ticker: str, now: datetime | None = None) -> dict:
    """Everything scheduled that could move this ticker. Never raises.

    Returns the shape `watch_schema.normalize_catalyst_calendar` accepts, with
    `sources` always populated — so "we looked and found nothing" is
    distinguishable from "we never looked". A caller that cannot tell those
    apart will read a lookup failure as an absence of catalysts and suppress
    urgency exactly when the instrument is broken.
    """
    now = now or datetime.now(timezone.utc)
    ticker = (ticker or "").upper().strip()
    events: list[dict] = []
    sources = ["fundamentals", "economic_calendar"]

    earnings, confidence = (None, "absent")
    if ticker:
        try:
            earnings, confidence = _earnings_from_fundamentals(ticker, now)
        except Exception as e:  # noqa: BLE001
            logger.warning("[WatchCatalysts] earnings lookup failed for %s: %s", ticker, e)
            earnings, confidence = None, "unavailable"
    if earnings:
        events.append(earnings)

    try:
        events.extend(_macro_events(now))
    except Exception as e:  # noqa: BLE001
        logger.warning("[WatchCatalysts] macro lookup failed: %s", e)

    events.sort(key=lambda e: e["at"])
    return {
        "events": events,
        "earnings_at": earnings["at"] if earnings else None,
        "earnings_confidence": confidence,
        "sources": sources,
        "checked_at": now,
    }


def hours_to_next(calendar: dict, kind: str | None = None, now: datetime | None = None) -> float | None:
    """Hours until the next event (optionally of one kind). None if there is none.

    None means "no scheduled event", and a caller must not turn it into 0 or
    into a large number silently — both make a missing calendar look like a
    measurement. `catalyst_urgency` below is the only intended consumer and it
    maps None to zero urgency explicitly.
    """
    now = now or datetime.now(timezone.utc)
    best = None
    for e in (calendar or {}).get("events") or []:
        if kind and e.get("kind") != kind:
            continue
        at = _aware(e.get("at"))
        if at is None or at <= now:
            continue
        d = (at - now).total_seconds() / 3600.0
        if best is None or d < best:
            best = d
    return best


def catalyst_urgency(calendar: dict, now: datetime | None = None) -> tuple[float, dict]:
    """Urgency in [0, 1] from calendar proximity, plus the detail that produced it.

    The curve is deliberately NOT monotone-decreasing in time-to-event. Peak
    urgency sits in the window just AFTER the numbers land, not the days before:
    analysing the day before earnings spends a full cycle on a thesis that a
    known, imminent, unknowable fact is about to rewrite. The measured AGX case
    is the other tail — a transcript headline 27 days late.

    Returns (score, detail) so the dashboard can print WHY a trip was urgent
    rather than only how much.
    """
    now = now or datetime.now(timezone.utc)
    detail: dict = {"reason": "no_scheduled_catalyst"}
    cal = calendar or {}

    earn_at = _aware(cal.get("earnings_at"))
    if earn_at is not None:
        h = (earn_at - now).total_seconds() / 3600.0
        detail = {"reason": "earnings", "hours_to_earnings": round(h, 1),
                  "confidence": cal.get("earnings_confidence")}
        if h < 0:
            # Post-release. The result and guidance are the highest-value
            # information the desk ever gets, and it decays fast.
            since = -h
            if since <= 36:
                return 1.0, {**detail, "phase": "post_release"}
            if since <= 24 * 5:
                return 0.5, {**detail, "phase": "post_release_decaying"}
            return 0.1, {**detail, "phase": "post_release_stale"}
        if h <= 18:
            # Imminent and unknowable — deliberately SUPPRESSED, not raised.
            return 0.05, {**detail, "phase": "pre_release_blackout"}
        if h <= 24 * 3:
            return 0.6, {**detail, "phase": "pre_release_positioning"}
        if h <= 24 * 10:
            return 0.3, {**detail, "phase": "pre_release_far"}
        return 0.1, {**detail, "phase": "pre_release_distant"}

    if cal.get("earnings_confidence") == "stale":
        # A stale instrument is a reason to ask, not a reason to relax.
        detail = {"reason": "earnings_date_stale"}
        return 0.25, detail

    hm = hours_to_next(cal, kind="macro", now=now)
    if hm is not None:
        detail = {"reason": "macro", "hours_to_macro": round(hm, 1)}
        return (0.3 if hm <= 48 else 0.15), detail

    return 0.0, detail
