#!/usr/bin/env python3
"""Smoke Test: Streaming Pipeline Validation — reads MONGO.

WHAT THIS ANSWERS, AND WHICH HALF OF IT IS DEAD
-----------------------------------------------
It starts a one-ticker cycle and reads `pipeline_events` and `analysis_results`
back to answer two different questions:

  A. did the cycle run?  Events appeared, the state reached a terminal status,
     and `analysis_results` holds a decision for the ticker.
  B. did the STREAMING pipeline behave?  The watchlist pre-push, the two
     parallel tracks, the analysis worker queue and its dedup — the milestones
     and the four `critical_checks` below.

Question A is alive. Question B is not, and that is the most important thing on
this page.

Counted 2026-08-30 over all 201,345 documents in `pipeline_events`:

    step name it looks for   events   last emitted   emitted by anything today?
    watchlist_prepush            10   2026-06-02     no
    worker_got_*               4647   2026-06-24     no
    worker_dedup_*               79   2026-05-18     no
    collection_complete         292   2026-06-24     no
    pipeline_done               237   2026-06-24     no
    track_a_start                 0   never          no
    track_b_start                 0   never          no
    parallel_start                0   never          no
    v2_start_*                    0   never          renamed `v3_start_*`

`grep -rn` over `app/` finds none of the nine. The five that did fire were
emitted from `app/cycle/**`, deleted whole on 2026-06-25 by 0cedef3 "chore:
remove deprecated v2 cycle pipeline" — the release that replaced the two-track
streaming cycle with the V3 per-ticker pipeline, in which collection happens
INSIDE each ticker's analysis (`v3_start_X` then `v3_precollect_X`), so there is
no pre-push, no Track A/Track B, no worker queue and no dedup left to observe.

The three `track_*` / `parallel_start` names are worse than retired: they appear
in no commit anywhere outside this file (`git grep track_a_start 0cedef3^`
matches only this script), so `checks["parallel_tracks"]` has been unfireable
since the file was written on 2026-05-17 in bec7f87. `v2_start_` did have an
emitter (`app/core/emit_helpers.py`), but every V2 call site passed an explicit
`step_name`, which is why a V2-era cycle such as `cycle-1782331390` records 82
`worker_got_*` events and not one `v2_start_*`.

So all four entries of `critical_checks` were dead on arrival: this script has
never been able to print `PASS`, and could only ever burn a full LLM cycle to
print PARTIAL or FAIL. `first_analysis_start` is the one milestone with a live
successor and is matched against `v3_start_<TICKER>` (and still against
`v2_start_`, so replaying an archived cycle behaves); the other eight are
marked RETIRED, printed with the reason, and excluded from the verdict rather
than scored as failures against subsystems that no longer exist.

`scripts/smoke_test_cycle.py` — already on Mongo — answers question A on its
own, so what survives here is a duplicate. This file is a retirement candidate.
It is left working and honest until that call is made; note that deleting it
today also fails
`tests/unit/test_bench_stage_reads_mongo.py::test_the_terminal_status_set_is_the_one_the_rest_of_the_repo_uses`,
which parses the cycle-status guard below out of this file by path.

WHY THE STORE CHANGED
---------------------
The three reads went to the archive, which froze at the 2026-08-19 cutover:
`pipeline_events` stops at 2026-08-19 22:55:03 with 190,775 rows against
Mongo's 201,345, and `analysis_results` at 5,102 rows against Mongo's 5,290.
A smoke test reads back the cycle it has just started, so on the archive every
read returned nothing at all. Since 2026-08-28 it did not get that far: the
archive DSN was reached through the `Settings` field removed that day (see
e67d240, "the archive DSN is asked for by name"), so the first `get_db()`
raised `AttributeError: 'Settings' object has no attribute ...` and the run
ended at "Failed to start cycle" / exit 1.

THE TWO TRAPS IN THE TRANSLATION
--------------------------------
1. `analysis_results.result_json` is a TEXT column in the archive and a
   SUBDOCUMENT in Mongo. `json.loads()` on a dict raises TypeError, which the
   old `except Exception` turned into "(parse error)" — for every row. Across
   the whole collection the two stores agree exactly (0 archive rows missing,
   188 written after the cutover, and all 1,972 non-null payloads identical
   once the archive's text is parsed), so the ONLY thing to change is the
   decode. It is now `decode_result()`, which takes either shape. The 3,130
   rows whose payload is genuinely null — 59% of the collection, one store
   agreeing with the other to the row — are reported as "no payload", not as a
   parse failure; the old code called those an error too.

2. `ORDER BY timestamp ASC OFFSET n` was a TOTAL order in the archive:
   microsecond timestamps, and 0 collisions inside a cycle across all 190,775
   rows. BSON keeps milliseconds, and those same rows truncated to milliseconds
   collide 2,515 times; Mongo holds 2,522 collision groups covering 6,274
   events, the worst of them 14 `v3_dropped_*` events sharing one millisecond.
   `OFFSET n` over a sort with ties is a page boundary inside a group whose
   order the sort does not define, so the tail can silently lose an event or
   re-show one — and events emitted in a burst are exactly what the milestone
   detector is made of. The sort is therefore `{"timestamp": 1, "_id": 1}`, and
   `_id` is unique by construction, so the order is total and the paging is
   repeatable.

   Two honest limits on that. It is a GUARANTEE, not an observed repair: on
   this deployment, `{"timestamp": 1}` alone did page consistently at all 56
   and all 51 offsets of two archived cycles carrying 8 and 4 collision groups
   — Mongo simply does not promise it, and the plan that delivers it can change
   with an index, with memory pressure, or with a `$sort` that spills. And the
   ordering WITHIN one millisecond is not recoverable from this store at all:
   the microseconds are gone, and for backfilled documents `_id` agrees with
   the archive's microsecond order in only 6 of the 14 tie groups measured. The
   milestones take the first event matching each pattern, so a reshuffle inside
   one millisecond moves a milestone by less than a millisecond; losing an
   event to a page boundary loses the milestone outright, which is the failure
   worth engineering against.

Milestone times are now measured between EVENT timestamps rather than from the
poller's own clock. The old numbers were quantised to the 2-second poll
interval and inflated by the poll's own latency, and they mixed this machine's
clock with the writer's; a difference of two event timestamps has neither
problem.

Usage:
    python scripts/smoke_test_streaming.py               # default AAPL
    python scripts/smoke_test_streaming.py NVDA          # custom ticker
    python scripts/smoke_test_streaming.py --timeout 600 # 10-minute timeout
    python scripts/smoke_test_streaming.py --cycle-id cycle-v3-1788074145
        # score a cycle that has ALREADY run, straight out of the event log.
        # Starts nothing and writes nothing, which is the only way to exercise
        # this instrument without spending a live cycle on it.

Environment:
    MONGO_URI / TRADING_MONGO_DB must resolve (or .env file present).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Callable, NamedTuple, Optional

# `python scripts/smoke_test_streaming.py` puts `scripts/` on sys.path[0], not
# the repo root, so `from app.db import ...` needs the root added first.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == "__main__":
    # ORDER IS LOAD-BEARING. `Settings` reads the environment once, when
    # `app.config` is first imported — which the `app.db` import below does —
    # so setting this afterwards is a no-op. Guarded on __main__ so that
    # importing this module (a test does) cannot cap someone else's cycle.
    os.environ["MAX_CYCLE_TICKERS"] = "1"

from app.db import mongo_query, mongo_store  # noqa: E402

# POSTGRES TABLE NAMES. Every helper resolves a table to its collection exactly
# once, internally; handing one `collection_for(...)` resolves it twice and, the
# day renames are switched on, reads a collection that does not exist. See
# tests/unit/test_no_double_collection_resolution.py.
EVENTS = "pipeline_events"
RESULTS = "analysis_results"

#: The SELECT list of the event read, in order. Callers unpack positionally.
EVENT_COLUMNS = ("phase", "step", "detail", "status", "elapsed_ms", "timestamp")

POLL_INTERVAL_SECONDS = 2
FAST_START_BUDGET_S = 30.0

OK, FAIL, SKIP, RETIRED_MARK = "✅", "❌", "⏭️", "🗑️"

LIVE = "live"
RETIRED = "retired"


class Milestone(NamedTuple):
    """One timing this test looks for, and whether it can still happen.

    `state` is the point of the type. A milestone whose emitter was deleted is
    not a milestone that FAILED — reporting it as a failure is how a dead check
    keeps a red light burning that nobody can ever turn green, and how the one
    check that still means something gets lost among four that cannot.
    """
    key: str
    label: str
    match: Callable[[str], bool]
    state: str
    note: str


def _exact(name: str) -> Callable[[str], bool]:
    return lambda step: step == name


def _prefix(*names: str) -> Callable[[str], bool]:
    return lambda step: step.startswith(names)


MILESTONES: tuple[Milestone, ...] = (
    Milestone("watchlist_prepush", "Watchlist Prepush",
              _exact("watchlist_prepush"), RETIRED,
              "10 events, last 2026-06-02; emitter deleted 2026-06-25 (0cedef3)"),
    Milestone("first_worker_got", "First Worker Got",
              _prefix("worker_got_"), RETIRED,
              "4647 events, last 2026-06-24; the worker queue was deleted with "
              "the V2 cycle (0cedef3)"),
    Milestone("track_a_start", "Track A Start",
              _exact("track_a_start"), RETIRED,
              "never emitted by anything, in any commit"),
    Milestone("track_b_start", "Track B Start",
              _exact("track_b_start"), RETIRED,
              "never emitted by anything, in any commit"),
    Milestone("parallel_start", "Parallel Start",
              _exact("parallel_start"), RETIRED,
              "never emitted by anything, in any commit"),
    Milestone("first_analysis_start", "First Analysis Start",
              _prefix("v3_start_", "v2_start_"), LIVE,
              "the V3 pipeline's per-ticker start; `v2_start_` kept so an "
              "archived cycle still replays"),
    Milestone("first_dedup", "First Dedup",
              _prefix("worker_dedup_"), RETIRED,
              "79 events, last 2026-05-18; queue dedup went with the V2 cycle"),
    Milestone("collection_complete", "Collection Complete",
              _exact("collection_complete"), RETIRED,
              "292 events, last 2026-06-24; V3 collects per ticker inside "
              "analysis (`v3_precollect_ok_<T>`), so there is no cycle-wide "
              "collection phase to finish"),
    Milestone("pipeline_done", "Pipeline Done",
              _exact("pipeline_done"), RETIRED,
              "237 events, last 2026-06-24; V3 ends per ticker (`v3_done_<T>`)"),
)

#: Only checks with a live subject decide the verdict.
CRITICAL_CHECKS = ("fast_start",)


# ── the read seam ──────────────────────────────────────────────────────────

def count_events(cycle_id: str) -> int:
    """`SELECT COUNT(*) FROM pipeline_events WHERE cycle_id = %s`."""
    return mongo_query.count(EVENTS, {"cycle_id": cycle_id})


def read_events_after(cycle_id: str, already_seen: int = 0) -> list[tuple]:
    """The cycle's events past the first `already_seen`, oldest first.

    `SELECT ... ORDER BY timestamp ASC OFFSET %s`. `sql_to_mongo` refuses this
    one outright — "Unsupported OFFSET — find_docs has no skip parameter" — so
    it is a pipeline, over the TABLE name, resolved once by `aggregate`.

    Two details are load-bearing:

    `{"timestamp": 1, "_id": 1}` and not `{"timestamp": 1}`. The archive's
    microsecond timestamps made `ORDER BY timestamp` a total order inside a
    cycle (0 collisions in 190,775 rows); at BSON's millisecond resolution the
    same events collide 2,522 times, up to 14 at once, and a `$skip` into a
    group the sort does not order is a page boundary that can drop an event.
    `_id` is unique, so the order is total and the paging repeatable — a
    guarantee rather than a repair, since the timestamp-only sort was measured
    paging consistently here; Mongo just never promised it would.

    `$skip` and not a Python slice, because a cycle is not always small: the
    largest in the archive holds 15,566 events, and re-reading them all every
    two seconds is a different program.
    """
    pipeline = [
        {"$match": {"cycle_id": cycle_id}},
        {"$sort": {"timestamp": 1, "_id": 1}},
        {"$skip": max(0, already_seen)},
        {"$project": {c: 1 for c in EVENT_COLUMNS} | {"_id": 0}},
    ]
    return [tuple(doc.get(c) for c in EVENT_COLUMNS)
            for doc in mongo_store.aggregate(EVENTS, pipeline)]


def read_analysis_results(cycle_id: str) -> list[tuple]:
    """`SELECT ticker, result_json FROM analysis_results WHERE cycle_id = %s`."""
    return mongo_query.find_rows(RESULTS, {"cycle_id": cycle_id},
                                 ["ticker", "result_json"])


def decode_result(value: Any) -> Optional[dict]:
    """The analysis payload as a dict, whatever shape the store hands back.

    The archive typed this column TEXT and Mongo holds a subdocument, so
    `json.loads(value)` raises TypeError on every live row. Returning None for
    a genuinely absent payload — 3,130 of 5,290 rows carry null here — keeps
    "nothing was stored" distinguishable from "something was stored and could
    not be read", which the old blanket `except Exception` did not.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# ── scoring ────────────────────────────────────────────────────────────────

