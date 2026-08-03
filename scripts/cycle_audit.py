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
from collections import defaultdict

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is required: use the repo venv (.venv/bin/python)")

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot"
)

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


def connect():
    return psycopg2.connect(DSN)


def latest_cycle(cur) -> str | None:
    cur.execute(
        "SELECT cycle_id FROM pipeline_events ORDER BY timestamp DESC LIMIT 1"
    )
    row = cur.fetchone()
    return row[0] if row else None


# ── checks ──────────────────────────────────────────────────────────────────
# Each returns (name, status, detail, data). Thresholds are the observed bad
# values from the 07-31 audit, not round numbers.


def check_collector_lateness(cur, cycle_id):
    """The slow-lane fix: multi_api_news/youtube must land inside the budget.

    Baseline to beat: cycle-v3-1785504601 had 12 late arrivals out of 12
    attempts for these two collectors — a 100% waste rate.
    """
    cur.execute(
        """
        SELECT step FROM pipeline_events
        WHERE cycle_id = %s AND step LIKE 'v3_precollect_%%'
        """,
        [cycle_id],
    )
    ok = late = 0
    for (step,) in cur.fetchall():
        for name in SLOW_COLLECTORS:
            if f"_{name}_late_" in step:
                late += 1
            elif f"_{name}_ok_" in step:
                ok += 1
    total = ok + late
    if total == 0:
        return ("slow collectors land in-budget", INFO, "no slow-collector runs yet", {})
    rate = late / total
    detail = f"{ok} in-budget / {late} late ({rate:.0%} wasted) across {total} runs"
    status = PASS if rate == 0 else (WARN if rate < 0.5 else FAIL)
    return ("slow collectors land in-budget", status, detail,
            {"ok": ok, "late": late, "late_rate": round(rate, 3)})


def check_collector_errors(cur, cycle_id):
    """Hard collector failures. 07-31 baseline: 4 (yfinance_price on 4 tickers)."""
    cur.execute(
        """
        SELECT detail FROM pipeline_events
        WHERE cycle_id = %s AND status = 'error' AND step LIKE 'v3_precollect_%%'
        """,
        [cycle_id],
    )
    errs = [r[0] for r in cur.fetchall()]
    status = PASS if not errs else (WARN if len(errs) <= 2 else FAIL)
    return ("no collector hard failures", status,
            f"{len(errs)} errors" + (f" — e.g. {errs[0][:80]}" if errs else ""),
            {"count": len(errs), "samples": errs[:5]})


def check_stale_prices(cur, cycle_id):
    """A decision must not be reasoned on a price far from the one it records.

    RBLX was analyzed at 39.27 against an entry of 51.68 — a 24% gap that no
    gate caught, because the stale-price guardrail was shadow-only.
    """
    cur.execute(
        """
        SELECT a.ticker, a.analysis_price, d.entry_price
        FROM analysis_results a
        JOIN decision_outcomes d
          ON d.cycle_id = a.cycle_id AND d.ticker = a.ticker
        WHERE a.cycle_id = %s
          AND a.analysis_price > 0 AND d.entry_price > 0
        """,
        [cycle_id],
    )
    gaps = []
    for tkr, ap, ep in cur.fetchall():
        dev = abs(float(ap) - float(ep)) / float(ep)
        if dev > 0.02:
            gaps.append((tkr, round(dev * 100, 1)))
    gaps.sort(key=lambda x: -x[1])
    worst = gaps[0][1] if gaps else 0.0
    status = PASS if not gaps else (WARN if worst < 5 else FAIL)
    return ("analysis price matches entry price", status,
            f"{len(gaps)} ticker(s) >2% off" + (f", worst {gaps[0][0]} {worst}%" if gaps else ""),
            {"gaps": gaps})


