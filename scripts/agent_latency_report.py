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
import os
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is required: use the repo venv (.venv/bin/python)")

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot"
)

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

QUERY = """
SELECT t.cycle_id,
       count(DISTINCT t.ticker)                                            AS tickers,
       count(*)                                                            AS runs,
       round(100.0 * count(*) FILTER (WHERE t.elapsed_ms > %(watchdog)s)
             / nullif(count(*), 0), 1)                                     AS pct_over,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY t.elapsed_ms)::numeric / 1000, 1) AS median_s,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY t.elapsed_ms)::numeric / 1000, 1) AS p90_s,
       round(max(t.elapsed_ms)::numeric / 1000, 1)                         AS max_s,
       min(t.created_at)                                                   AS started,
       max(t.created_at)                                                   AS ended,
       round(EXTRACT(EPOCH FROM (max(t.created_at) - min(t.created_at)))::numeric / 60, 1) AS span_min
FROM v3_agent_telemetry t
WHERE t.elapsed_ms IS NOT NULL
  {where}
GROUP BY t.cycle_id
HAVING count(DISTINCT t.ticker) >= %(min_tickers)s
ORDER BY min(t.created_at) DESC
LIMIT %(limit)s
"""


def fetch(cycle=None, min_tickers=6, days=None, limit=40, cycles=None):
    where = ""
    params = {"watchdog": WATCHDOG_MS, "min_tickers": min_tickers, "limit": limit}
    if cycle:
        where += " AND t.cycle_id = %(cycle)s"
        params["cycle"] = cycle
    if cycles:
        where += " AND t.cycle_id = ANY(%(cycles)s)"
        params["cycles"] = list(cycles)
    if days:
        where += " AND t.created_at > NOW() - make_interval(days => %(days)s)"
        params["days"] = days

    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(QUERY.format(where=where), params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


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
            failures.append(f"{cid}: MISSING from v3_agent_telemetry")
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
