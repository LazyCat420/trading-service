#!/usr/bin/env python3
"""Per-cycle agent latency, keyed on the number chapter 34 actually argues over.

The decisive column is **the share of agent runs over 300 s**, not the median.
300 s is prism's idle-watchdog kill threshold, so it is the one latency figure
that maps onto the incident: past it, a run does not come back slow, it comes
back dead.

Width is the confound, which is why every row carries its ticker count. Chapter
34 measured every 1-ticker cycle at 0% over 300 s and every 6-to-9-ticker cycle
at 34-62%. A narrow cycle therefore proves nothing about a change aimed at
concurrency — it never generated any. Only compare rows of comparable width.

  --self-test replays the six pre-fix wide cycles chapter 34 published and
  fails unless they land back in the 34-62% band. A tool that cannot reproduce
  a published number is not measuring the published thing.

READS MONGO (ported 2026-08-30). It read Postgres until then, which for a
latency report is the worst possible source: `v3_agent_telemetry` stopped
taking rows at the 2026-08-19 cutover, so every run printed a table of July
cycles, correctly formatted, with today's date nowhere in it — and `--days 2`
answered "no cycles matched" as though the fleet had gone quiet. Measured at
the port: Postgres 7,613 runs / 340 cycles, frozen at 2026-08-19 22:54; Mongo
8,787 runs / 430 cycles, current to the minute.

WHY THE ARITHMETIC IS IN PYTHON. Two of these columns have no faithful server
-side equivalent, and an approximation here is not a rounding difference, it is
a different verdict:

  * `percentile_cont` interpolates between order statistics. Mongo's
    `$percentile` accumulator is t-digest and documents itself as APPROXIMATE,
    so a median computed with it cannot be checked against a published number —
    it would disagree with chapter 34 by an unknown amount and there would be
    no way to tell that from a real regression. The elapsed values are pushed
    per cycle (~100 per group) and interpolated here, exactly as Postgres does.
  * `round(numeric, 1)` is half-UP; Python's built-in `round` is half-even, so
    36.25% prints as 36.2 instead of 36.3. Every rounded column goes through
    `_round1`, which is Decimal half-up.

Usage:
  scripts/agent_latency_report.py                      # every wide cycle, recent first
  scripts/agent_latency_report.py --cycle <id>
  scripts/agent_latency_report.py --min-tickers 1 --days 2
  scripts/agent_latency_report.py --json
  scripts/agent_latency_report.py --self-test
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_store  # noqa: E402

# The POSTGRES TABLE NAME, which is what every mongo_store helper takes and the
# only thing a caller is allowed to pass. `mongo_store._coll` resolves it to a
# physical collection through `app/db/collections.collection_for` exactly once,
# and its docstring says why a caller must not do that itself: "Never take a
# collection name from a caller ... a name that bypasses this function does not
# error; it silently starts a second, invisible collection." Resolving here as
# well would also hand `app/db/date_fields` the WRONG key -- the timestamp
# registry is keyed on the table, so `TIMESTAMP_FIELDS['v3_agent_telemetry']`
# is {'created_at'} while the collection name carries none, and the `--days`
# bound would stop being coerced the moment the renames are activated. That is
# the registry that exists to stop a string bound from being compared against
# BSON dates, so it has to stay armed. `app/v3/invariants.py` reads this same
# table the same way: mongo_store.aggregate("v3_agent_telemetry", ...).
TABLE = "v3_agent_telemetry"

# prism kills a generation that has been idle this long. Chapter 34's metric.
WATCHDOG_MS = 300_000

# The six pre-fix wide cycles chapter 34 published, and the band it reported.
# These are the positive control: the query has to put them back where the
# chapter found them.
CONTROL_CYCLES = (
    "cycle-v3-1786297004",
    "cycle-v3-1786249531",
    "cycle-v3-1786241506",
    "cycle-v3-1786155154",
    "cycle-v3-1786109400",
    "cycle-v3-1786074021",
)
CONTROL_BAND = (34.0, 62.5)


def _round1(value):
    """`round(x::numeric, 1)` — Postgres rounds half-UP, Python half-EVEN.

    `round(36.25, 1)` is 36.2 in Python and 36.3 in Postgres. On the one column
    this report exists for that is the difference between a cycle landing
    inside chapter 34's published band and outside it, so the rounding mode is
    part of the translation, not a detail.

    Via `str()`: `Decimal(36.25)` is the float's true binary value, which is
    what the `float8 -> numeric` cast in the original SQL also renders (shortest
    round-trip text), so the two agree digit for digit.
    """
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _percentile_cont(ordered, p):
    """`percentile_cont(p) WITHIN GROUP (ORDER BY x)`.

    Postgres interpolates linearly between the two order statistics either side
    of `p * (n - 1)` — it does NOT pick an existing element the way
    `percentile_disc` does. `ordered` must already be sorted ascending.
    """
    n = len(ordered)
    if n == 0:
        return None
    pos = p * (n - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    return float(ordered[lo]) + (float(ordered[hi]) - float(ordered[lo])) * (pos - lo)


def _match(cycle=None, days=None, cycles=None) -> dict:
    """The WHERE clause. `elapsed_ms IS NOT NULL` is `$ne: None`, which excludes
    a null AND a document that never had the field — both of which SQL counted
    as NULL."""
    clauses = [{"elapsed_ms": {"$ne": None}}]
    if cycle:
        clauses.append({"cycle_id": cycle})
    if cycles:
        clauses.append({"cycle_id": {"$in": list(cycles)}})
    if days:
        clauses.append(
            {"created_at": {"$gt": datetime.now(timezone.utc) - timedelta(days=days)}}
        )
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def pipeline(cycle=None, min_tickers=6, days=None, limit=40, cycles=None) -> list[dict]:
    """The GROUP BY / HAVING / ORDER BY / LIMIT, as a pipeline.

    The stage order is the SQL's order and cannot be rearranged: HAVING filters
    groups BEFORE the sort and the limit, so moving the width floor after
    `$limit` would take the 40 most recent cycles and then discard the narrow
    ones — a report of 12 rows where the SQL returned 40.

    `min_tickers=6` is the confound control, not a convenience: chapter 34
    scored every 1-ticker cycle at 0% over the watchdog, so a default that let
    narrow cycles in would dilute the one column this report exists for.
    """
    stages: list[dict] = [
        {"$match": _match(cycle=cycle, days=days, cycles=cycles)},
        {"$group": {
            "_id": "$cycle_id",
            # count(DISTINCT ticker): $addToSet keeps NULL, SQL's COUNT does
            # not, so the nulls come out below before the size is taken.
            "tickers": {"$addToSet": "$ticker"},
            "runs": {"$sum": 1},
            # count(*) FILTER (WHERE elapsed_ms > watchdog)
            "over": {"$sum": {"$cond": [{"$gt": ["$elapsed_ms", WATCHDOG_MS]}, 1, 0]}},
            # percentile_cont has no exact server-side form; interpolate here.
            "elapsed": {"$push": "$elapsed_ms"},
            "started": {"$min": "$created_at"},
            "ended": {"$max": "$created_at"},
        }},
        {"$addFields": {"tickers": {"$size": {"$filter": {
            "input": "$tickers", "cond": {"$ne": ["$$this", None]}}}}}},
        {"$match": {"tickers": {"$gte": min_tickers}}},
        # ORDER BY min(created_at) DESC. The -1 is the point of the whole port:
        # ascending would put July at the top of a report whose reason for
        # existing is that it used to print July at the top.
        {"$sort": {"started": -1}},
    ]
    if limit:
        stages.append({"$limit": limit})
    return stages


def _utc(dt):
    """Postgres handed back an aware UTC timestamp; BSON stores naive UTC. Only
    the tzinfo is restored — the value is not shifted, and the millisecond
    precision is left alone because it is the provenance: `...:59.657000` is
    Mongo answering where `...:59.657498` was Postgres."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _row(doc) -> dict:
    runs = doc["runs"]
    ordered = sorted(doc["elapsed"])
    started, ended = _utc(doc["started"]), _utc(doc["ended"])
    span_s = Decimal(0)
    if started and ended:
        delta = ended - started
        span_s = (Decimal(delta.days * 86400 + delta.seconds)
                  + Decimal(delta.microseconds) / Decimal(1_000_000))
    return {
        "cycle_id": doc["_id"],
        "tickers": doc["tickers"],
        "runs": runs,
        # nullif(count(*), 0): a zero-run group cannot survive the HAVING, but
        # the SQL guarded it and so does this.
        "pct_over": _round1(Decimal(100 * doc["over"]) / Decimal(runs)) if runs else None,
        "median_s": _round1(Decimal(str(_percentile_cont(ordered, 0.5))) / 1000),
        "p90_s": _round1(Decimal(str(_percentile_cont(ordered, 0.9))) / 1000),
        "max_s": _round1(Decimal(ordered[-1]) / 1000) if ordered else None,
        "started": started,
        "ended": ended,
        "span_min": _round1(span_s / 60),
    }


