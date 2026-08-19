#!/usr/bin/env python3
"""Watch a V3 cycle as it runs, then grade it against the defects we know about.

Two modes over one shared set of queries:

  --watch    live tail: phase transitions, errors and warnings as they land,
             plus a running precollect scoreboard.
  --check    post-cycle invariants. Exit 1 if any FAIL, so it can gate a
             deploy or run from cron.

Every check here exists because a real cycle failed it. The audit of
cycle-v3-1785504601 (2026-07-31) found: 3 of 6 tickers analyzed on prices up
to 10 trading days stale while the guardrail watched in shadow mode, all 12
news/video collectors timing out and landing late, 11 of 29 screener calls
failing on schema drift, 17 duplicate analyst runs, and 32 of 58 quality
flags firing on a regex false positive. A cycle that passes every check
below would not have had any of those go unnoticed.

The point is not a health score. It is that each number here has a known
bad value that was actually observed, so "looks fine" has to be earned.

READS MONGO. This is one of the instruments the cutover is verified WITH, so
it cannot be the last thing still reading Postgres: a Mongo-only cycle graded
by a Postgres reader grades a store nothing writes any more, and every check
would come back clean because every table would be empty.

Usage:
  scripts/cycle_audit.py --watch                 # follow the running cycle
  scripts/cycle_audit.py --check                 # grade the latest cycle
  scripts/cycle_audit.py --check --cycle <id>    # grade a specific cycle
  scripts/cycle_audit.py --check --json          # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_query, mongo_store  # noqa: E402

# Collectors granted the slow lane in app/v3/data_report.py. If these still
# land late, the extended budget is not big enough (or is not deployed).
SLOW_COLLECTORS = ("multi_api_news", "youtube")

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
_COLOR = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", INFO: "\033[36m"}
_RESET = "\033[0m"


def _c(status: str, text: str | None = None) -> str:
    if not sys.stdout.isatty():
        return text or status
    return f"{_COLOR.get(status, '')}{text or status}{_RESET}"


def _prefix(value: str) -> dict:
    """`LIKE 'value%'` — anchored, with the value escaped.

    Anchored because an unanchored regex is a `LIKE '%value%'`, which is a
    different query: `policy_action LIKE 'HOLD_POLICY_BLOCKED%'` would start
    matching anything that merely CONTAINS the phrase.
    """
    import re as _re

    return {"$regex": f"^{_re.escape(value)}"}


def latest_cycle() -> str | None:
    row = mongo_query.find_row(
        "pipeline_events", {}, ["cycle_id"], sort=[("timestamp", -1)])
    return row[0] if row else None


# ── checks ──────────────────────────────────────────────────────────────────
# Each returns (name, status, detail, data). Thresholds are the observed bad
# values from the 07-31 audit, not round numbers.


def check_collector_lateness(cycle_id):
    """The slow-lane fix: multi_api_news/youtube must land inside the budget.

    Baseline to beat: cycle-v3-1785504601 had 12 late arrivals out of 12
    attempts for these two collectors — a 100% waste rate.
    """
    steps = mongo_query.find_rows(
        "pipeline_events",
        {"cycle_id": cycle_id, "step": _prefix("v3_precollect_")},
        ["step"])
    ok = late = 0
    for (step,) in steps:
        for name in SLOW_COLLECTORS:
            if f"_{name}_late_" in (step or ""):
                late += 1
            elif f"_{name}_ok_" in (step or ""):
                ok += 1
    total = ok + late
    if total == 0:
        return ("slow collectors land in-budget", INFO, "no slow-collector runs yet", {})
    rate = late / total
    detail = f"{ok} in-budget / {late} late ({rate:.0%} wasted) across {total} runs"
    status = PASS if rate == 0 else (WARN if rate < 0.5 else FAIL)
    return ("slow collectors land in-budget", status, detail,
            {"ok": ok, "late": late, "late_rate": round(rate, 3)})


def check_collector_errors(cycle_id):
    """Hard collector failures. 07-31 baseline: 4 (yfinance_price on 4 tickers)."""
    errs = [r[0] for r in mongo_query.find_rows(
        "pipeline_events",
        {"cycle_id": cycle_id, "status": "error", "step": _prefix("v3_precollect_")},
        ["detail"])]
    status = PASS if not errs else (WARN if len(errs) <= 2 else FAIL)
    return ("no collector hard failures", status,
            f"{len(errs)} errors" + (f" — e.g. {str(errs[0])[:80]}" if errs else ""),
            {"count": len(errs), "samples": errs[:5]})


def check_stale_prices(cycle_id):
    """A decision must not be reasoned on a price far from the one it records.

    RBLX was analyzed at 39.27 against an entry of 51.68 — a 24% gap that no
    gate caught, because the stale-price guardrail was shadow-only.
    """
    # The SQL joined on (cycle_id, ticker). cycle_id is pinned on BOTH sides by
    # the filters, so the remaining equality is the ticker — which is what
    # join_rows expresses. Pinning it on both sides is not optional: joining on
    # the ticker alone would pair this cycle's analysis with another cycle's
    # outcome and grade a price gap that never happened.
    rows = mongo_query.join_rows(
        "analysis_results",
        {"cycle_id": cycle_id, "analysis_price": {"$gt": 0}}, "ticker",
        "decision_outcomes", "ticker",
        {"cycle_id": cycle_id, "entry_price": {"$gt": 0}},
        left_fields=["ticker", "analysis_price"], right_fields=["entry_price"],
        select=[("l", "ticker"), ("l", "analysis_price"), ("r", "entry_price")],
    )
    gaps = []
    for tkr, ap, ep in rows:
        dev = abs(float(ap) - float(ep)) / float(ep)
        if dev > 0.02:
            gaps.append((tkr, round(dev * 100, 1)))
    gaps.sort(key=lambda x: -x[1])
    worst = gaps[0][1] if gaps else 0.0
    status = PASS if not gaps else (WARN if worst < 5 else FAIL)
    return ("analysis price matches entry price", status,
            f"{len(gaps)} ticker(s) >2% off" + (f", worst {gaps[0][0]} {worst}%" if gaps else ""),
            {"gaps": gaps})


def check_stale_guardrail_enforcing(cycle_id):
    """A guardrail that says would_block while blocking nothing is not a gate.

    07-31: 3 SHADOW_STALE_PRICE_DATA firings, all would_block=true, none
    enforced. The promotion of this gate to enforcing is the top open item.
    """
    rows = dict(Counter(
        r[0] for r in mongo_query.find_rows(
            "v3_guardrail_firings", {"cycle_id": cycle_id}, ["guardrail"])))
    shadow = sum(v for k, v in rows.items() if (k or "").startswith("SHADOW_"))
    enforced = sum(v for k, v in rows.items() if not (k or "").startswith("SHADOW_"))
    status = PASS if shadow == 0 else WARN
    return ("no shadow-only guardrail firings", status,
            f"{shadow} shadow / {enforced} enforced" + (f" — {rows}" if rows else ""),
            {"shadow": shadow, "enforced": enforced, "rules": rows})


def check_tool_failures(cycle_id):
    """Agent tool call health. 07-31 baseline: 14/123 failed (11.4%)."""
    calls = mongo_query.find_rows(
        "agent_tool_telemetry", {"cycle_id": cycle_id},
        ["tool_name", "success", "was_blocked"])
    total = len(calls)
    if total == 0:
        return ("agent tool calls succeed", INFO, "no tool calls yet", {})
    # `NOT success` in SQL is false OR... nothing: the column is NOT NULL. A
    # document missing the field reads as None here, which is not a success
    # either, so `not s` is the same set.
    failed = sum(1 for _, s, _ in calls if not s)
    blocked = sum(1 for _, _, b in calls if b)
    worst = Counter(t for t, s, _ in calls if not s).most_common(4)
    rate = failed / total
    status = PASS if rate < 0.02 else (WARN if rate < 0.10 else FAIL)
    return ("agent tool calls succeed", status,
            f"{failed}/{total} failed ({rate:.1%}), {blocked} blocked"
            + (f" — worst: {', '.join(f'{t}×{n}' for t, n in worst)}" if worst else ""),
            {"total": total, "failed": failed, "blocked": blocked,
             "by_tool": dict(worst), "fail_rate": round(rate, 3)})


def check_duplicate_agent_runs(cycle_id):
    """One research agent should run once per ticker. 07-31: 17 extra runs."""
    pairs = Counter(
        (a, t) for a, t in mongo_query.find_rows(
            "v3_agent_telemetry",
            {"cycle_id": cycle_id, "ticker": {"$nin": [None, ""]}},
            ["agent_name", "ticker"])
    )
    dupes = [(a, t, c) for (a, t), c in pairs.items() if c > 1]
    extra = sum(c - 1 for _, _, c in dupes)
    status = PASS if extra == 0 else (WARN if extra <= 3 else FAIL)
    return ("no duplicate agent runs", status,
            f"{extra} extra run(s) across {len(dupes)} agent/ticker pairs",
            {"extra": extra, "pairs": dupes[:8]})


def check_policy_blocks_recorded(cycle_id):
    """A blocked decision must be visible in the outcome record.

    07-31: RIVN's BUY was blocked by the confidence gate, but
    decision_outcomes.overridden_from was NULL — the block is invisible to
    anyone reading outcomes, so it can never be scored.
    """
    # A LEFT JOIN, and the outer half is the whole check: a blocked trade with
    # NO outcome row at all is the worst version of "not recorded", and an
    # inner join would drop exactly those and report a clean cycle.
    rows = mongo_query.left_join_rows(
        "trade_results",
        {"cycle_id": cycle_id, "policy_action": _prefix("HOLD_POLICY_BLOCKED")},
        "ticker",
        "decision_outcomes", "ticker", {"cycle_id": cycle_id},
        left_fields=["ticker", "policy_action"], right_fields=["overridden_from"],
        select=[("l", "ticker"), ("l", "policy_action"), ("r", "overridden_from")],
    )
    unrecorded = [t for t, _, ov in rows if not ov]
    status = PASS if not unrecorded else FAIL
    return ("policy blocks recorded in outcomes", status,
            f"{len(rows)} block(s), {len(unrecorded)} not recorded"
            + (f" — {', '.join(unrecorded)}" if unrecorded else ""),
            {"blocks": len(rows), "unrecorded": unrecorded})


def check_cycle_attribution(cycle_id):
    """Telemetry that loses its cycle_id cannot be audited later.

    07-31: 92 agent_audit_log rows in the window all had cycle_id = ''.
    """
    # `cycle_id IS NULL OR cycle_id = ''` — in Mongo `{"$in": [None, ""]}` also
    # matches documents with no such field, which is the same "unattributed"
    # the SQL meant.
    orphans = mongo_store.count_docs("agent_audit_log", {
        "cycle_id": {"$in": [None, ""]},
        "created_at": {"$gt": datetime.now(timezone.utc) - timedelta(hours=2)},
    })
    status = PASS if orphans == 0 else WARN
    return ("telemetry keeps its cycle_id", status,
            f"{orphans} unattributed agent_audit_log rows in the last 2h",
            {"orphans": orphans})


def check_benchmark_timings(cycle_id):
    """Phase timings should be filled from pipeline_events since 2026-08-03.

    cache_hit_pct is the COLLECTOR fast-path skip rate (scraper steps skipped
    because a <48h thesis existed) — it says nothing about LLM KV-cache reuse;
    that lives in v3_agent_telemetry.cached_tokens.
    """
    row = mongo_query.find_row(
        "cycle_benchmarks", {"cycle_id": cycle_id},
        ["collect_ms", "analyze_ms", "trade_ms", "total_tokens", "cache_hit_pct"])
    if not row:
        return ("phase timings recorded", INFO, "no benchmark row yet", {})
    collect, analyze, trade, tokens, cache = row
    missing = [n for n, v in
               (("collect_ms", collect), ("analyze_ms", analyze), ("trade_ms", trade))
               if v is None]
    status = PASS if not missing else WARN
    return ("phase timings recorded", status,
            f"missing: {', '.join(missing) or 'none'}"
            + f" | tokens={tokens or 0:,} collector_skip={cache or 0}%",
            {"missing": missing, "tokens": tokens, "collector_skip_pct": cache})


def check_confidence_is_monotonic(cycle_id):
    """Higher stated confidence must win more often, or it is not confidence.

    This is a COHORT check, not a per-cycle one — it reads the whole resolved
    record, so it moves slowly and the same verdict will repeat across cycles.
    It is here because it is the one quality question the current sample size
    can actually answer: `power_report.py` puts the detectable effect on mean
    P&L at ~8.84pp, far above any edge we could show, but a monotonicity
    violation in the ranking needs far less data than a difference in means.

    Measured 2026-07-31: 70-78 wins 62.7%, 80-89 wins 67.4%, and 90-95 wins
    58.9% — the desk's most confident bucket is worse than its least confident
    one above the floor. Average P&L still rises with confidence (2.92 → 2.72
    → 4.57), so confidence is tracking magnitude while wearing a probability
    label. Anything sizing positions off it is reading the wrong axis.
    """
    rows = mongo_query.find_rows(
        "decision_outcomes",
        {"outcome": {"$in": ["WIN", "LOSS"]}, "confidence": {"$gte": 70}},
        ["confidence", "outcome"])

    # `width_bucket(confidence, 70, 95, 3)`: three equal bands across [70, 95),
    # and everything at or above 95 lands in a fourth. Reproduced exactly —
    # collapsing the overflow band into the top one would merge the desk's most
    # confident decisions into the band this check is comparing them against.
    def bucket(c: float) -> int:
        if c >= 95:
            return 4
        return int((float(c) - 70) / (25 / 3)) + 1

    grouped: dict[int, list] = defaultdict(list)
    for conf, outcome in rows:
        if conf is None:
            continue
        grouped[bucket(float(conf))].append((float(conf), outcome))

    ordered = [(b, v) for b, v in sorted(grouped.items()) if len(v) >= 30]
    if len(ordered) < 2:
        return ("confidence ranks outcomes", INFO,
                "not enough resolved rows above the floor to rank", {})

    buckets = []
    for _, vals in ordered:
        n = len(vals)
        stated = round(sum(c for c, _ in vals) / n, 1)
        realized = round(100.0 * sum(1 for _, o in vals if o == "WIN") / n, 1)
        buckets.append((stated, realized, n))

    inversions = [
        (buckets[i][0], buckets[i][1], buckets[i + 1][0], buckets[i + 1][1])
        for i in range(len(buckets) - 1)
        if buckets[i + 1][1] < buckets[i][1]
    ]
    worst_gap = max((s - r for s, r, _ in buckets), default=0.0)
    detail = " | ".join(f"conf~{s:.0f}: {r:.0f}% won (n={n})" for s, r, n in buckets)
    if inversions:
        s0, r0, s1, r1 = inversions[0]
        detail = (f"conf~{s1:.0f} wins {r1:.0f}% vs conf~{s0:.0f} at {r0:.0f}% "
                  f"— higher confidence, worse outcome. {detail}")
    status = FAIL if inversions else (WARN if worst_gap > 15 else PASS)
    return ("confidence ranks outcomes", status, detail,
            {"buckets": buckets, "inversions": len(inversions),
             "worst_stated_minus_realized": round(worst_gap, 1)})


CHECKS = [
    check_collector_lateness,
    check_collector_errors,
    check_stale_prices,
    check_stale_guardrail_enforcing,
    check_tool_failures,
    check_duplicate_agent_runs,
    check_policy_blocks_recorded,
    check_cycle_attribution,
    check_benchmark_timings,
    check_confidence_is_monotonic,
]


def run_checks(cycle_id: str, as_json: bool = False) -> int:
    results = []
    for fn in CHECKS:
        try:
            results.append(fn(cycle_id))
        except Exception as e:  # a broken check must not look like a pass
            results.append((fn.__name__, FAIL, f"check errored: {e}", {}))

    summary = mongo_query.find_row(
        "cycle_run_summaries", {"cycle_id": cycle_id},
        ["status", "buy_count", "sell_count", "hold_count", "trade_executed",
         "elapsed_ms", "tickers_final"])

    if as_json:
        print(json.dumps({
            "cycle_id": cycle_id,
            "checks": [{"name": n, "status": s, "detail": d, "data": x}
                       for n, s, d, x in results],
            "failed": sum(1 for _, s, _, _ in results if s == FAIL),
        }, indent=2, default=str))
    else:
        print(f"\n  Cycle audit — {cycle_id}")
        if summary:
            st, b, s_, h, tx, ms, tf = summary
            # tickers_final arrives as a list from a jsonb column and as a
            # string from a text one, depending on which migration created it.
            tickers = tf if isinstance(tf, list) else (json.loads(tf) if tf else [])
            print(f"  {st} · {len(tickers)} tickers · {b}B/{s_}S/{h}H · "
                  f"{tx or 0} executed · {(ms or 0)/60000:.1f} min")
        print()
        width = max(len(n) for n, _, _, _ in results)
        for name, status, detail, _ in results:
            print(f"  {_c(status, f'{status:<4}')}  {name:<{width}}  {detail}")
        n_fail = sum(1 for _, s, _, _ in results if s == FAIL)
        n_warn = sum(1 for _, s, _, _ in results if s == WARN)
        print(f"\n  {n_fail} failed, {n_warn} warned, "
              f"{sum(1 for _, s, _, _ in results if s == PASS)} passed\n")

    return 1 if any(s == FAIL for _, s, _, _ in results) else 0


def watch(cycle_id: str | None, poll: float = 5.0) -> int:
    """Tail pipeline_events, printing each new row once."""
    seen_max_id = 0
    last_phase = None
    counts: dict[str, int] = defaultdict(int)
    print(f"  watching {cycle_id or '(latest)'} — ctrl-c to stop\n")
    try:
        while True:
            cid = cycle_id or latest_cycle()
            if not cid:
                time.sleep(poll)
                continue
            events = mongo_query.find_rows(
                "pipeline_events",
                {"cycle_id": cid, "id": {"$gt": seen_max_id}},
                ["id", "timestamp", "phase", "step", "detail", "status"],
                sort=[("id", 1)])
            for _id, ts, phase, step, detail, status in events:
                seen_max_id = max(seen_max_id, _id or 0)
                counts[status] += 1
                if phase != last_phase:
                    print(f"\n  ── {phase} ──")
                    last_phase = phase
                if status in ("error", "warning"):
                    print(f"  {ts:%H:%M:%S} {_c(FAIL if status=='error' else WARN, status.upper())} "
                          f"{step}: {(detail or '')[:100]}")
                elif status == "ok":
                    print(f"  {ts:%H:%M:%S} {(detail or step)[:110]}")
            row = mongo_query.find_row(
                "pipeline_state", {"singleton_id": "current"}, ["status"])
            if row and row[0] in ("done", "error", "stopped"):
                print(f"\n  cycle {row[0]} — "
                      f"{counts['error']} errors, {counts['warning']} warnings\n")
                return run_checks(cid)
            time.sleep(poll)
    except KeyboardInterrupt:
        print("\n  stopped watching\n")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", action="store_true", help="follow a running cycle")
    ap.add_argument("--check", action="store_true", help="grade a finished cycle")
    ap.add_argument("--cycle", help="cycle id (default: most recent)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--poll", type=float, default=5.0)
    args = ap.parse_args()

    cid = args.cycle
    if not cid and not args.watch:
        cid = latest_cycle()
        if not cid:
            sys.exit("no cycles found")

    if args.watch:
        return watch(cid, args.poll)
    return run_checks(cid, args.json)


if __name__ == "__main__":
    sys.exit(main())
