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


def _desk_data(doc: dict) -> dict:
    """`shared_desk.desk_data` as a dict, whichever shape it is stored in.

    Text today (`json.dumps` in `save_desk`), an embedded document for every
    desk written before the cutover. `load_desk` already accepts both; so must
    anything else that reads the field.
    """
    raw = (doc or {}).get("desk_data")
    if isinstance(raw, str):
        try:
            import json
            return json.loads(raw)
        except Exception:  # noqa: BLE001 — one corrupt desk is not an outage
            return {}
    return raw or {}


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
        # The WHOLE desk_data, parsed here — not a dotted projection.
        #
        # `save_desk` stores desk_data as `json.dumps(...)` TEXT, and a Mongo
        # projection cannot descend into a string: it returns no `desk_data`
        # key at all, so the document is skipped in silence — no exception, no
        # log line, one fewer substitute. Every desk written since the cutover
        # is text; the desks that still answered this query were the
        # pre-cutover ones, written as embedded documents. Measured
        # 2026-08-19: 36 document-shaped and 6 text-shaped desks in the
        # 72-hour window, all six of them from after the cutover — so this
        # function was days away from returning {} forever while looking
        # exactly as healthy as it does now.
        docs = mongo_store.find_docs(
            "shared_desk",
            {"created_at": {"$gte": cutoff}},
            projection={"desk_data": 1},
        )
        counts: Counter[str] = Counter()
        for d in docs:
            desk = _desk_data(d)
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
