#!/usr/bin/env python3
"""Run ONE stage of the V3 cycle, on ONE ticker, in a read-only sandbox.

    python3 scripts/bench_stage.py --list
    python3 scripts/bench_stage.py context --ticker AAPL
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
1. **The database session is READ ONLY.** `get_db` is wrapped so every
   connection issues `SET default_transaction_read_only = on`. An accidental
   INSERT raises `psycopg.errors.ReadOnlySqlTransaction` instead of writing a
   fake desk, a fake telemetry row, or a fake decision into the tables the
   audits read. Pass `--allow-writes` only when you specifically want the
   stage's persistence path exercised, and expect rows.
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
import contextlib
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

def install_read_only_db() -> None:
    """Wrap `get_db` so every sandbox connection is read-only.

    Patched on the module (`app.db.connection.get_db`) rather than on each
    caller, because callers import it inside functions at call time — the same
    reason the test suite's autouse fixture patches it there.
    """
    from app.db import connection as _conn

    real_get_db = _conn.get_db

    @contextlib.contextmanager
    def _read_only_get_db():
        with real_get_db() as cur:
            cur.execute("SET LOCAL default_transaction_read_only = on")
            yield cur

    _conn.get_db = _read_only_get_db


def live_cycle_id() -> str | None:
    """Return the cycle id of a running cycle, or None. Never raises."""
    try:
        from app.db.connection import get_db

        with get_db() as db:
            row = db.execute(
                "SELECT cycle_id, status FROM pipeline_state WHERE singleton_id = 'current'"
            ).fetchone()
        if row and row[1] in ("running", "starting", "collecting", "analyzing", "trading"):
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
                    help="do NOT force the DB session read-only (rows will be written)")
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
    print("=" * 72)

    payload = {"ticker": ctx.ticker, "cycle_id": cycle_id,
               "live_cycle": bool(live), "results": results}

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
