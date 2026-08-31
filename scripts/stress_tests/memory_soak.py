"""Memory soak: repeat a real read workload N times and watch RSS for a leak.

USAGE
    python scripts/stress_tests/memory_soak.py                 (100 iterations)
    python scripts/stress_tests/memory_soak.py --iterations 20
    python scripts/stress_tests/memory_soak.py --iterations 20 --leak-mb 250

Exit 0 when RSS growth stayed under the threshold and every iteration read
something, 1 otherwise. See "THE EXIT CODE WAS WRONG" below: the old version
always exited 0.

THIS FILE NAMES NONE OF THE ARCHIVE TOKENS, DELIBERATELY
--------------------------------------------------------
The port check for this migration is a grep for five strings: the old driver,
the DSN setting, the archive connection module, a DSN keyword and the vendor's
lowercase name. An earlier draft of this docstring spelled two of them while
EXPLAINING that they had been removed, which is a hazard and not merely untidy:
`_strip_prose` in `scripts/pg_script_inventory.py` removes line comments before
it removes docstrings by verbatim replace, so one hash character anywhere in a
docstring truncates the line the replace is looking for, the replace no-ops,
and the whole docstring is then scanned as code. The prose "explaining" a
removed coupling then IS the coupling, to that scanner. Guarding the hash with
a test made the trap survivable rather than absent. So the words are gone
instead, in every case, and `tests/unit/test_memory_soak.py` keeps them gone.

WHAT THIS SOAKS, AND WHY IT IS NOT WHAT IT USED TO SOAK
-------------------------------------------------------
This file used to drive `execute_v2_pipeline` from
`app.cognition.orchestration.runner`. That module was DELETED on 2026-06-26 by
commit 834c894 "Cleanup legacy V1/V2 agents and orchestration logic", so the
import at the top of this file raised ModuleNotFoundError for two months --
since well before the 2026-08-19 cutover. The instrument measured nothing
during the entire migration it was meant to help judge.

The v2 pipeline has no drop-in successor that a soak may call. The live one is
`PipelineService.start_cycle`, and it is the wrong shape for this instrument in
three separate ways: it WRITES decisions, telemetry and events to the live
store; it spends real LLM tokens per ticker; and `PipelineService` keeps its
cycle state in class-level attributes, so a soak looping on it would fight the
scheduler for the singleton. Running it 100 times to look for a leak would
corrupt the thing it is measuring.

So the workload changed, and the change is the honest half of this port: what
gets soaked now is the MONGO READ PATH. The cutover replaced the entire
persistence layer on 2026-08-19; whether repeated reads through
`mongo_query`/`mongo_store` accumulate memory is not something anybody has
measured, and a leak there reaches every caller in `app/`.

HOW MUCH OF THE SEAM THIS ACTUALLY COVERS
-----------------------------------------
Eight of the fifteen public readers: `find_rows`, `find_row`, `find_dicts`,
`scalar`, `agg_row`, `group_rows`, `count`, `exists`. That is the projected
tuple read, the whole-document read, the one-row and one-value reads, the
existence probe, two `$group` pipelines and a plain count -- every distinct
allocation shape the seam has, since `find_row`/`scalar`/`exists` all bottom
out in `mongo_store.find_docs` and `agg_row`/`group_rows` in
`mongo_store.aggregate`.

NOT covered, and this is not a claim that they are leak-free:
`join_rows`, `left_join_rows` and `anti_join_rows` (each pulls BOTH sides into
Python and stitches them there, which is the one shape in the seam that could
plausibly hold memory -- but the pair that would exercise it honestly is
positions x price_history, and price_history is barred here for the vendor
reason below), and `mongo_store.distinct_values` (an unindexed distinct over a
200k collection is 20+ seconds, which at 100 iterations turns a 3-minute soak
into an hour). Anyone extending this should add the joins against a small pair
of collections first.

This does NOT measure what the file's name implied in June (a full trading
cycle). If you need that, it is a new instrument against `PipelineService`,
and it needs a sandbox store to write into first.

WHY THE ARCHIVE IMPORT WAS NOT A PORT
-------------------------------------
The old line 9 imported `get_db` from the archive connection helper under
`scripts/migration/`. It was never used: an AST walk over the module body finds
no reference to `get_db` anywhere -- not as a name, not as an attribute. It got
there mechanically, because commit 6bc835f (2026-08-18) rewrote every
`app.db.connection` import in the tree when the relational driver left the
application image, including this one, in a file that had already been
unimportable for eight weeks.

Calling it would have failed anyway. `get_db()` resolves its DSN through a
settings attribute that was deleted on 2026-08-28, so it raises AttributeError
before it ever opens a socket -- which is what
`docs/migration/pg_script_inventory.json` means by `"can_connect": false` for
this path. Removing the import is therefore the whole of the archive port;
there was no query, and `verify_translations.py` confirms it -- "no mechanical
SELECTs match ['memory_soak.py']".

AN EMPTY WORKLOAD CANNOT LEAK
-----------------------------
The failure this instrument is most likely to have is not a false alarm, it is
silence: a battery whose filters match nothing reads zero documents, allocates
nothing, and reports "no leak" forever. That is the same shape as the deleted
pipeline -- an instrument that passes because it did no work.

Two of the probes below were written with filters that matched nothing on the
first attempt (`severity="ERROR"` -- the stored values are lowercase, and
`severity` never carries "error" at all; the field is `event_type`). So every
probe asserts a non-empty result on EVERY iteration, and the soak aborts with
exit 1 if one comes back empty. `_read_battery` returning zero rows is a RED
result, not a clean one.

A filter that cannot come back empty is not a guard, either. The news probe
used to filter `quality_status` on `{"$exists": true}`, which matched 116,354
of 116,354 documents: it would have survived the vocabulary change that is the
exact failure this guard exists to catch. It now selects `"ok"` -- 81,970 of
116,354 on 2026-08-30 -- so the guard has something to lose.

The same doctrine applies to the iteration count. `--iterations 0` opens no
connection, reads nothing and cannot leak, so it is refused with exit 1 rather
than reported as a clean soak; it used to log "completed" and exit 0.

Likewise an exception no longer scrolls past. The old loop caught every
Exception per iteration and logged it, so a run in which all 100 iterations
raised still logged "Memory soak test completed." and returned True. These are
deterministic reads: one raising is a fault, not flake.

THE EXIT CODE WAS WRONG
-----------------------
`asyncio.run(memory_soak_test())` discarded the return value, so the process
exited 0 whether or not it had just logged "SEVERE MEMORY LEAK DETECTED". A
leak detector nothing can gate on is decoration; it now exits 1.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import mongo_query  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#: Batteries to run when nothing says otherwise. Long enough that a per-read
#: leak of a few KB shows up against the ~0.75 MB of first-connection noise.
DEFAULT_ITERATIONS = 100

#: Growth past this many MB is reported as a severe leak and stops the run.
DEFAULT_LEAK_MB = 500


def _rss_mb() -> float:
    """Resident set size in MB.

    psutil is the preferred reading and is declared in requirements.txt, but it
    is not installed in every checkout's venv (it is absent from this
    worktree's), and an instrument that cannot start is worth less than one
    that reads RSS the way psutil reads it on Linux. `/proc/self/statm` field 2
    is resident pages; multiply by the page size.

    Not `resource.getrusage().ru_maxrss`: that is a high-WATER mark, which
    never falls, so it cannot tell a leak from a transient peak that was freed.
    """
    try:
        import psutil  # noqa: PLC0415 - optional, see above
        return psutil.Process().memory_info().rss / 1024 / 1024
    except ImportError:
        import os  # noqa: PLC0415
        with open("/proc/self/statm", encoding="ascii") as fh:
            resident_pages = int(fh.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024


def _read_battery() -> tuple[list[tuple[str, int]], int]:
    """One pass of representative reads.

    Returns `(probes, materialized)` -- each probe as (name, value) for the
    non-empty guard, and separately the number of documents actually pulled
    into this process. The two are NOT the same number and must not be added
    together: `count/news_articles` reports 81,970 while transferring one
    integer, so a single figure summing them would claim ~118,000 documents of
    allocation for 312. Only `materialized` is memory this process holds.

    Read-only by construction: every call is a `mongo_query` reader. Nothing
    here writes, and nothing may be added here that does -- this is pointed at
    the live store and it runs 100 times.

    Each collection is named by its TABLE NAME, never a resolved collection
    name: every helper calls `collection_for()` internally exactly once, and
    passing an already-resolved name resolves it twice (see
    `tests/unit/test_no_double_collection_resolution.py`).

    `price_history` is deliberately absent. Every read of it must pin one
    vendor -- one ticker-date carries several vendor prints that disagree by
    ~20% -- and a soak has no reason to take on that obligation when four other
    collections exercise the same code path.

    Every filter, sort and limit here is pinned by
    `tests/unit/test_memory_soak.py`, because none of them can be inferred from
    the numbers this function returns: a dropped filter, a flipped sort and a
    removed limit all come back the same length, and a battery that reads the
    OLDEST 200 documents of a growing collection has stopped being a soak of
    the live read path.
    """
    probes: list[tuple[str, int]] = []
    materialized = 0

    # Projected tuple read with a sort and a limit -- the commonest shape.
    # Descending, not ascending: natural order and an ascending date both hand
    # back the OLDEST rows of a collection that grows all day, which is a
    # sample of the past dressed as a sample.
    rows = mongo_query.find_rows(
        "pipeline_events", {"phase": "analyzing"},
        ["cycle_id", "step", "status", "elapsed_ms"],
        sort=[("timestamp", -1)], limit=200)
    probes.append(("find_rows/pipeline_events", len(rows)))
    materialized += len(rows)

    # Whole documents -- the heavier per-row allocation.
    docs = mongo_query.find_dicts(
        "agent_tool_telemetry", {"success": True},
        sort=[("created_at", -1)], limit=100)
    probes.append(("find_dicts/agent_tool_telemetry", len(docs)))
    materialized += len(docs)

    # Aggregate over a filtered scan. `event_type`, not `severity`: the stored
    # severities are 'critical'/'info'/'warning' and never 'error'.
    total, distinct_cycles = mongo_query.agg_row(
        "cycle_audit_log", {"event_type": "error"},
        [("count", None), ("count_distinct", "cycle_id")])
    probes.append(("agg_row/cycle_audit_log", int(total or 0)))
    probes.append(("agg_row/cycle_audit_log:distinct", int(distinct_cycles or 0)))

    # Grouped top-N -- a $group pipeline with a sort on the aggregate.
    top = mongo_query.group_rows(
        "agent_tool_telemetry", {"success": True}, ["agent_name"],
        [("count", None)], [("key", "agent_name"), ("agg", 0)],
        sort=[("a0", -1)], limit=10)
    probes.append(("group_rows/agent_tool_telemetry", len(top)))
    materialized += len(top)

    # A plain count over a large collection, on a filter that can go to zero.
    n_news = mongo_query.count("news_articles", {"quality_status": "ok"})
    probes.append(("count/news_articles", n_news))

    # One row, one value, one existence probe: the three cheap seam readers.
    # They allocate almost nothing, which is the point -- a leak in the
    # per-call machinery shows up here without the row volume masking it.
    newest_error = mongo_query.find_row(
        "cycle_audit_log", {"event_type": "error"}, ["cycle_id", "message"],
        sort=[("timestamp", -1)])
    probes.append(("find_row/cycle_audit_log", 1 if newest_error else 0))
    materialized += 1 if newest_error else 0

    newest_headline = mongo_query.scalar(
        "news_articles", {"quality_status": "ok"}, "title",
        sort=[("published_at", -1)])
    probes.append(("scalar/news_articles", 1 if newest_headline else 0))
    materialized += 1 if newest_headline else 0

    any_failure = mongo_query.exists("agent_tool_telemetry", {"success": False})
    probes.append(("exists/agent_tool_telemetry", int(any_failure)))

    return probes, materialized


async def memory_soak_test(iterations: int = DEFAULT_ITERATIONS,
                           leak_mb: float = DEFAULT_LEAK_MB) -> bool:
    """Run N consecutive read batteries and measure memory usage over time.

    Returns True when growth stayed under `leak_mb` and every iteration read
    something; False otherwise, including when there was nothing to measure.
    """
    if iterations < 1:
        logger.error(
            f"Refusing to soak for {iterations} iterations. A run of fewer "
            "than one battery opens no connection, reads no documents and "
            "allocates nothing, so it cannot leak and cannot report anything "
            "about the read path -- it would exit 0 having measured nothing, "
            "which is the one result this instrument exists to not produce.")
        return False

    logger.info(f"Starting memory soak test for {iterations} iterations...")
    baseline_mem = _rss_mb()

    logger.info(f"Baseline memory: {baseline_mem:.2f} MB")

    for i in range(iterations):
        start_t = time.monotonic()
        try:
            probes, materialized = _read_battery()
        except Exception as e:
            # Deterministic reads: an exception is a fault, not flake. The old
            # loop swallowed these and still reported success at the end.
            logger.error(f"Iteration {i+1} failed: {e}")
            return False

        empty = [name for name, n in probes if n == 0]
        if empty:
            logger.error(
                f"Iteration {i+1}: read battery returned NOTHING for "
                f"{', '.join(empty)}. An empty workload allocates nothing and "
                "cannot leak, so this run would report clean while measuring "
                "no memory at all. Fix the probe, do not lower the bar.")
            return False

        elapsed = time.monotonic() - start_t

        # Force garbage collection to measure true leak, not just lazy GC
        gc.collect()

        current_mem = _rss_mb()
        growth = current_mem - baseline_mem
        logger.info(
            f"Iteration {i+1}/{iterations} completed in {elapsed:.1f}s. "
            f"Memory: {current_mem:.2f} MB (Growth: {growth:+.2f} MB) "
            f"[{materialized} docs]")

        if growth > leak_mb:
            logger.error(
                f"SEVERE MEMORY LEAK DETECTED: Growth reached {growth:.2f} MB "
                f"after {i+1} iterations.")
            return False

    logger.info("Memory soak test completed.")
    return True


def _build_parser() -> argparse.ArgumentParser:
    """The CLI, as a function so its defaults can be asserted.

    Built here rather than inline in `main()` because the defaults ARE the
    contract -- an unattended run is `--iterations 100 --leak-mb 500` -- and a
    default nothing reads is a default nothing notices changing.
    """
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS,
                    help=f"read batteries to run (default: {DEFAULT_ITERATIONS})")
    ap.add_argument("--leak-mb", type=float, default=DEFAULT_LEAK_MB,
                    help=f"RSS growth in MB reported as a leak (default: {DEFAULT_LEAK_MB})")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    ok = asyncio.run(memory_soak_test(iterations=args.iterations,
                                      leak_mb=args.leak_mb))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
