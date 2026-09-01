"""One definition of "this cycle_id belongs to a test run", for every live reader.

WHY THIS EXISTS. `scripts/observe_cycle.py` runs a REAL cycle in the deployed
container with order placement disabled — that is the whole point of it, and it
has behaved that way since 2026-07-25. What nobody had decided is what the rest
of the system should do with the rows it leaves behind.

The 2026-08-31 audit ladder answered that by accident. Four observe cycles wrote
14 trade_results / 14 shared_desk / 20 decision_scores / 13 decision_outcomes /
12 episodic_memory / 159 whiteboard_entries rows, and the ladder's contamination
probe could not see any of it: `measurement_window_report.py` counts only ids
starting `cycle-v3-`, so a flat counter was evidence about the FILTER, not about
the system. Meanwhile the live readers had no filter at all:

  * `load_latest_desk_for_ticker` reads "regardless of cycle_id", so the next
    production NVDA/JPM/MP desk would have been handed an observe desk as its
    "Previous Cycle's SharedDesk (Manila Envelope)" — injected into EVERY agent
    prompt — and would have triaged on its age. This already chained inside the
    ladder: MP's "Age: 2h" prior desk was itself an observe desk, which is why
    MP took the cheap delta path instead of the full panel its real 490-hour-old
    production desk would have forced.
  * the data-report 48h fast path would have seeded production prompts with an
    observe thesis AND skipped fundamentals / multi-API news / reddit / youtube.
  * the watch desk armed LIVE price triggers from observe levels, and a trip
    enqueues START_V3_CYCLE with `"trade": True`.
  * `record_cycle_decisions` inserted 13 observe HOLDs into decision_outcomes,
    which `resolve_pending_outcomes` would have resolved ~7 days later into the
    hold-accuracy and calibration-ECE cohorts.

WHICH DIRECTION EACH PREDICATE FAILS. `is_production_cycle` is an ALLOWLIST
(`cycle-v3-`), matching `scripts/hold_wall_report.py`; there is exactly one
production minter — `pipeline_service.py`'s `f"cycle-v3-{int(time.time())}"` —
and `test_cycle_scope.py` parses the tree to keep that true. `is_synthetic_cycle`
is a DENYLIST of the prefixes we actually mint, so a row with an unrecognised or
absent cycle_id is treated as REAL and stays visible to live readers. That is
the safe direction for a reader: the cost of a mistake is showing a real row, not
silently dropping one. An allowlist here would hide every legacy row that
predates cycle ids.

Adding a new test harness means adding its prefix HERE, not writing a fifth
prefix check at the call site — the audit found the same rule re-derived at
eight sites with three different spellings.
"""

from __future__ import annotations

#: Every prefix this repo mints for a run that is NOT the production desk.
#: Sources, all verified 2026-09-01:
#:   scripts/observe_cycle.py   -> cycle-observe-<epoch>
#:   scripts/bench_stage.py     -> bench-<ticker>-<epoch>
#:   scripts/trigger_canary.py  -> canary_v3_<hex>
#:   app/v3/challenger.py       -> challenger-<cycle_id>
#: plus prefixes present in the live store from older harnesses.
SYNTHETIC_CYCLE_PREFIXES: tuple[str, ...] = (
    "cycle-observe-",
    "cycle-smoke",
    "cycle-verify",
    "cycle-guard",
    "cycle-audit-",
    "cycle-v3-audit-",
    "test-cycle",
    "canary_",
    "bench-",
    "challenger-",
    # Found by test_synthetic_cycles_stay_out_of_live_readers' AST scan of the
    # tree, not by anyone remembering them — which is the point of that guard.
    "variance-",            # app/autoresearch/variance.py, a re-run of a desk
    "ondemand-chart-",      # app/routers/chart_router.py, a chart render
    "sc-",                  # scripts/self_consistency_bench.py
    "stress-concurrency-",  # scripts/stress_tests/concurrency_test.py
)

#: The one prefix the production pipeline mints.
PRODUCTION_CYCLE_PREFIX = "cycle-v3-"


def is_production_cycle(cycle_id: object) -> bool:
    """True only for the ids `pipeline_service` mints for a real desk.

    Same predicate as `scripts/hold_wall_report._is_production_cycle`, kept
    here so app-side readers and the reports agree by construction.
    """
    cid = str(cycle_id or "")
    return cid.startswith(PRODUCTION_CYCLE_PREFIX) and not cid.startswith("cycle-v3-audit-")


def is_synthetic_cycle(cycle_id: object) -> bool:
    """True for a cycle_id this repo mints for a test / benchmark / canary run.

    An empty, missing or unrecognised id is NOT synthetic — see the module
    docstring on which direction each predicate fails.
    """
    cid = str(cycle_id or "")
    if not cid:
        return False
    return cid.startswith(SYNTHETIC_CYCLE_PREFIXES)


def exclude_synthetic(field: str = "cycle_id") -> dict:
    """A Mongo clause dropping synthetic rows, for pushing into a live read.

    `$not` + `$regex` also matches documents where the field is MISSING or
    null, which is what we want: legacy rows without a cycle_id stay visible.
    """
    pattern = "^(" + "|".join(SYNTHETIC_CYCLE_PREFIXES) + ")"
    return {field: {"$not": {"$regex": pattern}}}