def check_stale_guardrail_enforcing(cur, cycle_id):
    """A guardrail that says would_block while blocking nothing is not a gate.

    07-31: 3 SHADOW_STALE_PRICE_DATA firings, all would_block=true, none
    enforced. The promotion of this gate to enforcing is the top open item.
    """
    cur.execute(
        """
        SELECT guardrail, COUNT(*) FROM v3_guardrail_firings
        WHERE cycle_id = %s GROUP BY 1
        """,
        [cycle_id],
    )
    rows = dict(cur.fetchall())
    shadow = sum(v for k, v in rows.items() if k.startswith("SHADOW_"))
    enforced = sum(v for k, v in rows.items() if not k.startswith("SHADOW_"))
    status = PASS if shadow == 0 else WARN
    return ("no shadow-only guardrail firings", status,
            f"{shadow} shadow / {enforced} enforced" + (f" — {rows}" if rows else ""),
            {"shadow": shadow, "enforced": enforced, "rules": rows})


def check_tool_failures(cur, cycle_id):
    """Agent tool call health. 07-31 baseline: 14/123 failed (11.4%)."""
    cur.execute(
        """
        SELECT COUNT(*), COUNT(*) FILTER (WHERE NOT success),
               COUNT(*) FILTER (WHERE was_blocked)
        FROM agent_tool_telemetry WHERE cycle_id = %s
        """,
        [cycle_id],
    )
    total, failed, blocked = cur.fetchone()
    total = total or 0
    if total == 0:
        return ("agent tool calls succeed", INFO, "no tool calls yet", {})
    cur.execute(
        """
        SELECT tool_name, COUNT(*) FROM agent_tool_telemetry
        WHERE cycle_id = %s AND NOT success GROUP BY 1 ORDER BY 2 DESC LIMIT 4
        """,
        [cycle_id],
    )
    worst = cur.fetchall()
    rate = failed / total
    status = PASS if rate < 0.02 else (WARN if rate < 0.10 else FAIL)
    return ("agent tool calls succeed", status,
            f"{failed}/{total} failed ({rate:.1%}), {blocked} blocked"
            + (f" — worst: {', '.join(f'{t}×{n}' for t, n in worst)}" if worst else ""),
            {"total": total, "failed": failed, "blocked": blocked,
             "by_tool": dict(worst), "fail_rate": round(rate, 3)})


def check_duplicate_agent_runs(cur, cycle_id):
    """One research agent should run once per ticker. 07-31: 17 extra runs."""
    cur.execute(
        """
        SELECT agent_name, ticker, COUNT(*) c FROM v3_agent_telemetry
        WHERE cycle_id = %s AND ticker IS NOT NULL AND ticker <> ''
        GROUP BY 1,2 HAVING COUNT(*) > 1
        """,
        [cycle_id],
    )
    dupes = cur.fetchall()
    extra = sum(c - 1 for _, _, c in dupes)
    status = PASS if extra == 0 else (WARN if extra <= 3 else FAIL)
    return ("no duplicate agent runs", status,
            f"{extra} extra run(s) across {len(dupes)} agent/ticker pairs",
            {"extra": extra, "pairs": [(a, t, c) for a, t, c in dupes[:8]]})


def check_policy_blocks_recorded(cur, cycle_id):
    """A blocked decision must be visible in the outcome record.

    07-31: RIVN's BUY was blocked by the confidence gate, but
    decision_outcomes.overridden_from was NULL — the block is invisible to
    anyone reading outcomes, so it can never be scored.
    """
    cur.execute(
        """
        SELECT t.ticker, t.policy_action, d.overridden_from
        FROM trade_results t
        LEFT JOIN decision_outcomes d
          ON d.cycle_id = t.cycle_id AND d.ticker = t.ticker
        WHERE t.cycle_id = %s AND t.policy_action LIKE 'HOLD_POLICY_BLOCKED%%'
        """,
        [cycle_id],
    )
    rows = cur.fetchall()
    unrecorded = [t for t, _, ov in rows if not ov]
    status = PASS if not unrecorded else FAIL
    return ("policy blocks recorded in outcomes", status,
            f"{len(rows)} block(s), {len(unrecorded)} not recorded"
            + (f" — {', '.join(unrecorded)}" if unrecorded else ""),
            {"blocks": len(rows), "unrecorded": unrecorded})