def fetch(cycle=None, min_tickers=6, days=None, limit=40, cycles=None):
    if limit is not None and limit < 0:
        raise ValueError("--limit must not be negative")
    if limit == 0:
        return []  # SQL's LIMIT 0 returns no rows; $limit: 0 is an error.
    docs = mongo_store.aggregate(
        TABLE,  # the TABLE name -- mongo_store resolves the collection, once.
        pipeline(cycle=cycle, min_tickers=min_tickers, days=days,
                 limit=limit, cycles=cycles),
    )
    return [_row(d) for d in docs]


def _num(v):
    return float(v) if v is not None else None


def to_json(rows):
    return [
        {
            "cycle_id": r["cycle_id"],
            "tickers": r["tickers"],
            "runs": r["runs"],
            "pct_over_300s": _num(r["pct_over"]),
            "median_s": _num(r["median_s"]),
            "p90_s": _num(r["p90_s"]),
            "max_s": _num(r["max_s"]),
            "span_min": _num(r["span_min"]),
            "started": r["started"].isoformat() if r["started"] else None,
            "ended": r["ended"].isoformat() if r["ended"] else None,
        }
        for r in rows
    ]


def render(rows):
    if not rows:
        print("no cycles matched (try --min-tickers 1, or widen --days)")
        return
    print(
        f"{'cycle_id':<26} {'tk':>3} {'runs':>5} {'>300s':>7} "
        f"{'med_s':>7} {'p90_s':>7} {'max_s':>8} {'span_m':>7}  started"
    )
    print("-" * 100)
    for r in rows:
        started = r["started"].strftime("%m-%d %H:%M") if r["started"] else "?"
        print(
            f"{r['cycle_id']:<26} {r['tickers']:>3} {r['runs']:>5} "
            f"{_num(r['pct_over']):>6.1f}% {_num(r['median_s']):>7.1f} "
            f"{_num(r['p90_s']):>7.1f} {_num(r['max_s']):>8.1f} "
            f"{_num(r['span_min']):>7.1f}  {started}"
        )
    print()
    print(f"'>300s' is the share of agent runs past prism's {WATCHDOG_MS // 1000}s watchdog.")
    print("Compare only rows of similar width — chapter 34: narrow cycles score 0% regardless.")


