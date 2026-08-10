#!/usr/bin/env python3
"""Reconstruct how many requests were in flight on a vLLM box at once.

vLLM's own `Running:`/`Waiting:` log line is the direct measurement, but it
only exists in the box's live logs — there is no history. The prism request
ledger does have history, so concurrency can be recovered after the fact by
treating each row as the interval [createdAt - totalTime, createdAt] and
sweeping for maximum overlap. Chapter 36 cross-validated the two against each
other: the sweep said 16 where vLLM's log said `Running: 6, Waiting: 9`.

Two numbers come out, and they answer different questions:

  peak overlap   the worst instant. What a concurrency cap has to survive.
  offered load   sum(duration) / span — the *sustained* demand, in
                 concurrent-equivalents. This is the one that decides whether
                 a cap queues or sheds: a cap below offered load is not a
                 queue that drains, it is a backlog that grows until the
                 queue timeout turns it into 503s.

Two ledger traps, both load-bearing:
  - `createdAt` is a **string**, not a date. Range queries work only because
    ISO-8601 sorts lexically, and only when both bounds are the same shape.
  - `totalTime` is in **seconds**, not milliseconds.

  --self-test replays chapter 36's 9-ticker window and fails unless the sweep
  returns the peak that chapter published.

Usage:
  scripts/inflight_peak.py --cycle cycle-v3-1786297004
  scripts/inflight_peak.py --from 2026-08-09T18:05 --to 2026-08-09T19:55
  scripts/inflight_peak.py --cycle <id> --provider vllm-1
  scripts/inflight_peak.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from pymongo import MongoClient
except ImportError:
    sys.exit("pymongo is required: use the repo venv (.venv/bin/python)")

try:
    import psycopg2
except ImportError:
    psycopg2 = None

MONGO_URI = os.environ.get("PRISM_MONGO_URI")
DSN = os.environ.get(
    "DATABASE_URL", "postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot"
)

# vllm-2 is Gold Spark; see vault-service/projects.json PROVIDER_VLLM_2_URL,
# which points at the shim rather than the box directly.
DEFAULT_PROVIDER = "vllm-2"

# Chapter 36's 9-ticker window, and the peak the sweep has to reproduce.
CONTROL_FROM = "2026-08-09T18:05:00"
CONTROL_TO = "2026-08-09T19:55:00"
CONTROL_PEAK = (11, 15)  # was (13,17) against the mirrored reading; corrected: 13

# The DISCRIMINATING control. The window above cannot fail: its traffic is
# desynchronized, so mirroring the agent intervals moves each one by a
# different amount and the peak barely shifts (15 -> 13, inside any sane
# tolerance). It passed in the broken state AND the fixed state, which makes it
# a check of nothing.
#
# This window is a synchronized fan-out — ~17 agent-iteration STARTS per 30s
# across 76 conversations. Mirroring stacks those starts into an instant that
# precedes all of them, and the peak inflates 68 vs 39. Any regression back to
# the single-convention reading fails here loudly.
BURST_FROM = "2026-08-10T07:25:55"
BURST_TO = "2026-08-10T08:16:12"
BURST_PEAK = (35, 43)   # corrected 39; the mirrored reading gives 68
BURST_BROKEN = 60       # anything at or above this is the old mirrored bug


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def cycle_window(cycle_id: str, pad_min: int = 5) -> tuple[str, str]:
    """Derive a ledger window from a cycle's telemetry timestamps."""
    if psycopg2 is None:
        sys.exit("psycopg2 is required for --cycle: use the repo venv")
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT min(created_at), max(created_at) FROM v3_agent_telemetry WHERE cycle_id = %s",
            [cycle_id],
        )
        lo, hi = cur.fetchone()
    if lo is None:
        sys.exit(f"no telemetry rows for {cycle_id}")
    pad = timedelta(minutes=pad_min)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return (lo - pad).strftime(fmt), (hi + pad).strftime(fmt)


