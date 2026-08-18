"""Decision-variance harness core — the noise floor of the decision desk.

Pure MongoDB implementation for variance_runs collection.
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

from app.db import mongo_store

logger = logging.getLogger(__name__)

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

NOISE_BAND_CONFIDENCE_PTS = 3


async def run_variance(cycle_id: str | None, ticker: str, runs: int,
                       progress=None) -> dict:
    """Replay the decision synthesizer `runs` times on a frozen desk copy."""
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
        run_base = copy.deepcopy(base)
        run_base["trade_decision"] = None
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
        "action_flip_rate": round(flip_rate, 3) if flip_rate is not None else None,
        "confidence_mean": round(statistics.mean(confs), 1) if confs else None,
        "confidence_stdev": round(statistics.stdev(confs), 2) if len(confs) > 1 else 0.0,
        "confidence_range": [min(confs), max(confs)] if confs else None,
        "raw": results,
    }


def persist_variance_run(report: dict, status: str = "done",
                         error: str | None = None) -> str:
    """Store a harness report in variance_runs; returns the row id."""
    run_id = f"vr-{uuid.uuid4().hex[:12]}"
    mongo_store.insert_docs('variance_runs', [{
        'id': run_id,
        'cycle_id': report.get("cycle_id"),
        'ticker': report.get("ticker"),
        'runs': report.get("runs"),
        'completed': report.get("completed"),
        'actions': json.dumps(report.get("actions") or {}),
        'majority_action': report.get("majority_action"),
        'action_flip_rate': report.get("action_flip_rate"),
        'confidence_mean': report.get("confidence_mean"),
        'confidence_stdev': report.get("confidence_stdev"),
        'confidence_range': json.dumps(report.get("confidence_range")),
        'raw': json.dumps(report.get("raw") or []),
        'status': status,
        'error': error,
        'created_at': datetime.now(timezone.utc),
        'finished_at': datetime.now(timezone.utc),
    }])
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
