"""Decision-variance harness core — the noise floor of the decision desk.

Agents run at temperature 0.3, so identical evidence can produce different
decisions. Any A/B comparison of pipeline changes is meaningless until the
run-to-run spread on IDENTICAL inputs is known. This module replays the
decision synthesizer N times against a frozen SharedDesk snapshot (persisted
per cycle in shared_desk) and reports flip rate and confidence spread — the
minimum detectable effect for every future experiment.

Read-only with respect to the live system: runs against a COPY of the desk,
never persists artifacts, never writes analysis_results or triggers. Results
are persisted to variance_runs so the dashboard can show the measured noise
floor; that table is telemetry only.

Callers: scripts/decision_variance.py (operator CLI, stdout report) and
app/routers/eval_trust_router.py (guarded dashboard action).
"""

from __future__ import annotations

import copy
import json
import logging
import statistics
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# The 2026-07-19 pre-registered baseline (NVDA desk, run inside the container
# before variance_runs persistence existed — raw JSON was reported in the
# eval-trust handoff, not stored). Served as fallback context until live runs
# populate the table; the ±3-point rule of thumb derives from its σ≈1.5.
DOCUMENTED_BASELINE = {
    "ticker": "NVDA",
    "runs": 6,
    "completed": 6,
    "action_flip_rate": 0.0,
    "majority_action": "BUY",
    "confidence_range": [71, 75],
    "confidence_stdev": 1.5,
    "measured_at": "2026-07-19",
    "source": "handoff-eval-trust-wave (pre-persistence harness run)",
}

# Confidence movement inside this band is indistinguishable from sampling
# noise under the measured baseline (σ≈1.5 → ~2σ).
NOISE_BAND_CONFIDENCE_PTS = 3

_TABLE_READY = False


def _ensure_table() -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS variance_runs (
                id TEXT PRIMARY KEY,
                cycle_id TEXT,
                ticker TEXT NOT NULL,
                runs INTEGER,
                completed INTEGER,
                actions TEXT,
                majority_action TEXT,
                action_flip_rate DOUBLE PRECISION,
                confidence_mean DOUBLE PRECISION,
                confidence_stdev DOUBLE PRECISION,
                confidence_range TEXT,
                raw TEXT,
                status TEXT DEFAULT 'done',
                error TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                finished_at TIMESTAMPTZ
            )
            """
        )
    _TABLE_READY = True


async def run_variance(cycle_id: str | None, ticker: str, runs: int,
                       progress=None) -> dict:
    """Replay the decision synthesizer `runs` times on a frozen desk copy.

    Raises LookupError when no persisted desk exists for the ticker/cycle.
    `progress(i, total, action, confidence)` is called after each run.
    """
    from app.v3.desk_persistence import load_desk, load_latest_desk_for_ticker
    from app.v3.shared_desk import SharedDesk
    from app.v3.agent_runner import run_v3_agent
    from app.v3.agents import decision_agent

    desk = (
        load_desk(cycle_id, ticker)
        if cycle_id
        else load_latest_desk_for_ticker(ticker)
    )
    if desk is None:
        raise LookupError(
            f"No persisted desk found for ticker={ticker}"
            + (f" cycle={cycle_id}" if cycle_id else "")
        )

    base = desk.to_dict()
    results = []
    for i in range(runs):
        # Fresh copy per run — the runner appends the decision to the desk
        # itself (run_v3_agent returns only a PhaseOutcome enum), and a shared
        # desk would let run N see run N-1's decision.
        run_base = copy.deepcopy(base)
        run_base["trade_decision"] = None  # the field the synthesizer writes
        replica = SharedDesk.from_dict(run_base)
        await run_v3_agent(
            replica,
            decision_agent,
            cycle_id=f"variance-{desk.cycle_id}",
            timeout_seconds=300.0,
        )
        artifact = replica.trade_decision or {}
        action = artifact.get("action")
        confidence = artifact.get("confidence")
        results.append({"run": i + 1, "action": action, "confidence": confidence})
        if progress:
            progress(i + 1, runs, action, confidence)

    actions = [r["action"] for r in results if r["action"]]
    confs = [r["confidence"] for r in results if isinstance(r["confidence"], (int, float))]
    counts = Counter(actions)
    majority_action, majority_n = counts.most_common(1)[0] if counts else (None, 0)
    flip_rate = 1.0 - (majority_n / len(actions)) if actions else None

    return {
        "cycle_id": desk.cycle_id,
        "ticker": desk.ticker,
        "runs": runs,
        "completed": len(results),
        "actions": dict(counts),
        "majority_action": majority_action,
        # Fraction of runs that disagreed with the majority — the headline
        # noise-floor number. 0.0 = deterministic at this temperature.
        "action_flip_rate": round(flip_rate, 3) if flip_rate is not None else None,
        "confidence_mean": round(statistics.mean(confs), 1) if confs else None,
        "confidence_stdev": round(statistics.stdev(confs), 2) if len(confs) > 1 else 0.0,
        "confidence_range": [min(confs), max(confs)] if confs else None,
        "raw": results,
    }


def persist_variance_run(report: dict, status: str = "done",
                         error: str | None = None) -> str:
    """Store a harness report in variance_runs; returns the row id."""
    _ensure_table()
    run_id = f"vr-{uuid.uuid4().hex[:12]}"
    with get_db() as db:
        db.execute(
            """
            INSERT INTO variance_runs
            (id, cycle_id, ticker, runs, completed, actions, majority_action,
             action_flip_rate, confidence_mean, confidence_stdev,
             confidence_range, raw, status, error, finished_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                run_id,
                report.get("cycle_id"),
                report.get("ticker"),
                report.get("runs"),
                report.get("completed"),
                json.dumps(report.get("actions") or {}),
                report.get("majority_action"),
                report.get("action_flip_rate"),
                report.get("confidence_mean"),
                report.get("confidence_stdev"),
                json.dumps(report.get("confidence_range")),
                json.dumps(report.get("raw") or []),
                status,
                error,
                datetime.now(timezone.utc),
            ],
        )
    return run_id


async def run_and_persist(cycle_id: str | None, ticker: str, runs: int) -> dict:
    """Run the harness and persist the result (including failures)."""
    try:
        report = await run_variance(cycle_id, ticker, runs)
    except LookupError:
        raise
    except Exception as e:
        logger.warning("[Variance] %s run failed: %s", ticker, e)
        persist_variance_run(
            {"cycle_id": cycle_id, "ticker": ticker, "runs": runs, "completed": 0},
            status="error",
            error=str(e)[:500],
        )
        raise
    report["id"] = persist_variance_run(report)
    return report


def _stderr_progress(i: int, total: int, action, confidence) -> None:
    print(f"  run {i}/{total}: {action} @ {confidence}", file=sys.stderr)