def fetch(frm: str, to: str, provider: str | None):
    if not MONGO_URI:
        sys.exit("PRISM_MONGO_URI is not set (source trading-service/.env)")
    query: dict = {"createdAt": {"$gte": frm, "$lte": to}}
    if provider:
        query["provider"] = provider
    col = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)["prism"]["requests"]
    return list(
        col.find(
            query,
            {"createdAt": 1, "totalTime": 1, "provider": 1, "project": 1,
             "operation": 1, "success": 1, "status": 1},
        )
    )


def _is_agent_row(d) -> bool:
    """True when `createdAt` is the START of this row's work, not the end.

    Prism has TWO writers of createdAt and they disagree:

      RequestLogger.log()          stamps at insert, which runs AFTER the work
                                   -> createdAt is the END.  (chat, memory:*,
                                   and the /agent error row)
      RequestLogger.insertPending() stamps before `start = performance.now()`
                                   in BaseAgenticHarness.createPassState
                                   -> createdAt is the START, and
                                   completePending never rewrites it.
                                   (every agent:* iteration row)

    totalTime always runs FORWARD from whichever instant that writer chose.
    """
    return str(d.get("operation") or "").startswith("agent:")


def analyse(docs) -> dict:
    intervals = []
    for d in docs:
        created = d.get("createdAt")
        if not created:
            continue
        stamp = _parse_iso(created).timestamp()
        # totalTime is SECONDS. A missing value yields a zero-width interval,
        # which contributes nothing rather than silently reading as instant.
        dur = float(d.get("totalTime") or 0.0)
        # Applying the END convention to an agent row mirrors its interval
        # backwards in time. That is not a rounding error: it moves a burst of
        # simultaneous STARTS into the minute before it happened, and stacks
        # them into a peak that never existed. Measured 2026-08-10 — a reported
        # peak of 68 sat at 08:10:10 while all 68 rows straddling it carried
        # createdAt of 08:10:12..08:12:53, every one of them beginning AFTER
        # the instant they were reported to be running. Corrected: 39.
        if _is_agent_row(d):
            start, end = stamp, stamp + dur
        else:
            start, end = stamp - dur, stamp
        intervals.append((start, end, dur, d))

    if not intervals:
        return {"requests": 0}

    events = sorted(
        [(s, 1) for s, _, _, _ in intervals] + [(e, -1) for _, e, _, _ in intervals]
    )
    cur = peak = 0
    peak_at = None
    for t, delta in events:
        cur += delta
        if cur > peak:
            peak, peak_at = cur, t

    span_lo = min(s for s, _, _, _ in intervals)
    span_hi = max(e for _, e, _, _ in intervals)
    span = max(span_hi - span_lo, 1e-9)
    busy = sum(d for _, _, d, _ in intervals)
    durs = sorted(d for _, _, d, _ in intervals)
    n = len(durs)

    by_source = defaultdict(lambda: [0, 0.0])
    for _, _, dur, d in intervals:
        key = f"{d.get('project') or '?'} / {d.get('operation') or '?'}"
        by_source[key][0] += 1
        by_source[key][1] += dur

    return {
        "requests": n,
        "peak_overlap": peak,
        "peak_at": datetime.fromtimestamp(peak_at, timezone.utc).isoformat() if peak_at else None,
        "span_s": round(span, 1),
        "busy_s": round(busy, 1),
        "offered_load": round(busy / span, 2),
        "median_s": round(durs[n // 2], 1),
        "p90_s": round(durs[min(int(n * 0.9), n - 1)], 1),
        "max_s": round(durs[-1], 1),
        "sources": sorted(
            (
                {"source": k, "n": v[0], "busy_s": round(v[1], 1),
                 "conc_equiv": round(v[1] / span, 2)}
                for k, v in by_source.items()
            ),
            key=lambda r: -r["busy_s"],
        ),
    }


def render(res: dict, frm: str, to: str, provider: str, cap: int | None):
    if not res.get("requests"):
        print(f"no ledger rows for provider={provider} in {frm}..{to}")
        return
    print(f"window   {frm} .. {to}  (provider={provider})")
    print(f"requests {res['requests']}   span {res['span_s'] / 60:.1f} min")
    print()
    print(f"  PEAK OVERLAP    {res['peak_overlap']}       at {res['peak_at']}")
    print(f"  offered load    {res['offered_load']:.2f} concurrent-equivalents")
    print(f"  service time    median {res['median_s']:.0f}s  p90 {res['p90_s']:.0f}s  max {res['max_s']:.0f}s")
    print()
    print("  by source (concurrent-equivalents):")
    for s in res["sources"][:10]:
        print(f"    {s['source']:<48} n={s['n']:<5} {s['conc_equiv']:>5.2f}")
    if cap:
        util = res["offered_load"] / cap
        print()
        print(f"  against a cap of {cap}: utilisation {util:.2f}")
        if util >= 1.0:
            print("    ^ cap is BELOW sustained demand — the queue grows rather than drains,")
            print("      so waits are bounded only by the shim's queue timeout, then 503.")
        else:
            print("    ^ cap is above sustained demand — queueing absorbs the peaks.")


def self_test() -> int:
    """Reproduce chapter 36's published peak for the 9-ticker window."""
    lo, hi = CONTROL_PEAK
    print(f"positive control: chapter 36's 9-ticker window, expected peak {lo}-{hi}")
    print(f"  {CONTROL_FROM} .. {CONTROL_TO}  provider={DEFAULT_PROVIDER}\n")
    res = analyse(fetch(CONTROL_FROM, CONTROL_TO, DEFAULT_PROVIDER))
    if not res.get("requests"):
        print("SELF-TEST FAILED — no rows; the ledger window is empty or unreachable.")
        return 1
    peak = res["peak_overlap"]
    print(f"  requests {res['requests']}   peak overlap {peak}   offered load {res['offered_load']}")
    print()
    if not (lo <= peak <= hi):
        print(f"SELF-TEST FAILED — peak {peak} outside {lo}-{hi}; the sweep does not "
              "reproduce the published reconstruction.")
        return 1
    print("  ok — but this window agrees under BOTH conventions, so on its own")
    print("       it proves nothing. The burst control below is the real check.\n")

    blo, bhi = BURST_PEAK
    print(f"discriminating control: the 2026-08-10 fan-out burst, expected {blo}-{bhi}")
    print(f"  {BURST_FROM} .. {BURST_TO}  provider={DEFAULT_PROVIDER}\n")
    bres = analyse(fetch(BURST_FROM, BURST_TO, DEFAULT_PROVIDER))
    if not bres.get("requests"):
        print("SELF-TEST FAILED — no rows in the burst window.")
        return 1
    bpeak = bres["peak_overlap"]
    print(f"  requests {bres['requests']}   peak overlap {bpeak}   "
          f"offered load {bres['offered_load']}")
    print()
    if bpeak >= BURST_BROKEN:
        print(f"SELF-TEST FAILED — peak {bpeak} >= {BURST_BROKEN}. That is the "
              "mirrored reading: agent rows are being treated as if createdAt "
              "were their END. See _is_agent_row().")
        return 1
    if not (blo <= bpeak <= bhi):
        print(f"SELF-TEST FAILED — peak {bpeak} outside {blo}-{bhi}.")
        return 1
    print("SELF-TEST PASSED — both controls, including the one that can fail.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cycle", help="derive the window from this cycle's telemetry")
    ap.add_argument("--from", dest="frm", help="ISO start, e.g. 2026-08-09T18:05")
    ap.add_argument("--to", dest="to", help="ISO end")
    ap.add_argument("--provider", default=DEFAULT_PROVIDER,
                    help=f"ledger provider; default {DEFAULT_PROVIDER} (Gold Spark). "
                         "'all' for every provider")
    ap.add_argument("--cap", type=int, help="compare offered load against this concurrency cap")
    ap.add_argument("--pad-min", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.cycle:
        frm, to = cycle_window(args.cycle, args.pad_min)
    elif args.frm and args.to:
        frm, to = args.frm, args.to
    else:
        ap.error("need --cycle, or both --from and --to")

    provider = None if args.provider == "all" else args.provider
    res = analyse(fetch(frm, to, provider))

    if args.json:
        print(json.dumps({"window": [frm, to], "provider": args.provider, **res}, indent=2))
    else:
        render(res, frm, to, args.provider, args.cap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