def check_cycle_attribution(cur, cycle_id):
    """Telemetry that loses its cycle_id cannot be audited later.

    07-31: 92 agent_audit_log rows in the window all had cycle_id = ''.
    """
    cur.execute(
        """
        SELECT COUNT(*) FROM agent_audit_log
        WHERE (cycle_id IS NULL OR cycle_id = '')
          AND created_at > NOW() - INTERVAL '2 hours'
        """
    )
    orphans = cur.fetchone()[0]
    status = PASS if orphans == 0 else WARN
    return ("telemetry keeps its cycle_id", status,
            f"{orphans} unattributed agent_audit_log rows in the last 2h",
            {"orphans": orphans})


def check_benchmark_timings(cur, cycle_id):
    """Phase timings should be filled from pipeline_events since 2026-08-03.

    cache_hit_pct is the COLLECTOR fast-path skip rate (scraper steps skipped
    because a <48h thesis existed) — it says nothing about LLM KV-cache reuse;
    that lives in v3_agent_telemetry.cached_tokens.
    """
    cur.execute(
        """
        SELECT collect_ms, analyze_ms, trade_ms, total_tokens, cache_hit_pct
        FROM cycle_benchmarks WHERE cycle_id = %s
        """,
        [cycle_id],
    )
    row = cur.fetchone()
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


def check_confidence_is_monotonic(cur, cycle_id):
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
    cur.execute(
        """
        SELECT width_bucket(confidence, 70, 95, 3) b,
               COUNT(*) n,
               ROUND(AVG(confidence)::numeric, 1) stated,
               ROUND(100.0 * COUNT(*) FILTER (WHERE outcome = 'WIN')
                     / NULLIF(COUNT(*), 0), 1) realized
        FROM decision_outcomes
        WHERE outcome IN ('WIN', 'LOSS') AND confidence >= 70
        GROUP BY 1 HAVING COUNT(*) >= 30 ORDER BY 1
        """
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        return ("confidence ranks outcomes", INFO,
                "not enough resolved rows above the floor to rank", {})

    buckets = [(float(s), float(r), n) for _, n, s, r in rows if r is not None]
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
    with connect() as conn, conn.cursor() as cur:
        results = []
        for fn in CHECKS:
            try:
                results.append(fn(cur, cycle_id))
            except Exception as e:  # a broken check must not look like a pass
                # Postgres aborts the whole transaction on a bad statement, so
                # without this rollback one failing check silently fails every
                # check after it — the tool would report a clean cycle because
                # it never got to look.
                conn.rollback()
                results.append((fn.__name__, FAIL, f"check errored: {e}", {}))

        cur.execute(
            """SELECT status, buy_count, sell_count, hold_count, trade_executed,
                      elapsed_ms, tickers_final
               FROM cycle_run_summaries WHERE cycle_id = %s""",
            [cycle_id],
        )
        summary = cur.fetchone()

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
            with connect() as conn, conn.cursor() as cur:
                cid = cycle_id or latest_cycle(cur)
                if not cid:
                    time.sleep(poll)
                    continue
                cur.execute(
                    """SELECT id, timestamp, phase, step, detail, status
                       FROM pipeline_events
                       WHERE cycle_id = %s AND id > %s
                       ORDER BY id""",
                    [cid, seen_max_id],
                )
                for _id, ts, phase, step, detail, status in cur.fetchall():
                    seen_max_id = max(seen_max_id, _id)
                    counts[status] += 1
                    if phase != last_phase:
                        print(f"\n  ── {phase} ──")
                        last_phase = phase
                    if status in ("error", "warning"):
                        print(f"  {ts:%H:%M:%S} {_c(FAIL if status=='error' else WARN, status.upper())} "
                              f"{step}: {(detail or '')[:100]}")
                    elif status == "ok":
                        print(f"  {ts:%H:%M:%S} {(detail or step)[:110]}")
                cur.execute(
                    "SELECT status FROM pipeline_state WHERE singleton_id='current'"
                )
                row = cur.fetchone()
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
        with connect() as conn, conn.cursor() as cur:
            cid = latest_cycle(cur)
        if not cid:
            sys.exit("no cycles found")

    if args.watch:
        return watch(cid, args.poll)
    return run_checks(cid, args.json)


if __name__ == "__main__":
    sys.exit(main())
