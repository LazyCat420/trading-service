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

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"ticker": ctx.ticker, "cycle_id": cycle_id,
                       "live_cycle": live, "results": results}, f, indent=2)
        print(f"  wrote {args.json_out}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