def self_test() -> int:
    """Reproduce chapter 34's published band, or refuse to be trusted."""
    rows = {r["cycle_id"]: r for r in fetch(cycles=CONTROL_CYCLES, min_tickers=1, limit=50)}
    lo, hi = CONTROL_BAND
    failures = []

    print(f"positive control: chapter 34's six pre-fix wide cycles, expected {lo}-{hi}%\n")
    for cid in CONTROL_CYCLES:
        r = rows.get(cid)
        if r is None:
            failures.append(f"{cid}: MISSING from {TABLE}")
            print(f"  {cid:<26} MISSING")
            continue
        pct = _num(r["pct_over"])
        ok = lo <= pct <= hi and r["tickers"] >= 6
        if not ok:
            failures.append(f"{cid}: {pct}% over 300s, {r['tickers']} tickers")
        print(
            f"  {cid:<26} {r['tickers']:>2} tickers  {pct:>5.1f}%  "
            f"{'ok' if ok else 'OUT OF BAND'}"
        )

    print()
    if failures:
        print("SELF-TEST FAILED — this tool does not reproduce the published numbers:")
        for f in failures:
            print(f"  {f}")
        return 1
    print("SELF-TEST PASSED — the query reproduces chapter 34's band.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cycle", help="a single cycle id")
    ap.add_argument("--min-tickers", type=int, default=6,
                    help="width floor; default 6 (wide cycles only)")
    ap.add_argument("--days", type=int, help="only cycles from the last N days")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="reproduce chapter 34's published band and exit")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    min_tickers = 1 if args.cycle else args.min_tickers
    rows = fetch(cycle=args.cycle, min_tickers=min_tickers, days=args.days, limit=args.limit)

    if args.json:
        print(json.dumps(to_json(rows), indent=2))
    else:
        render(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
