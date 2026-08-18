"""A candidate pool for a wake, so the bear on a name we OWN can be asked.

MEASURED 2026-08-12, over 149 desks since the label shipped:

    candidate pool present ......  2 of 33 held desks   (90 of 115 unheld)
    bear substitute NOT_ASKED ... 21 of 23 held desks that produced an artifact
    bear won the debate .........  0 of 26 held debates (54 of 78 unheld, 69%)

`NOT_ASKED` means *there was no pool*. A Watch Desk wake names one ticker and
bypasses discovery, so `cycle_candidates` is `[]` — and the names the desk owns
are exactly the ones it re-looks at on a wake. The result is that the substitute
axis, which `hold_reason` calls its PRIMARY axis, is unavailable on precisely
the population where an exit decision matters.

Under the unheld bear-win base rate of 69%, observing 0 bear wins in 26 held
debates has probability ~0.31^26 — this is not sampling noise. The desk cannot
argue for leaving a position when it has nothing to leave *for*.

WHAT THIS DOES. When a held name is re-looked at with no pool, reuse the ticker
list from the desk's most recent FULL cycle. Those names were chosen by the same
scoring engine, capped by the same sector rule, and shown to that cycle's own
agents. No discovery re-run, no model call, no fetch beyond one indexed read.

WHY THIS DOES NOT VIOLATE `cycle_candidates._FIELDS`. That module refuses to
read `decision_scores` because, *within one cycle*, the other names have not
been scored yet — reaching for them would race the concurrent fan-out or render
blanks. This reads a **prior, completed** cycle, where those rows are final and
nothing is in flight. Different fact, and the ban does not apply.

WHY THE SCREEN NUMBERS ARE DROPPED. `chg` and `rvol` are intraday. Re-rendering
yesterday's relative volume under a header that reads as current is a freshness
defect wearing a data table, and this desk has been bitten by exactly that shape
before. The tickers and the age of the list are what the substitute question
needs; the bear has tools if it wants a quote.

DELIBERATELY HELD-ONLY. Unheld pool-less desks are left untouched, and stay as
the control group: if held-desk `NAMED`/`DECLINED` rises while the unheld
pool-less rate does not move, the pool is what did it. Widening this to every
wake would buy coverage and lose the comparison.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How stale a pool may be and still be offered as "names the desk scored
#: recently". Measured 2026-08-12: a full cycle lands every few hours, so the
#: most recent pool is normally <12h old and this window is slack, not a
#: stretch. A list older than this is dropped rather than shown with an
#: apology — an alternatives table nobody has looked at in two days invites the
#: bear to name something the desk has no current read on.
MAX_AGE_HOURS = 48

#: Mirrors `cycle_candidates.MAX_CANDIDATES` — the gatekeeper's own budget.
MAX_TICKERS = 12


def _as_dict(v: Any) -> dict:
    """JSONB reaches this driver as a str often enough to be the default case."""
    if isinstance(v, dict):
        return v
    if isinstance(v, (str, bytes)):
        try:
            out = json.loads(v)
            return out if isinstance(out, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def build_wake_pool(
    self_ticker: str,
    *,
    exclude_cycle_id: str = "",
    max_age_hours: int = MAX_AGE_HOURS,
    limit: int = MAX_TICKERS,
) -> dict:
    """The most recent full cycle's candidate names, or an empty record.

    Returns ``{"tickers": [...], "cycle_id": str, "as_of": datetime|None,
    "age_hours": float|None, "reason": str}``. `reason` is always set, including
    on success, so a reader never has to infer WHY a desk has no pool — that
    inference is the `NOT_ASKED` conflation this module exists to end.

    Never raises. A pool is an enrichment; a desk that cannot build one runs
    exactly as it does today.
    """
    me = (self_ticker or "").upper().strip()
    empty = {"tickers": [], "cycle_id": "", "as_of": None, "age_hours": None}

    try:
        from datetime import datetime, timezone, timedelta
        from app.db import mongo_store

        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(max_age_hours))
        query = {
            "created_at": {"$gte": cutoff},
            "cycle_id": {"$ne": exclude_cycle_id or ""},
        }
        docs = mongo_store.find_docs(
            "shared_desk",
            query,
            sort=[("created_at", -1)],
            limit=200,
        )
        rows = [(d.get("cycle_id"), d.get("created_at"), d.get("desk_data")) for d in docs]
    except Exception as e:  # noqa: BLE001
        logger.warning("[WakePool] %s: pool lookup failed (non-fatal): %s: %s",
                       me, type(e).__name__, e)
        return {**empty, "reason": "lookup_failed"}

    if not rows:
        return {**empty, "reason": "no_recent_desks"}

    for cycle_id, created_at, desk_data in rows:
        meta = _as_dict(_as_dict(desk_data).get("cycle_metadata"))
        pool = meta.get("cycle_candidate_tickers") or []
        if not isinstance(pool, list) or not pool:
            continue

        # Exclude the name we are re-looking at. A "substitute" that is the
        # position itself is not an answer, and `substitute.read_substitute`
        # would reject it anyway — better it never appears than that the bear
        # names it and gets an OFF_POOL it cannot see the reason for.
        tickers = [
            str(t).upper().strip() for t in pool
            if str(t or "").upper().strip() and str(t).upper().strip() != me
        ]
        if not tickers:
            continue

        age = None
        if isinstance(created_at, datetime):
            ref = created_at if created_at.tzinfo else created_at.replace(
                tzinfo=timezone.utc)
            age = round(
                (datetime.now(timezone.utc) - ref).total_seconds() / 3600.0, 1)

        return {
            "tickers": tickers[:limit],
            "cycle_id": str(cycle_id),
            "as_of": created_at,
            "age_hours": age,
            "reason": "ok",
        }

    return {**empty, "reason": "no_pool_in_window"}


def build_wake_pool_block(record: dict, *, self_ticker: str = "") -> str:
    """Render the wake pool, or "" when there is nothing to show.

    A separate renderer from `cycle_candidates.build_candidate_block` because
    the two make different promises. That block says "the scoring engine's
    ranked shortlist, computed before any agent ran" and prints live screen
    numbers; this one can promise neither, and reusing its header would state
    two things that are not true of these rows.

    The ASK is byte-identical in intent to the live block's, deliberately: the
    two populations must be answering the same question or the comparison
    between them measures the prompt instead of the pool.
    """
    tickers = [t for t in (record or {}).get("tickers") or []
               if t and t != (self_ticker or "").upper().strip()]
    if not tickers:
        return ""

    age = record.get("age_hours")
    when = f"{age:g}h ago" if isinstance(age, (int, float)) else "recently"

    return "\n".join([
        "## NAMES THE DESK SHORTLISTED IN ITS LAST FULL CYCLE",
        (f"You are re-looking at a position the desk ALREADY OWNS, so this "
         f"cycle ran no discovery of its own. These are the alternatives the "
         f"scoring engine put in front of the desk {when} — same engine, same "
         f"sector cap, and agents did read them that cycle."),
        "",
        "  " + ", ".join(tickers),
        "",
        ("**Screen numbers are deliberately omitted.** They are intraday and "
         "these are not. Treat this as a list of names, not as a ranking, and "
         "look one up if you want its current tape."),
        ("**A negative view is only actionable on this book if it names "
         "something better.** The desk is long-only, so 'keep this' and 'sell "
         "this and own that instead' are the executable readings of your case. "
         "If your thesis is negative, say which of the names above you would "
         "rather the desk owned, and why — or say plainly that none of them is "
         "better, which is also a real answer."),
    ])