def _seconds_between(t0: Any, ts: Any) -> Optional[float]:
    try:
        return (ts - t0).total_seconds()
    except Exception:  # noqa: BLE001 - a stray non-datetime is "not observed"
        return None


def collect_milestones(events: list[tuple]) -> dict[str, Optional[float]]:
    """key -> seconds between the cycle's FIRST event and that milestone's.

    Measured between two event timestamps, so the answer does not depend on the
    poller's 2-second cadence, on how long a poll took, or on the clock of the
    machine running this file.
    """
    found: dict[str, Optional[float]] = {m.key: None for m in MILESTONES}
    if not events:
        return found
    t0 = events[0][5]
    for _phase, step, _detail, _status, _ms, ts in events:
        step = step or ""
        for m in MILESTONES:
            if found[m.key] is None and m.match(step):
                found[m.key] = _seconds_between(t0, ts)
                break
    return found


def event_line(event: tuple, t0: Any) -> str:
    phase, step, detail, status, ms, ts = event
    emoji = (OK if status == "ok" else FAIL if status == "error"
             else "⏳" if status == "running" else SKIP)
    at = _seconds_between(t0, ts)
    at_str = f"{int(at):>4}s" if at is not None else "   ?s"
    ms_str = f" ({ms}ms)" if ms else ""
    return f"  {emoji} [{at_str}] [{phase}] {step}: {(detail or '')[:80]}{ms_str}"


