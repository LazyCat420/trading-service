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

This script reads TWO stores, and only one of them was ever Postgres:

  prism.requests             the request ledger — Mongo from the start, and
                             still live (326k rows, newest today).
  trading_bot.v3_agent_telemetry
                             read ONLY by --cycle, to turn a cycle id into a
                             time window. That read WAS Postgres, and Postgres
                             froze at the 2026-08-19 cutover, so --cycle died
                             with "no telemetry rows" for every cycle run
                             since. Ported 2026-08-30.

Two ledger traps, both load-bearing:
  - prism's `createdAt` is a **string**, not a date. Range queries work only
    because ISO-8601 sorts lexically, and only when both bounds are the same
    shape — which is why cycle_window() hands back formatted strings and not
    datetimes.
  - `totalTime` is in **seconds**, not milliseconds.

  --self-test replays chapter 36's 9-ticker window and fails unless the sweep
  returns the peak that chapter published.

THE DEFAULT PROVIDER IS STALE, and porting --cycle is what exposed it. vllm-2's
last ledger row is 2026-08-27T05:39:52Z; vllm-3 (163,511 rows, newest today) is
where the traffic went. Of the 90 cycles --cycle can now reach for the first
time, 65 have no vllm-2 row anywhere in their window — so `--cycle <id>` with
no --provider is the MAJORITY-EMPTY path (65/90, measured 2026-08-30; with
--provider all, 1 of the 90 is empty). The constant is left alone on purpose:
peak overlap is a per-box number, so quietly switching boxes — or defaulting to
"all" — would answer a different question under the same heading. Instead an
empty result now NAMES the providers that do have rows in that window and
exits 1. It used to print one bare line and exit 0, which is precisely the
"compiles, runs, returns nothing" failure this migration exists to catch.

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from pymongo.errors import PyMongoError

    from app.db import mongo_query
    from app.db.mongo import get_mongo_db
except ImportError as exc:  # pragma: no cover - wrong interpreter
    sys.exit(f"use the repo venv (.venv/bin/python): {exc}")

# The telemetry collection --cycle reads. Postgres kept the same name, so
# `collection_for` is the identity here, but route through it anyway: the
# rename map is inert today and flipping it must not silently orphan this read.
TELEMETRY = "v3_agent_telemetry"

# Prism's ledger lives in prism's OWN database, not trading_bot, so it cannot
# go through mongo_query/mongo_store (those are bound to TRADING_MONGO_DB).
LEDGER = "requests"

# vllm-2 is Gold Spark; see vault-service/projects.json PROVIDER_VLLM_2_URL,
# which points at the shim rather than the box directly. It is the provider the
# two self-test controls were measured on, which is why it stays the default —
# but it has taken no traffic since 2026-08-27, so on a recent window this
# default is empty and `render` says which provider to ask for instead.
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
    """Derive a ledger window from a cycle's telemetry timestamps.

    Was:

        SELECT min(created_at), max(created_at)
        FROM v3_agent_telemetry WHERE cycle_id = %s

    against Postgres. That is not a query that broke loudly when the store
    froze — it kept answering, and its answer for anything recent was
    `(None, None)`, i.e. this function exited "no telemetry rows for <id>" for
    a cycle that had run perfectly well an hour earlier. Measured 2026-08-30:
    340 cycle ids in the archive, 430 in Mongo, 90 of them reachable only
    here, and 0 the other way — so every cycle since the cutover was a cycle
    --cycle refused to look at.

    Two shape checks behind the straight translation:

      * `created_at` is a BSON **Date** in all 8787 documents (typed and
        counted, no string contamination), so $min/$max are chronological. A
        string would have sorted lexically and, worse, sorted BELOW every real
        date — the newest row would read as the oldest. Postgres' column type
        used to make that impossible and nothing does now, so the check is a
        GUARD below rather than a sentence here: a docstring is not a gate.
      * Mongo returns it naive-UTC where the archive driver returned
        aware-UTC. `strftime` on the format below ignores tzinfo, so the
        window string is identical either way; verified against 6 pre-cutover
        cycles, 6/6 agreeing to the second.
        (Precision is how you tell the stores apart: the archive keeps
        ...:43.675091, Mongo ...:43.675000.)
    """
    lo, hi = mongo_query.agg_row(
        TELEMETRY,
        {"cycle_id": cycle_id},
        [("min", "created_at"), ("max", "created_at")],
    )
    if lo is None:
        sys.exit(f"no telemetry rows for {cycle_id}")
    if not isinstance(lo, datetime) or not isinstance(hi, datetime):
        # BSON orders String BELOW Date, so ONE string-typed created_at wins
        # $min outright and the window silently starts in the wrong place. The
        # next line would raise `unsupported operand type(s) for -: 'str' and
        # 'datetime.timedelta'`; say why instead.
        sys.exit(
            f"telemetry timestamps for {cycle_id} are "
            f"{type(lo).__name__}/{type(hi).__name__}, not datetimes — a "
            "string-typed created_at sorts below every real Date in BSON, so "
            "$min/$max are not chronological. Fix the writer, not this script."
        )
    pad = timedelta(minutes=pad_min)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return (lo - pad).strftime(fmt), (hi + pad).strftime(fmt)


