"""event_timing — turn a Finnhub earnings event into a precise UTC snipe time.

Used by the research governor's `once` auto-resolution: an earnings event is a
calendar `date` + a coarse `bmo`/`amc` day-part (no clock time), so we map it to
a concrete UTC datetime to land analysis right after the numbers drop. DST-correct
via US/Eastern. (Kept separate so a future Watch Desk `earnings_upcoming` trigger can
reuse it — see the plan backlog.)
"""

from datetime import datetime, timezone

import pytz

_ET = pytz.timezone("US/Eastern")


def earnings_event_to_run_at(date_str: str | None, hour: str | None) -> datetime | None:
    """Earnings (`date`, `hour`) → a UTC datetime to run analysis on fresh numbers.

    - bmo (before market open): report pre-open → snipe 09:45 ET (open reaction).
    - amc (after market close): report post-close → snipe 17:30 ET (after-hours).
    - dmh / unknown: snipe 16:15 ET (just after close).

    Returns None if the date can't be parsed. ET→UTC handles EDT/EST correctly.
    """
    if not date_str:
        return None
    try:
        d = datetime.strptime(str(date_str).strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    h = (hour or "").strip().lower()
    if h == "bmo":
        et = d.replace(hour=9, minute=45)
    elif h == "amc":
        et = d.replace(hour=17, minute=30)
    else:  # dmh / unknown
        et = d.replace(hour=16, minute=15)
    return _ET.localize(et).astimezone(timezone.utc)


def next_earnings_run_at(events: list[dict]) -> tuple[datetime | None, dict | None]:
    """From Finnhub earnings events, pick the soonest FUTURE one and return
    (run_at_utc, the_event). Returns (None, None) if none are usable/future."""
    now = datetime.now(timezone.utc)
    best: tuple[datetime, dict] | None = None
    for e in events or []:
        run_at = earnings_event_to_run_at(e.get("date"), e.get("hour"))
        if run_at is None or run_at <= now:
            continue
        if best is None or run_at < best[0]:
            best = (run_at, e)
    return (best[0], best[1]) if best else (None, None)