def report(cycle_id: str, events: list[tuple], analysis_rows: list[tuple],
           status: str, total_elapsed: float) -> bool:
    """Sections [3/6]..[6/6]; returns the verdict the caller exits on."""
    milestones = collect_milestones(events)

    print()
    print("=" * 70)
    print("[3/6] STREAMING MILESTONES")
    print("=" * 70)
    for m in MILESTONES:
        ts = milestones[m.key]
        if ts is not None:
            print(f"  {OK} {m.label:.<40} {ts:.1f}s")
        elif m.state == RETIRED:
            print(f"  {RETIRED_MARK} {m.label:.<40} (retired — {m.note})")
        else:
            print(f"  {SKIP} {m.label:.<40} (not observed)")

    print()
    print("=" * 70)
    print("[4/6] STREAMING VALIDATION")
    print("=" * 70)

    checks: dict[str, Optional[bool]] = {}

    # The one check with a live subject: how long until the analysis pipeline
    # started for the first ticker.
    start = milestones["first_analysis_start"]
    if start is not None:
        checks["fast_start"] = start < FAST_START_BUDGET_S
        verdict = OK if checks["fast_start"] else FAIL
        tail = "" if checks["fast_start"] else f" (should be < {FAST_START_BUDGET_S:.0f}s)"
        print(f"  {verdict} Fast start: analysis pipeline started at {start:.1f}s"
              f"{tail}")
    else:
        checks["fast_start"] = False
        print(f"  {FAIL} Fast start: no v3_start_<TICKER> event in this cycle's "
              f"{len(events)} events")

    retired = [m.label for m in MILESTONES if m.state == RETIRED]
    print(f"  {RETIRED_MARK} Not checked, subject deleted 2026-06-25 (0cedef3): "
          f"{', '.join(retired)}")
    print(f"       — pre-push, the two tracks, the worker queue and its dedup "
          f"are gone; see the module docstring for the per-step counts.")

    print()
    print("=" * 70)
    print("[5/6] ANALYSIS RESULTS")
    print("=" * 70)

    if analysis_rows:
        for ticker, payload in analysis_rows:
            result = decode_result(payload)
            if result is None:
                # Not an error: 3,130 of 5,290 rows store no payload at all.
                print(f"  {SKIP} {ticker}: (no result payload stored)")
                continue
            action = result.get("action", "?")
            confidence = result.get("confidence", 0)
            try:
                took = float(result.get("total_time_s", 0) or 0)
            except (TypeError, ValueError):
                took = 0.0
            try:
                tokens = int(result.get("total_tokens", 0) or 0)
            except (TypeError, ValueError):
                tokens = 0
            print(f"  {OK} {ticker}: {action} @ {confidence}% "
                  f"({took:.1f}s, {tokens:,} tokens)")
    else:
        print(f"  {FAIL} No analysis results found")

    print()
    print("=" * 70)
    print("[6/6] VERDICT")
    print("=" * 70)

    passed = all(checks.get(c) for c in CRITICAL_CHECKS)
    completed = status in ("done", "replay") and len(analysis_rows) > 0

    if passed and completed:
        print(f"  🟢 PASS — cycle {cycle_id} validated in {int(total_elapsed)}s")
        print(f"     Time to first analysis: {milestones['first_analysis_start']:.1f}s "
              f"(target: <{FAST_START_BUDGET_S:.0f}s)")
    elif completed:
        failed = [c for c in CRITICAL_CHECKS if not checks.get(c)]
        print(f"  🟡 PARTIAL — cycle completed but checks failed: {', '.join(failed)}")
    else:
        print(f"  🔴 FAIL — cycle status: {status}, results: {len(analysis_rows)}")
    print(f"     {len(MILESTONES) - 1} of {len(MILESTONES)} milestones are RETIRED: "
          f"the streaming pipeline they timed was deleted on 2026-06-25.")

    print()
    print("=" * 70)
    return passed and completed


