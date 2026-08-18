"""Reading the bear's named alternative back, one cycle later.

WHY THIS EXISTS. `substitute.py` (shipped 2026-08-08, `db7b3fe`) made the bear
name a ticker it would rather the desk owned than the one it just argued
against. Measured 2026-08-11 over the 131 decisions since that deploy: the
mechanism works — 41 `NAMED`, 13 `DECLINED` — and **nothing whatsoever read the
answer back.** The desk asked "what would you rather own?", got a real answer 41
times, and analysed the named ticker no sooner than it otherwise would have.

That is the missing half of the long-only story. A bear thesis on an unheld name
has no executable expression (`{BUY, HOLD}` is the whole menu, so 221 of 221
unheld bear wins became HOLD), but "own that one instead" IS executable — it is
a BUY of a different ticker, on the next cycle. This module is the carry: named
alternatives become discovery pressure on the pool the next cycle screens.

WHAT IT DELIBERATELY DOES NOT DO.
- It does not select, rank or admit anything. It returns demand counts; the
  scoring engine and the gatekeeper keep every decision they already made.
- It does not resurrect `OFF_POOL` names. Only `NAMED` is carried, and `NAMED`
  is by construction a ticker from the pool the bear was SHOWN
  (`cycle_candidates.shown_rows`), so it is already screened, priced and real.
  An unshown ticker is unscored and unpriced — `substitute.py` fails it closed
  for that reason and so does this.
- It never raises. A failure here must not be able to stop a cycle from
  starting; the pool is simply un-boosted, which is exactly today's behaviour.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: How far back to read named alternatives. Three days spans a weekend, so a
#: Friday bear still steers Monday's pool.
DEFAULT_LOOKBACK_HOURS = 72

#: Cap on how many distinct names are carried, so a pathological run cannot
#: flood the pool with substitutes at the expense of discovery.
MAX_CARRIED = 10


def recent_substitute_demand(
    hours: int = DEFAULT_LOOKBACK_HOURS,
    limit: int = MAX_CARRIED,
) -> dict[str, int]:
    """`{ticker: times a bear named it}` over the window, newest window first."""
    try:
        from datetime import datetime, timezone, timedelta
        from collections import Counter
        from app.db import mongo_store

        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(hours))
        docs = mongo_store.find_docs(
            "shared_desk",
            {"created_at": {"$gte": cutoff}},
            projection={"desk_data.bear_rebuttal.preferred_alternative": 1},
        )
        counts: Counter[str] = Counter()
        for d in docs:
            desk = d.get("desk_data") or {}
            pa = (desk.get("bear_rebuttal") or {}).get("preferred_alternative") or {}
            if pa.get("status") == "NAMED":
                tkr = (pa.get("ticker") or "").strip().upper()
                if tkr:
                    counts[tkr] += 1
        return dict(counts.most_common(int(limit)))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[SubstituteDemand] read failed (non-fatal, pool un-boosted): %s", e
        )
        return {}


def merge_into_pool(all_pool: dict, demand: dict[str, int]) -> list[str]:
    """Add named alternatives the pool does not already carry. Returns the adds.

    Mutates `all_pool` in place, matching how the discovery merges above it
    work. A ticker already in the pool is left alone — its existing label and
    mention counts are real discovery evidence and a substitute mention does
    not improve them.
    """
    added: list[str] = []
    for ticker, n in (demand or {}).items():
        if ticker in all_pool:
            continue
        all_pool[ticker] = {
            "label": "BearSubstitute",
            "source_count": 1,
            "total_mentions": int(n),
        }
        added.append(ticker)
    return added
