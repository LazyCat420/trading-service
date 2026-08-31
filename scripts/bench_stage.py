#!/usr/bin/env python3
"""Run ONE stage of the V3 cycle, on ONE ticker, in a read-only sandbox.

    python3 scripts/bench_stage.py --list
    python3 scripts/bench_stage.py policy_gates --ticker AAPL
    python3 scripts/bench_stage.py data_report --ticker AAPL --repeat 5
    python3 scripts/bench_stage.py junior --ticker AAPL          # one LLM agent
    python3 scripts/bench_stage.py --all-context --ticker AAPL --json out.json

WHY THIS EXISTS
===============
A full wide cycle is the only thing this repo had that exercised the pipeline,
and it costs 20-60 minutes, real LLM tokens on shared hardware, and a live
`pipeline_state` claim that a parallel session's deploy can destroy. That made
the feedback loop for "did my prompt/collector/gate change work" longer than the
change itself — which is how a defect like the dead `degradation_note` artifact
type survived from 2026-07-28 to 2026-08-10.

This runs one stage against one ticker in seconds-to-a-minute and answers a
narrower question honestly, so a full cycle is a confirmation rather than a
probe.

WHAT MAKES IT A SANDBOX
=======================
1. **The store is READ ONLY.** Every mutating method on a pymongo
   `Collection`/`Database` is blocked for the life of the process, so an
   accidental write raises `SandboxWriteBlocked` instead of putting a fake
   desk, a fake telemetry row, a fake whiteboard entry or a fake decision into
   the collections the audits read. Pass `--allow-writes` only when you
   specifically want the stage's persistence path exercised, and expect rows.

   Until 2026-08-30 this guard patched the archive connection module's
   `get_db` to issue `SET default_transaction_read_only = on`, and it has
   answered nothing since the 2026-08-19 cutover. That seam is empty: the
   cutover left `app/` with **zero** live `get_db()` call sites and **312**
   `mongo_store` write calls, so the header printed `db=READ-ONLY` over a
   completely open write path. `bench_stage quant -t AAPL` really did append
   rows to `whiteboard_entries` (`app/agents/whiteboard.py:108`) on every run.
   The guard is now installed at the pymongo class, which is the only level
   that catches all three write routes — the `mongo_store` helpers,
   `mongo_store._coll(t).bulk_write(...)` (`app/analytics/returns_engine.py:414`)
   and `vector_store._mongo_coll()`. The footer NAMES the writes it blocked,
   so the guard is a measurement rather than a claim — a first live run on
   AAPL blocked 8, into `price_history`, `technicals`, `data_source_status`,
   `watch_events` and `v3_guardrail_firings`.

   Expect a blocked write to show up as a warning inside a stage: the
   collectors cache what they fetch, so `data_report` logs
   `AAPL/yfinance_price failed: SandboxWriteBlocked` and falls back to what is
   already stored. That is the sandbox working, and it is why `--allow-writes`
   exists — but it does mean a read-only `data_report` measures the CACHED
   path, not the fetch-and-persist one.
2. **It never claims a cycle.** `pipeline_state` is not touched and no
   `START_CYCLE` command is queued, so this cannot deduplicate against, stall,
   or be killed by the real scheduler. The cycle id is stamped `bench-*` so any
   row that does escape is identifiable and excludable.
3. **It never trades.** No stage here reaches the execution path.

WHAT IT IS NOT
==============
Not a reliability measurement and not a benchmark of the boxes. The LLM stages
talk to the same shared Jetson / Gold Spark as production, so a timing taken
while a real cycle is running is not a datapoint — the header prints whether a
cycle is live so you can throw the number away. For box throughput use
`scripts/jetson_benchmark.py`, which is built for it.

Not a correctness oracle either. A stage `PASS` means "produced output that
satisfies its contract", which is the cheap half. `--repeat` gives you a median
and a spread rather than one sample, because one timing on a shared box is not
a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Staging mode skips the production-API-key assertion; the sandbox never trades.
os.environ.setdefault("EXECUTION_MODE", "staging")


# ─────────────────────────────────────────────────────────────────────
# Read-only guard
# ─────────────────────────────────────────────────────────────────────

class SandboxWriteBlocked(RuntimeError):
    """A stage tried to write to MongoDB while the sandbox was read-only."""


# Every MUTATING method on a pymongo Collection, plus the two catalog verbs on
# Database and the one on MongoClient. Reads (`find`, `find_one`, `aggregate`,
# `count_documents`, `distinct`, `list_indexes`, `watch`) are deliberately
# absent — this must not narrow what the stages can measure.
_COLLECTION_WRITES = (
    "insert_one", "insert_many",
    "update_one", "update_many", "replace_one",
    "delete_one", "delete_many",
    "bulk_write",
    "find_one_and_update", "find_one_and_replace", "find_one_and_delete",
    "drop", "rename",
    "create_index", "create_indexes", "drop_index", "drop_indexes",
)
_DATABASE_WRITES = ("create_collection", "drop_collection")
_CLIENT_WRITES = ("drop_database",)

# Every blocked call, as "collection.method", in order. Printed in the footer:
# a guard whose number never moves is a guard nobody can tell is installed.
BLOCKED_WRITES: list[str] = []


# ── Index-DDL latches ────────────────────────────────────────────────
# Index creation is DDL, so the guard blocks it, so it lands in BLOCKED_WRITES
# and buries the writes the footer exists to name. Every "have I ensured my
# indexes yet" latch in `app/` is therefore pinned True before any stage runs.
#
# `carries` is the (file, attribute) each pin covers, so a test can compare the
# pinned set against the latches actually present in `app/` — in BOTH
# directions. This defect class has already bitten once: the first version of
# the guard pinned `mongo_store` only, and `VectorStore`'s latch (which flips
# only on SUCCESS, inside a swallow-everything `except`) retried its DDL on
# every single call, adding an unbounded `embeddings.create_index` to the count.

def _pin_mongo_store_indexes() -> None:
    from app.db import mongo_store

    mongo_store._indexes_ready = True


_pin_mongo_store_indexes.carries = ("app/db/mongo_store.py", "_indexes_ready")


def _pin_vector_store_indexes() -> None:
    from app.db.vector_store import VectorStore

    # The CLASS attribute, which is where it lives — the module also exposes a
    # `vector_store` singleton, and setting the instance would leave the class
    # default False for anything that builds its own VectorStore.
    VectorStore._mongo_indexes_ready = True


_pin_vector_store_indexes.carries = ("app/db/vector_store.py", "_mongo_indexes_ready")

_INDEX_LATCHES = (_pin_mongo_store_indexes, _pin_vector_store_indexes)


def install_read_only_db() -> None:
    """Block every MongoDB write for the life of this process.

    Installed on the pymongo CLASSES, not on `app.db.mongo_store`, for the same
    reason the old version patched `get_db` on its module rather than on each
    caller: the guard has to sit at the one place every route converges. There
    are three routes here and only one of them goes through a `mongo_store`
    helper — `app/analytics/returns_engine.py:414` calls
    `mongo_store._coll(...).bulk_write(...)` directly, and
    `app/db/vector_store.py:197,428` calls `bulk_write`/`delete_many` on its own
    handle. Patching the helpers would have left both of those open while
    reporting READ-ONLY, which is the exact shape of the bug this replaces.

    Idempotent: a second call is a no-op, so `--allow-writes` off/on inside one
    process cannot double-wrap.

    EVERY index-DDL latch in the codebase is pinned as part of installing,
    because a write helper calls its index-ensure first and that DDL would
    otherwise land in `BLOCKED_WRITES` before the real write was even
    attempted — burying the one number that matters under catalog noise. Index
    DDL is skipped, not performed: this is a read-only sandbox and it has no
    business creating collections either.

    There are TWO latches and they behave differently, which is why the first
    version of this guard fixed one and left the other flooding:

      `mongo_store._indexes_ready`        set unconditionally by `ensure_indexes()`
      `VectorStore._mongo_indexes_ready`  set ONLY on success (`vector_store.py:88`)

    The second one is inside a `try/except` that logs and continues, so under
    the guard `create_index` raises, the latch never flips, and EVERY later
    `VectorStore._mongo_coll()` retries the DDL. Measured in-process with the
    guard proved installed: 3 calls -> `BLOCKED_WRITES == ['embeddings.create_index'] * 3`,
    latch still False — unbounded on any embeddings/RAG-touching agent stage,
    and it corrupts both the footer line and the `blocked_writes` JSON key,
    which are the port's only evidence that the sandbox is real. Pinning it is
    what makes the count "the writes the sandbox actually stopped".

    Each latch is pinned in its OWN try/except: one module failing to import
    must not skip the other, and neither may take the write block down with it.
    """
    import pymongo

    if getattr(pymongo.collection.Collection, "_bench_read_only", False):
        return

    def _blocker(cls_label: str, method: str):
        def _raise(self, *_a, **_kw):
            # `isinstance(..., str)` and not a bare getattr default: pymongo's
            # `MongoClient.__getattr__` resolves ANY unknown attribute to a
            # Database, so `getattr(client, "name", cls_label)` hands back a
            # Database object rather than falling through to the default, and
            # the blocked-write line would read `Database(...).drop_database`.
            name = getattr(self, "name", None)
            where = f"{name if isinstance(name, str) else cls_label}.{method}"
            BLOCKED_WRITES.append(where)
            raise SandboxWriteBlocked(
                f"read-only sandbox: blocked {where}(). Re-run with "
                f"--allow-writes if you meant to exercise the persistence path."
            )
        return _raise

    for cls, methods, label in (
        (pymongo.collection.Collection, _COLLECTION_WRITES, "collection"),
        (pymongo.database.Database, _DATABASE_WRITES, "database"),
        (pymongo.MongoClient, _CLIENT_WRITES, "client"),
    ):
        for name in methods:
            if hasattr(cls, name):
                setattr(cls, name, _blocker(label, name))

    pymongo.collection.Collection._bench_read_only = True

    for pin in _INDEX_LATCHES:
        try:
            pin()
        except Exception:  # noqa: BLE001 - the write block is what matters
            # Independently, so a module that fails to import cannot skip the
            # next latch and quietly restore the flood it was meant to stop.
            pass


# The repo's ONE definition of "this cycle is over". Stated negatively, exactly
# as `cycle_scheduler.py:313,1790`, `watch_desk.py:833`,
# `smoke_test_cycle.py:64` and `smoke_test_streaming.py:484` state it, so five
# places cannot drift apart about whether a cycle is live.
#
# This used to be a POSITIVE allowlist here — ("running", "starting",
# "collecting", "analyzing", "trading") — and it had drifted in both
# directions: `collecting`/`analyzing`/`trading` are `pipeline_state.phase`
# values and are never written to `status` (grep: zero sites), while `stopping`
# (`pipeline_service.py:2705`) and `blocked` (`boot_service.py:240`) ARE live
# statuses and the list missed both. A cycle being stopped read as "no cycle",
# which is precisely when `--force` should have been required.
TERMINAL_CYCLE_STATUSES = ("idle", "done", "error", "stopped", "interrupted")


def live_cycle_id() -> str | None:
    """Return the cycle id of a running cycle, or None. Never raises.

    Reads MONGO. Postgres' `pipeline_state` froze at the 2026-08-19 cutover on
    `cycle-v3-1787179210 (done)` and has not moved since, so the Postgres
    version of this answered "no cycle is live" through the whole of every live
    cycle — the LLM-load refusal below could never fire, and every timing this
    tool printed was labelled clean.

    `pipeline_state` is a SINGLETON: one document, `singleton_id='current'`,
    overwritten in place. There is nothing to sort and nothing to sample.
    """
    try:
        from app.db import mongo_query

        row = mongo_query.find_row(
            "pipeline_state", {"singleton_id": "current"}, ["cycle_id", "status"]
        )
        if row and row[1] not in TERMINAL_CYCLE_STATUSES:
            return f"{row[0]} ({row[1]})"
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────
# Stage registry
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Stage:
    name: str
    group: str                       # "context" | "compute" | "agent" | "gate"
    run: Callable[["Ctx"], Any]      # sync or async; returns the stage output
    contract: Callable[[Any], str]   # "" when satisfied, else the reason it failed
    needs_llm: bool = False
    blurb: str = ""


@dataclass
class Ctx:
    """Everything a stage might need, built lazily and shared across repeats."""
    ticker: str
    cycle_id: str
    bot_id: str = "bench"
    _desk: Any = None
    notes: list[str] = field(default_factory=list)

    def desk(self):
        """A SharedDesk seeded from the cheap context blocks.

        Agent stages need a desk that looks like the one the orchestrator would
        hand them. This builds the same blocks the orchestrator builds, in the
        same order, but skips the parts that require a live cycle.
        """
        if self._desk is not None:
            return self._desk
        from app.v3.shared_desk import SharedDesk

        desk = SharedDesk(cycle_id=self.cycle_id, ticker=self.ticker)

        # REAL cycle_metadata, not an empty dict. A bare desk reports
        # `held=None` and carries no candidate pool, so every agent stage ran
        # in a state no production desk is ever in — and the two agents that
        # are SHOWN the pool (`v3_bear_agent`, `v3_board_of_directors`, see
        # `agent_runner.py:742`) could never be observed using it.
        #
        # The ACTIVE bot, not `self.bot_id`: that defaults to "bench", a bot
        # that owns nothing, so holdings-dependent context would be uniformly
        # empty and every held-name behaviour would be invisible.
        try:
            from app.services.bot_manager import get_active_bot_id
            from app.v3.orchestrator import _build_cycle_metadata
            from app.v3.substitute import POOL_KEY
            from app.v3.wake_pool import build_wake_pool, build_wake_pool_block

            bot_id = get_active_bot_id() or self.bot_id
            desk.cycle_metadata = _build_cycle_metadata(
                ticker=self.ticker, bot_id=bot_id, trigger_type="bench")
            if desk.cycle_metadata.get("held") is True and not desk.cycle_metadata.get(POOL_KEY):
                rec = build_wake_pool(self.ticker, exclude_cycle_id=self.cycle_id)
                block = build_wake_pool_block(rec, self_ticker=self.ticker)
                if block:
                    desk.cycle_metadata["cycle_candidates_context"] = block
                    desk.cycle_metadata[POOL_KEY] = list(rec["tickers"])
                self.notes.append(
                    f"held desk; wake pool n={len(rec['tickers'])} ({rec['reason']})")
            else:
                self.notes.append(
                    f"held={desk.cycle_metadata.get('held')!r}; no wake pool")
        except Exception as e:  # noqa: BLE001
            self.notes.append(f"cycle_metadata seed failed: {type(e).__name__}: {e}")

        self._desk = desk
        return desk


def _nonempty(min_chars: int, label: str) -> Callable[[Any], str]:
    def check(out: Any) -> str:
        if not isinstance(out, str):
            return f"{label}: expected a string, got {type(out).__name__}"
        if len(out.strip()) < min_chars:
            # An empty block is the pipeline's most common silent failure: the
            # stage "succeeds", the agent gets no data, and the desk holds a
            # confident decision built on nothing.
            return f"{label}: {len(out.strip())} chars — under the {min_chars}-char floor (empty block)"
        return ""
    return check


def build_registry() -> dict[str, Stage]:
    stages: dict[str, Stage] = {}

    def add(s: Stage) -> None:
        stages[s.name] = s

    # ── Context blocks: real network + DB reads, no LLM ──────────────
    async def _data_report(c: Ctx):
        from app.v3.data_report import build_ticker_data_report
        return await build_ticker_data_report(c.ticker, cycle_id=c.cycle_id)

    add(Stage("data_report", "context", _data_report, _nonempty(200, "data_report"),
              blurb="collectors + news + price/fundamentals text block"))

    def _quant_math(c: Ctx):
        from app.quant.context_block import build_quant_math_block
        return build_quant_math_block(c.ticker, bot_id=c.bot_id, cycle_id=c.cycle_id)

    add(Stage("quant_math", "compute", _quant_math, _nonempty(40, "quant_math"),
              blurb="GARCH + HRP/covariance + strategy health"))

    def _technical(c: Ctx):
        from app.quant.technical_baseline import build_technical_baseline_block
        return build_technical_baseline_block(c.ticker)

    add(Stage("technical", "compute", _technical, _nonempty(40, "technical"),
              blurb="code-computed technical baseline"))

    def _valuation(c: Ctx):
        from app.quant.valuation_block import build_valuation_block
        return build_valuation_block(c.ticker)

    add(Stage("valuation", "compute", _valuation, _nonempty(40, "valuation"),
              blurb="valuation block"))

    def _fundamental(c: Ctx):
        from app.quant.fundamental_block import build_fundamental_block
        return build_fundamental_block(c.ticker)

    add(Stage("fundamental", "compute", _fundamental, _nonempty(40, "fundamental"),
              blurb="precomputed fundamental snapshot"))

    # ── Memory + whiteboard: the two prompt channels the registry missed ──
    # (2026-08-31: the memory seam was silently dead 08-18..08-31 — a ghost
    # import raised on every retrieve and nothing off the live path exercised
    # it. A stage that RUNS the real retriever turns that class of break red.)
    def _memory_retrieval(c: Ctx):
        from app.services.memory.retriever import MemoryRetriever
        from app.services.retrieval_context import build_memory_addenda
        results = MemoryRetriever.retrieve(ticker=c.ticker)
        brief = ""
        if results:
            brief = MemoryRetriever.build_memory_brief(results).get("brief_text", "")
        addenda = build_memory_addenda(c.ticker)
        combined = "\n\n".join(b for b in (brief, addenda) if b)
        c.notes.append(
            f"memory: {len(results or [])} canonical candidates, {len(combined)} chars injected")
        return combined

    def _memory_contract(out) -> str:
        # Empty is legal (a ticker may have no memories); an exception is the
        # failure mode this stage exists to catch, and Stage machinery already
        # fails on raise. Only the shape is asserted here.
        return "" if isinstance(out, str) else f"expected str, got {type(out).__name__}"

    add(Stage("memory_retrieval", "context", _memory_retrieval, _memory_contract,
              blurb="canonical brief + episodic/working-memory addenda (the real retriever)"))

    async def _whiteboard_build(c: Ctx):
        from app.db import mongo_store
        from app.agents.whiteboard import whiteboard
        # A bench cycle writes no board, so replay delivery for the most recent
        # REAL cycle that has entries for this ticker — summarize() is exactly
        # what agent_runner injects (for_agent_prompt=True).
        docs = mongo_store.find_docs(
            "whiteboard_entries", {"ticker": c.ticker.upper()},
            sort=[("created_at", -1)], limit=1)
        if not docs:
            c.notes.append("whiteboard: no entries on record for this ticker")
            return "NO_ENTRIES"
        cyc = docs[0].get("cycle_id") or "default_cycle"
        out = await whiteboard.summarize(c.ticker, cyc, for_agent_prompt=True)
        c.notes.append(f"whiteboard: replayed {cyc}, delivered {len(out)} chars")
        return out

    def _whiteboard_contract(out) -> str:
        if not isinstance(out, str):
            return f"expected str, got {type(out).__name__}"
        if out == "NO_ENTRIES":
            return ""
        if len(out.strip()) < 50:
            return ("entries exist but summarize() delivered "
                    f"{len(out.strip())} chars — the board is not reaching prompts")
        return ""

    add(Stage("whiteboard_build", "context", _whiteboard_build, _whiteboard_contract,
              blurb="replay agent-prompt whiteboard delivery for the latest real cycle"))

    # ── Policy gate: pure logic on a synthetic desk, sub-millisecond ──
    def _policy_gates(c: Ctx):
        from app.v3.orchestrator import _apply_policy_gates
        from app.v3.shared_desk import SharedDesk

        # The gate reads `desk.trade_decision or desk.final_decision` — the
        # DICT, not the scalar `final_action`/`final_confidence` attributes.
        # Setting the scalars leaves `decision` empty, which resolves to
        # action="HOLD" and returns HOLD_NO_SIGNAL at every confidence. A probe
        # shaped that way reports a broken gate on working code.
        results = {}
        for conf in (95, 75, 69, 40):
            desk = SharedDesk(cycle_id=c.cycle_id, ticker=c.ticker)
            desk.final_decision = {
                "action": "BUY",
                "confidence": conf,
                "decision_provenance": "board_reasoned",
            }
            results[conf] = _apply_policy_gates(desk)
        return results

    def _gate_contract(out: Any) -> str:
        if not isinstance(out, dict):
            return f"expected a dict of confidence->gate, got {type(out).__name__}"
        # A gate that returns the same verdict at 95 and at 40 is not a gate.
        if out.get(95) == out.get(40):
            return (
                f"the floor never fired: confidence 95 and 40 both returned "
                f"{out.get(95)!r} — this gate is not discriminating"
            )
        return ""

    add(Stage("policy_gates", "gate", _policy_gates, _gate_contract,
              blurb="confidence floor / policy gate, probed at 95/75/69/40"))

    # ── The position branch: does a HELD name reach the bear with a pool? ──
    def _wake_pool(c: Ctx):
        """Replays the orchestrator's guard verbatim, against LIVE holdings.

        This is the stage the 2026-08-12 audit had no way to run. The wake pool
        only fires on a HELD re-look, and a *discovery* cycle selects unheld
        names — so a full cycle can confirm the label path and can never
        exercise this one. Measured on cycle-v3-1786564552: 6 desks, 0 held.
        """
        from app.v3.orchestrator import _build_cycle_metadata
        from app.v3.substitute import POOL_KEY
        from app.v3.wake_pool import build_wake_pool, build_wake_pool_block

        # THE ACTIVE BOT, not `c.bot_id`. That defaults to "bench" — a bot
        # that owns nothing — so every ticker reads held=False and this stage
        # would report a green "guard correctly did not fire" while testing
        # nothing at all. Holdings ARE the subject here. (bot_id resolution has
        # burned this repo before: when it broke in 07-24 it read False for
        # every ticker including ones the desk genuinely owned.)
        from app.services.bot_manager import get_active_bot_id
        bot_id = get_active_bot_id() or c.bot_id
        meta = _build_cycle_metadata(ticker=c.ticker, bot_id=bot_id,
                                     trigger_type="bench")
        held = meta.get("held")
        fired = held is True and not meta.get(POOL_KEY)
        out = {
            "bot_id": bot_id,
            "held": held,
            "guard_fired": fired,
            # Recorded because the whole defect class here is a prose key being
            # read as a mapping: `portfolio_context.get("held")` raises on a str.
            "portfolio_context_type": type(meta.get("portfolio_context")).__name__,
            "position_is_structured": isinstance(meta.get("position"), dict),
            "pool": [], "reason": None, "block_chars": 0,
            "self_in_pool": False, "asks_for_substitute": False,
        }
        if fired:
            rec = build_wake_pool(c.ticker, exclude_cycle_id=c.cycle_id)
            block = build_wake_pool_block(rec, self_ticker=c.ticker)
            out.update(
                pool=rec["tickers"], reason=rec["reason"],
                source_cycle=rec["cycle_id"], age_hours=rec["age_hours"],
                block_chars=len(block),
                self_in_pool=c.ticker.upper() in rec["tickers"],
                asks_for_substitute=(
                    "only actionable on this book if it names something better"
                    in block),
            )
        return out

    def _wake_pool_contract(out: Any) -> str:
        if not isinstance(out, dict):
            return f"expected a dict, got {type(out).__name__}"
        # The prose/mapping trap, asserted rather than remembered.
        if out.get("portfolio_context_type") != "str":
            return ("portfolio_context is no longer a str — re-check every "
                    "reader; `.get('held')` on it used to raise into a blanket "
                    "except and drop the label silently")
        if out.get("held") is not True:
            # Not a failure: an unheld ticker SHOULD NOT fire the guard. Say so
            # rather than passing silently, so nobody reads a green run on an
            # unheld ticker as proof the pool works.
            return ("NOT A TEST OF THIS STAGE: this ticker is not held "
                    f"(held={out.get('held')!r}), so the guard correctly did not "
                    "fire. Re-run with a ticker the book actually owns.")
        # Only meaningful once we know the desk IS held — `position` is
        # legitimately absent on an unheld desk.
        if not out.get("position_is_structured"):
            return "cycle_metadata['position'] is not a dict — the structured fallback is gone"
        if not out.get("guard_fired"):
            return "held ticker but the guard did not fire — the pool is unreachable"
        if not out.get("pool"):
            return (f"guard fired but no pool was borrowable (reason="
                    f"{out.get('reason')!r}) — the bear still cannot be asked")
        if out.get("self_in_pool"):
            return "the ticker being re-looked at is in its own substitute pool"
        if not out.get("asks_for_substitute"):
            return ("the block renders but does not ask for a substitute — the "
                    "two populations are answering different questions")
        return ""

    add(Stage("wake_pool", "gate", _wake_pool, _wake_pool_contract,
              blurb="HELD name -> borrowed candidate pool (needs a held ticker)"))

    # ── LLM agents ───────────────────────────────────────────────────
    agent_specs = [
        ("regime", "regime_engine", "market regime label"),
        ("junior", "junior_analyst", "baseline research desk_note"),
        ("fundamental_agent", "fundamental_analyst", "fundamental thesis"),
        ("quant", "quant_analyst", "quant signals + overlays"),
        ("valuation_agent", "valuation_analyst", "valuation read"),
        ("bull", "bull_agent", "bull case"),
        ("bear", "bear_agent", "bear case"),
        ("defense", "bull_defense", "bull rebuttal"),
        ("judge", "debate_judge", "debate verdict"),
        ("board", "board_of_directors", "board decision"),
        ("decision", "decision_agent", "final action + confidence"),
    ]

    def _make_agent(module_name: str):
        async def _run(c: Ctx):
            import importlib

            from app.v3.agent_runner import run_v3_agent
            from app.v3.shared_desk import _VALID_ARTIFACT_TYPES

            def _present(desk) -> set[str]:
                # `append_artifact` ends in `setattr(desk, artifact_type, ...)`,
                # so the artifact type IS the attribute name — there is no
                # `desk.artifacts` list to count.
                return {t for t in _VALID_ARTIFACT_TYPES if getattr(desk, t, None)}

            module = importlib.import_module(f"app.v3.agents.{module_name}")
            desk = c.desk()
            before = _present(desk)
            outcome = await run_v3_agent(
                desk, module, cycle_id=c.cycle_id, bot_id=c.bot_id,
            )
            gained = sorted(_present(desk) - before)
            return {
                "outcome": str(outcome),
                "artifacts_added": len(gained),
                "artifacts": ",".join(gained) or "-",
            }
        return _run

    def _agent_contract(out: Any) -> str:
        if not isinstance(out, dict):
            return f"expected a dict, got {type(out).__name__}"
        if "SUCCESS" not in out.get("outcome", "").upper():
            return f"outcome={out.get('outcome')!r}"
        # A SUCCESS that appended nothing is the failure shape this system
        # produces most often: the agent ran, the artifact was rejected, and
        # the desk moved on with no evidence. See the degradation_note defect.
        if out.get("artifacts_added", 0) < 1:
            return "outcome was SUCCESS but the desk gained no artifact"
        return ""

    # ── Did the bear ANSWER the pool it was shown? ───────────────────
    def _substitute_ask(c: Ctx):
        """The end-to-end question the wake pool exists for.

        `wake_pool` proves the block is BUILT. `agent_runner.py:742` shows it
        reaches `v3_bear_agent` under `_KEEP` (never shed). This stage is the
        only one that answers the remaining question: shown that block, does
        the bear actually name a name?

        RUN IT AFTER `bull` — `bench_stage bull bear substitute_ask -t <held>`
        — because stages share one `Ctx.desk()` and the bear rebuts the bull.
        This stage runs no model itself; it reads back what the bear left.
        """
        from app.v3.substitute import POOL_KEY, read_record

        from app.v3.hold_reason import classify_hold

        desk = c.desk()
        rec = read_record(desk) or {}
        # THE LAST LINK. A NAMED substitute is only worth having if it reaches
        # the label — that is the whole chain Open Item 46 is about:
        #   held re-look -> borrowed pool -> bear asked -> NAMED -> EXIT_SIGNALLED
        # Reported here so nobody has to infer the final step from the first four.
        label = classify_hold(desk, "HOLD") or {}
        return {
            "held": desk.cycle_metadata.get("held"),
            "pool_size": len(desk.cycle_metadata.get(POOL_KEY) or []),
            "bear_ran": bool(getattr(desk, "bear_rebuttal", None)),
            "status": rec.get("status"),
            "ticker": rec.get("ticker"),
            "hold_reason": label.get("hold_reason"),
            "basis": label.get("basis"),
        }

    def _substitute_contract(out: Any) -> str:
        if not isinstance(out, dict):
            return f"expected a dict, got {type(out).__name__}"
        if out.get("held") is not True:
            return ("NOT A TEST OF THIS STAGE: ticker is not held "
                    f"(held={out.get('held')!r}) — re-run on a name the book owns")
        if not out.get("pool_size"):
            return "no pool on the desk — the bear had nothing to be asked about"
        if not out.get("bear_ran"):
            return ("the bear has not run on this desk — this stage reads back "
                    "its answer, it does not produce one. Run: "
                    "`bench_stage bull bear substitute_ask -t <held>`")
        status = out.get("status")
        if status in (None, "NOT_ASKED"):
            return (f"status={status!r} with a pool of {out['pool_size']} on the "
                    "desk — the bear was shown alternatives and the record still "
                    "says it was not asked. THIS IS THE DEFECT THE WAKE POOL "
                    "EXISTS TO FIX; the block is not reaching the prompt.")
        if status == "UNANSWERED":
            return ("status=UNANSWERED — the bear was asked and ignored the "
                    "question. An engagement failure, not an answer.")
        if status == "OFF_POOL":
            return ("status=OFF_POOL — the bear named something it was not "
                    "shown, which the desk cannot price.")
        # NAMED and DECLINED are BOTH successes: "none is better" is a real
        # answer, and treating it as failure would train the bear to invent a
        # preference it does not hold.
        #
        # But the label must have MOVED. A held desk that still reads WATCH or
        # AVOID after all of this is the original defect surviving the fix.
        label = out.get("hold_reason")
        if label in ("WATCH", "AVOID"):
            return (f"the bear answered ({status}) but the label is still "
                    f"{label!r} — the ENTRY vocabulary, on a name we own. This "
                    "is Open Item 46 surviving its own fix.")
        if status == "NAMED" and label != "EXIT_SIGNALLED":
            return (f"substitute NAMED on a held desk but hold_reason={label!r}, "
                    "expected EXIT_SIGNALLED")
        return ""

    # GROUP "agent", not "gate", and the group is what orders execution:
    # `main` runs ("context","compute","gate","agent") in that order and filters
    # `ordered` by group, so a gate stage ALWAYS runs before every agent no
    # matter what the command line says. This stage READS BACK what the bear
    # left, so in the gate group it could only ever report `bear_ran=False` —
    # which is exactly what it did on its first live run. `needs_llm` stays
    # False: it runs no model itself, so `--all-agents` will not sweep it up.
    add(Stage("substitute_ask", "agent", _substitute_ask, _substitute_contract,
              blurb="did the bear answer the pool? (run AFTER bull+bear, same invocation)"))

    for name, module_name, blurb in agent_specs:
        add(Stage(name, "agent", _make_agent(module_name), _agent_contract,
                  needs_llm=True, blurb=blurb))

    return stages


# ─────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────

async def run_stage(stage: Stage, ctx: Ctx, repeat: int) -> dict:
    timings: list[float] = []
    last_out: Any = None
    failures: list[str] = []

    for i in range(repeat):
        t0 = time.monotonic()
        try:
            out = stage.run(ctx)
            if asyncio.iscoroutine(out):
                out = await out
            elapsed = time.monotonic() - t0
            reason = stage.contract(out)
            last_out = out
            if reason:
                failures.append(f"run {i + 1}: {reason}")
        except Exception as e:
            elapsed = time.monotonic() - t0
            failures.append(f"run {i + 1}: raised {type(e).__name__}: {e}")
            if os.environ.get("BENCH_TRACE"):
                traceback.print_exc()
        timings.append(elapsed)

    ok = not failures
    result = {
        "stage": stage.name,
        "group": stage.group,
        "status": "PASS" if ok else "FAIL",
        "runs": repeat,
        "median_s": round(statistics.median(timings), 3),
        "min_s": round(min(timings), 3),
        "max_s": round(max(timings), 3),
        "failures": failures,
    }
    if isinstance(last_out, str):
        result["output_chars"] = len(last_out)
    elif isinstance(last_out, dict):
        result["output"] = {k: v for k, v in last_out.items() if not isinstance(v, (list, dict))}
    return result


def print_row(r: dict) -> None:
    mark = "✅" if r["status"] == "PASS" else "❌"
    spread = "" if r["runs"] == 1 else f"  ({r['min_s']:.2f}–{r['max_s']:.2f} over {r['runs']})"
    extra = ""
    if "output_chars" in r:
        extra = f"  {r['output_chars']:,} chars"
    elif "output" in r:
        extra = "  " + " ".join(f"{k}={v}" for k, v in r["output"].items())
    print(f"  {mark} {r['stage']:<20s} {r['median_s']:7.2f}s{spread}{extra}", flush=True)
    for f in r["failures"]:
        print(f"        ↳ {f}", flush=True)


def compare_runs(base: dict, now: dict) -> int:
    """Diff two bench runs. Returns the number of CONTRACT regressions.

    Two kinds of change, deliberately weighted very differently:

    **Contract PASS -> FAIL is a hard failure.** It is a behavioural statement
    that does not depend on how loaded the box was.

    **A timing change is not, unless it is enormous AND both runs were clean.**
    This box is shared: `pk-run.sh`-style budgets, parallel sessions, and a live
    trading cycle all move wall-clock by more than any code change here would.
    Measured on this repo: unit-test classes SHRINK on a busy box rather than
    fail. A benchmark that fails on a 20% timing move on a loaded machine
    trains people to ignore it, which is worse than not having it.

    So timings are always PRINTED and only ever FAIL when both runs were taken
    with no live cycle and the slowdown is past `_TIMING_FAIL_RATIO`.
    """
    _TIMING_FAIL_RATIO = 2.5
    _TIMING_NOTE_RATIO = 1.4

    def _ok(row: dict) -> bool:
        """`run_stage` writes `status: "PASS"|"FAIL"`. There is NO `ok` key.

        This read used to be `row.get("ok")`, which is None on every real row —
        so PASS->FAIL could never fire and the bar was decorative. It passed 11
        unit tests because they built their own `{"ok": ...}` fixtures: a test
        that defines its own subject proves nothing.
        `test_the_status_key_matches_what_run_stage_actually_emits` now pins it
        against the producer.
        """
        return str(row.get("status") or "").upper() == "PASS"

    b = {r["stage"]: r for r in base.get("results", [])}
    n = {r["stage"]: r for r in now.get("results", [])}
    clean = not base.get("live_cycle") and not now.get("live_cycle")

    print("\n" + "=" * 72)
    print(f"  COMPARE vs baseline   ticker {base.get('ticker')} -> {now.get('ticker')}")
    if not clean:
        print("  ⚠  one or both runs had a LIVE CYCLE — timing deltas below are")
        print("     NOT datapoints and cannot fail this comparison.")
    print("=" * 72)

    regressions = 0
    for stage in sorted(set(b) | set(n)):
        ob, on = b.get(stage), n.get(stage)
        if ob is None:
            print(f"  +  {stage:<20s} NEW stage, not in the baseline")
            continue
        if on is None:
            # Deliberately NOT a regression. A narrower run is a normal thing to
            # do (`bench_stage wake_pool --compare ...`), and failing it would
            # make the flag unusable for the quick checks it exists for. It is
            # printed so a run that silently lost a stage is still visible.
            print(f"  ·  {stage:<20s} not run in this comparison (narrower run)")
            continue
        was, is_ = _ok(ob), _ok(on)
        tb, tn = ob.get("median_s") or 0.0, on.get("median_s") or 0.0
        ratio = (tn / tb) if tb > 0 else 0.0
        rt = f"{tb:.2f}s -> {tn:.2f}s" + (f"  ({ratio:.2f}x)" if ratio else "")

        if was and not is_:
            regressions += 1
            print(f"  ❌ {stage:<20s} REGRESSION  PASS -> FAIL   {rt}")
            print(f"        ↳ {on.get('detail') or on.get('reason') or ''}")
        elif is_ and not was:
            print(f"  ✅ {stage:<20s} FIXED       FAIL -> PASS   {rt}")
        elif clean and ratio >= _TIMING_FAIL_RATIO:
            regressions += 1
            print(f"  ❌ {stage:<20s} {ratio:.2f}x SLOWER on a clean box   {rt}")
        elif ratio >= _TIMING_NOTE_RATIO:
            print(f"  ·  {stage:<20s} slower, not failed          {rt}")
        elif is_:
            print(f"  ✅ {stage:<20s} PASS                        {rt}")
        else:
            # Failing in BOTH runs is not a regression, but it is not a tick
            # either — a green mark on a red stage is how a broken bar goes
            # unnoticed.
            print(f"  ❗ {stage:<20s} still failing (was failing too) {rt}")

    print("=" * 72)
    print(f"  {regressions} contract regression(s)")
    if not clean:
        print("  Re-run with no live cycle before trusting any timing number.")
    print("=" * 72)
    return regressions


async def main() -> int:
    registry = build_registry()

    ap = argparse.ArgumentParser(
        description="Run one V3 cycle stage on one ticker, in a read-only sandbox.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("stages", nargs="*", help="stage names (see --list)")
    ap.add_argument("--ticker", "-t", default="AAPL", help="single ticker (default AAPL)")
    ap.add_argument("--repeat", "-n", type=int, default=1,
                    help="run each stage N times and report the median + spread")
    ap.add_argument("--list", action="store_true", help="list stages and exit")
    ap.add_argument("--all-context", action="store_true",
                    help="every non-LLM stage — the fast pre-flight")
    ap.add_argument("--all-agents", action="store_true", help="every LLM agent stage")
    ap.add_argument("--allow-writes", action="store_true",
                    help="do NOT block Mongo writes (rows will be written)")
    ap.add_argument("--force", action="store_true",
                    help="run even while a real cycle is live (timings will be junk)")
    ap.add_argument("--json", dest="json_out", help="write the full result to this path")
    ap.add_argument("--baseline", metavar="PATH",
                    help="write this run as a baseline to compare future runs against")
    ap.add_argument("--compare", metavar="PATH",
                    help="run, then diff against a baseline. Exits non-zero on a "
                         "CONTRACT regression (PASS->FAIL); timing only fails when "
                         "both runs were taken with no live cycle")
    args = ap.parse_args()

    if args.list:
        for group in ("context", "compute", "gate", "agent"):
            print(f"\n{group.upper()}")
            for s in registry.values():
                if s.group == group:
                    print(f"  {s.name:<20s} {'[LLM] ' if s.needs_llm else '      '}{s.blurb}")
        print("\n  --all-context  runs every non-LLM stage")
        print("  --all-agents   runs every LLM stage")
        return 0

    selected: list[Stage] = []
    if args.all_context:
        selected += [s for s in registry.values() if not s.needs_llm]
    if args.all_agents:
        selected += [s for s in registry.values() if s.needs_llm]
    for name in args.stages:
        if name not in registry:
            print(f"unknown stage {name!r}; try --list", file=sys.stderr)
            return 2
        selected.append(registry[name])
    if not selected:
        ap.print_help()
        return 2
    # De-duplicate, preserve order.
    seen, ordered = set(), []
    for s in selected:
        if s.name not in seen:
            seen.add(s.name)
            ordered.append(s)

    if not args.allow_writes:
        install_read_only_db()

    live = live_cycle_id()
    cycle_id = f"bench-{args.ticker.lower()}-{int(time.time())}"

    print("=" * 72)
    print(f"  BENCH STAGE — {args.ticker}   cycle_id={cycle_id}")
    print(f"  db={'READ-ONLY' if not args.allow_writes else 'WRITES ALLOWED'}"
          f"   repeat={args.repeat}   stages={len(ordered)}")
    if live:
        print(f"  ⚠  A REAL CYCLE IS LIVE: {live}")
        print("     Timings taken now are not datapoints — the boxes are loaded.")
        if any(s.needs_llm for s in ordered) and not args.force:
            print("     Refusing to add LLM load to a live cycle. Use --force to override.")
            return 3
    print("=" * 72)

    ctx = Ctx(ticker=args.ticker.strip().upper(), cycle_id=cycle_id)
    results = []
    for group in ("context", "compute", "gate", "agent"):
        rows = [s for s in ordered if s.group == group]
        if not rows:
            continue
        print(f"\n[{group}]")
        for stage in rows:
            results.append(await run_stage(stage, ctx, args.repeat))
            print_row(results[-1])

    passed = sum(1 for r in results if r["status"] == "PASS")
    total_s = sum(r["median_s"] for r in results)
    print("\n" + "=" * 72)
    print(f"  {passed}/{len(results)} stages PASS   |   {total_s:.1f}s of measured work")
    if BLOCKED_WRITES:
        # Named, not just counted. "3 writes blocked" is not actionable;
        # "whiteboard_entries.insert_many x2" tells you which persistence path
        # you just declined to exercise, and is the receipt that the sandbox is
        # a guard rather than a label in the header.
        tally: dict[str, int] = {}
        for w in BLOCKED_WRITES:
            tally[w] = tally.get(w, 0) + 1
        detail = ", ".join(f"{k} x{v}" for k, v in sorted(tally.items()))
        print(f"  {len(BLOCKED_WRITES)} mongo write(s) BLOCKED by the sandbox: {detail}")
    print("=" * 72)

    payload = {"ticker": ctx.ticker, "cycle_id": cycle_id,
               "live_cycle": bool(live), "blocked_writes": list(BLOCKED_WRITES),
               "results": results}

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"  wrote {args.json_out}")

    if args.baseline:
        with open(args.baseline, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"  wrote baseline {args.baseline}")
        if live:
            print("  ⚠  taken while a cycle was LIVE — its timings are recorded as")
            print("     unclean and cannot fail a future --compare.")

    if args.compare:
        try:
            with open(args.compare, encoding="utf-8") as f:
                base = json.load(f)
        except (OSError, ValueError) as e:
            print(f"  could not read baseline {args.compare}: {e}")
            return 2
        if compare_runs(base, payload):
            return 1

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