def fetch(frm: str, to: str, provider: str | None):
    """Ledger rows whose `createdAt` string falls in [frm, to].

    Resolved through `app.db.mongo`, which reads PRISM_MONGO_URI out of
    settings (and therefore out of `.env`). The old direct `MongoClient` read
    the raw environment, so the script exited "PRISM_MONGO_URI is not set"
    unless the operator had sourced `.env` into their shell first — a failure
    mode that has nothing to do with the question being asked. An explicit
    PRISM_MONGO_URI in the environment still wins, as it did before.
    """
    query: dict = {"createdAt": {"$gte": frm, "$lte": to}}
    if provider:
        query["provider"] = provider
    try:
        col = get_mongo_db()[LEDGER]
        return list(
            col.find(
                query,
                {"createdAt": 1, "totalTime": 1, "provider": 1, "project": 1,
                 "operation": 1, "success": 1, "status": 1},
            )
        )
    except PyMongoError as exc:
        sys.exit(f"prism request ledger unreachable: {type(exc).__name__}: {exc}")


def _ledger_name() -> str:
    """`<database>.<collection>` actually resolved, for the empty messages.

    "the window itself is empty" is a claim about a specific store, and it is
    FALSE if the read was routed to the wrong one. Naming the database turns a
    misroute into something an operator can see: `prism.requests` is the
    ledger, `trading_bot.requests` does not exist.
    """
    try:
        return f"{get_mongo_db().name}.{LEDGER}"
    except Exception:  # display string only — never worth failing a run over
        return LEDGER


def providers_in_window(frm: str, to: str, limit: int = 8) -> list[tuple[str, int]] | None:
    """Which providers DO have ledger rows in [frm, to], busiest first.

    Only called when the asked-for provider had none. An empty answer that
    cannot say whether the WINDOW is empty or only the PROVIDER is empty is
    indistinguishable from a broken read — which is the whole reason this port
    exists — and the difference costs one grouped count.

    `[]` means CHECKED AND EMPTY; `None` means COULD NOT CHECK. Collapsing the
    two would let a failed follow-up query print "the window itself is empty",
    which is a probe failing open on the very evidence it exists to produce.
    """
    try:
        col = get_mongo_db()[LEDGER]
        rows = list(col.aggregate([
            {"$match": {"createdAt": {"$gte": frm, "$lte": to}}},
            {"$group": {"_id": "$provider", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
            {"$limit": limit},
        ]))
    except PyMongoError:
        return None
    return [(r["_id"] or "?", r["n"]) for r in rows]


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


def render(res: dict, frm: str, to: str, provider: str, cap: int | None,
           alternatives: list[tuple[str, int]] | None = None):
    """`alternatives` is only consulted on the empty path: a list of
    (provider, rows) that DO cover the window, or None if that could not be
    established."""
    if not res.get("requests"):
        print(f"no ledger rows for provider={provider} in {frm}..{to}")
        if alternatives:
            print("  providers WITH rows in this window: "
                  + ", ".join(f"{p} ({n})" for p, n in alternatives))
            print(f"  re-run with --provider {alternatives[0][0]}, "
                  "or --provider all to pool every box.")
        elif alternatives is None:
            print("  could not establish which providers DO have rows here — "
                  "the follow-up query failed, so this empty is unexplained.")
        else:
            print(f"  no provider has rows in this window in {_ledger_name()} "
                  "— the window itself is empty, not just this provider's "
                  "slice of it.")
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
                    help=f"ledger provider; default {DEFAULT_PROVIDER} (Gold Spark), "
                         "which has taken no traffic since 2026-08-27 — on a "
                         "recent window ask for vllm-3, or 'all' to pool every box")
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
    # Only reached when the answer is empty, and only then is it worth a
    # second query.
    alternatives = None if res.get("requests") else providers_in_window(frm, to)

    if args.json:
        payload = {"window": [frm, to], "provider": args.provider, **res}
        if not res.get("requests"):
            payload["providers_in_window"] = (
                None if alternatives is None
                else [{"provider": p, "n": n} for p, n in alternatives]
            )
        print(json.dumps(payload, indent=2))
    else:
        render(res, frm, to, args.provider, args.cap, alternatives)
    # An empty answer exits NON-ZERO. The old code exited 0, so `--cycle <id>`
    # against a provider that has not run since 2026-08-27 looked like a
    # successful measurement of zero concurrency rather than a question that
    # was never answered. 65 of the 90 newly reachable cycles take this path
    # under the default provider.
    return 0 if res.get("requests") else 1


if __name__ == "__main__":
    sys.exit(main())