# ── the two entry points ───────────────────────────────────────────────────

def score_recorded_cycle(cycle_id: str, verbose: bool) -> bool:
    """`--cycle-id`: score a cycle that has already run. Reads only.

    Starts nothing, resets nothing and writes nothing, which is what makes it
    usable as a check on this instrument itself — the live path below costs a
    full LLM cycle and moves `pipeline_state`.
    """
    print("=" * 70)
    print(f"  STREAMING PIPELINE SMOKE TEST: {cycle_id} (replay, read-only)")
    print(f"  Store: mongo {mongo_store.TRADING_MONGO_DB}")
    print("=" * 70)
    print()

    events = read_events_after(cycle_id)
    if not events:
        print(f"  {FAIL} No {EVENTS} documents for cycle_id={cycle_id!r} — "
              f"nothing to score.")
        return False

    t0 = events[0][5]
    span = _seconds_between(t0, events[-1][5]) or 0.0
    print(f"[2/6] Replaying {len(events)} events spanning {span:.1f}s "
          f"(first at {t0})")
    if verbose:
        for ev in events:
            print(event_line(ev, t0))

    return report(cycle_id, events, read_analysis_results(cycle_id),
                  "replay", span)


async def run_streaming_test(ticker: str, timeout: int, verbose: bool) -> bool:
    """Start a one-ticker cycle and score it. THIS WRITES: it starts a real
    cycle, and force-resets `pipeline_state` if a stuck one is in the way."""
    from app.services.pipeline_service import PipelineService

    print("=" * 70)
    print(f"  STREAMING PIPELINE SMOKE TEST: {ticker}")
    print(f"  Store: mongo {mongo_store.TRADING_MONGO_DB}")
    print("=" * 70)
    print()

    # Reset state if stuck
    current = PipelineService.get_current_state(summary_only=True)
    if current.get("status") not in ("idle", "done", "error", "stopped", "interrupted"):
        print(f"  ⚠️  Pipeline in '{current.get('status')}' state. Resetting.")
        PipelineService._state["status"] = "idle"
        PipelineService.save_state()

    print(f"[1/6] Starting cycle for [{ticker}]...")
    start_time = time.monotonic()

    try:
        result = await PipelineService.start_cycle(
            tickers=[ticker],
            collect=True,
            analyze=True,
            trade=False,  # Skip trading for smoke test
            trigger_type="streaming_smoke_test",
            max_tickers=1,
        )
        cycle_id = result.get("cycle_id", "unknown")
        print(f"  {OK} Cycle started: {cycle_id}")
    except Exception as e:  # noqa: BLE001 - the failure IS the answer here
        print(f"  {FAIL} Failed to start cycle: {e}")
        return False

    print(f"[2/6] Monitoring streaming events (timeout={timeout}s)...")
    print()

    events: list[tuple] = []
    status = "unknown"

    while True:
        elapsed = time.monotonic() - start_time
        if elapsed > timeout:
            print(f"\n  {FAIL} TIMEOUT after {int(elapsed)}s")
            break

        status = PipelineService.get_current_state(
            summary_only=True).get("status", "unknown")

        if count_events(cycle_id) > len(events):
            fresh = read_events_after(cycle_id, len(events))
            if verbose:
                t0 = events[0][5] if events else (fresh[0][5] if fresh else None)
                for ev in fresh:
                    print(event_line(ev, t0))
            events.extend(fresh)

        if status in ("done", "error", "stopped"):
            break

        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    # One last drain. The terminal status is written before the final events
    # land, so breaking straight out of the loop dropped the tail of every
    # cycle — including, on a fast one, the milestone being measured.
    tail = read_events_after(cycle_id, len(events))
    if tail:
        if verbose:
            t0 = events[0][5] if events else tail[0][5]
            for ev in tail:
                print(event_line(ev, t0))
        events.extend(tail)

    return report(cycle_id, events, read_analysis_results(cycle_id),
                  status, time.monotonic() - start_time)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate streaming pipeline timing")
    parser.add_argument("ticker", nargs="?", default="AAPL", help="Ticker symbol (default: AAPL)")
    parser.add_argument("--timeout", type=int, default=600, help="Max seconds (default: 600)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print all events")
    parser.add_argument("--cycle-id", default=None,
                        help="Score a cycle that has already run, from the event "
                             "log. Starts nothing and writes nothing; `ticker` "
                             "and --timeout are ignored.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    if args.cycle_id:
        success = score_recorded_cycle(args.cycle_id, args.verbose)
    else:
        success = asyncio.run(
            run_streaming_test(
                ticker=args.ticker.upper(),
                timeout=args.timeout,
                verbose=args.verbose,
            )
        )
    sys.exit(0 if success else 1)
