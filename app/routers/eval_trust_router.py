"""Eval-Trust Router — read-only observability for the eval-trust machinery.

Serves the dashboard everything it needs to interpret the challenger
experiment honestly: the active spec (so the UI never hardcodes a label),
HOLD calibration outcomes (kept out of directional win rates), grounding-judge
health (the Goodhart tripwire), and decision-variance noise-floor runs.

This router changes NOTHING about decision logic, experiment specs, scoring,
or promotion — it reads what the pipeline already produces. The one action it
exposes (POST /variance/run) replays the decision synthesizer on a frozen desk
COPY, which is observational by construction (see app/autoresearch/variance.py),
and is single-flight + run-capped so it cannot stampede the LLM backends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.connection import get_db
from app.autoresearch.outcome_tracker import RESOLVE_AFTER_DAYS, WIN_THRESHOLD_PCT
from app.autoresearch import variance as variance_mod

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/eval-trust", tags=["EvalTrust"])

# Promotion gate — mirrors the pre-registration rules in experiments/README.md
# and the e-process thresholds in app/autoresearch/sequential.py. Served so
# the client renders the real gate instead of hardcoding it.
E_VALUE_PROMOTION_THRESHOLD = 20  # alpha 0.05, anytime-valid
E_VALUE_STRONG_THRESHOLD = 100    # alpha 0.01

# Goodhart tripwire: faithfulness red cards (hallucinations flagged by the
# grounding judge) as a fraction of judged decisions in the window.
GOODHART_WINDOW_DAYS = 7
GOODHART_TRIGGER_RATE = 0.10   # ≥10% hallucination rate with enough volume
GOODHART_MIN_EVALS = 10


def _read_spec_raw() -> tuple[dict | None, str | None]:
    """Active spec with env-over-file precedence, WITHOUT dropping disabled
    specs (get_challenger_spec returns None for those; the UI must be able to
    distinguish 'disabled' from 'absent')."""
    from app.v3.challenger import _SPEC_FILE

    raw = os.getenv("CHALLENGER_SPEC", "").strip()
    source = "env:CHALLENGER_SPEC"
    if not raw:
        try:
            with open(_SPEC_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            source = "experiments/active_spec.json"
        except OSError:
            return None, None
    if not raw:
        return None, None
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        return None, source
    if not isinstance(spec, dict) or not spec.get("label"):
        return None, source
    return spec, source


@router.get("/experiment")
async def active_experiment():
    """Active experiment metadata + the promotion gate the UI should render."""
    try:
        spec, source = _read_spec_raw()
        payload: dict = {
            "active": bool(spec) and spec.get("enabled") is not False,
            "spec": None,
            "source": source,
            "promotion_gate": {
                "e_value_threshold": E_VALUE_PROMOTION_THRESHOLD,
                "e_value_strong_threshold": E_VALUE_STRONG_THRESHOLD,
                "requirements": [
                    f"e_value >= {E_VALUE_PROMOTION_THRESHOLD} (anytime-valid, alpha 0.05)",
                    "leader = challenger",
                    "no regressing sectors",
                    "Goodhart tripwire (grounding judge) clear",
                ],
            },
            "noise_context": {
                "confidence_noise_band_pts": variance_mod.NOISE_BAND_CONFIDENCE_PTS,
                "note": (
                    "Confidence movement inside this band is indistinguishable "
                    "from sampling noise at the measured baseline; action-level "
                    "flips are the meaningful signal."
                ),
            },
            "outcome_contract": {
                "horizon_days": RESOLVE_AFTER_DAYS,
                "band_pct": WIN_THRESHOLD_PCT,
            },
        }
        if spec:
            payload["spec"] = {
                "label": spec.get("label"),
                "enabled": spec.get("enabled") is not False,
                "custom_instructions": spec.get("custom_instructions"),
            }
            from app.v3.challenger import _ensure_table
            _ensure_table()
            with get_db() as db:
                row = db.execute(
                    "SELECT MIN(created_at), MAX(created_at), COUNT(*) "
                    "FROM challenger_decisions WHERE spec_label = %s",
                    [spec.get("label")],
                ).fetchone()
            if row:
                payload["first_pair_at"] = row[0].isoformat() if row[0] else None
                payload["last_pair_at"] = row[1].isoformat() if row[1] else None
                payload["pairs_logged"] = row[2] or 0
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        return payload
    except Exception as e:
        logger.warning("[EvalTrust] experiment failed: %s", e)
        return {"active": False, "error": str(e)}


@router.get("/hold-outcomes")
async def hold_outcomes():
    """HOLD calibration cohort + directional splits, kept strictly separate.

    HOLD_CORRECT/HOLD_MISS grade whether the ticker stayed inside the ±band
    over the horizon — calibration evidence, never directional skill. The
    directional win rate here excludes HOLDs and FLATs (basis ex_flat_ex_hold),
    matching decision_audit's score v4.
    """
    try:
        with get_db() as db:
            resolved = db.execute(
                "SELECT outcome, COUNT(*) FROM decision_outcomes "
                "WHERE resolved_at IS NOT NULL AND outcome IS NOT NULL "
                "GROUP BY outcome"
            ).fetchall()
            pending = db.execute(
                "SELECT action, COUNT(*), MIN(created_at) FROM decision_outcomes "
                "WHERE resolved_at IS NULL GROUP BY action"
            ).fetchall()
            recent = db.execute(
                "SELECT ticker, action, confidence, pnl_pct, outcome, cycle_id, "
                "       created_at, resolved_at "
                "FROM decision_outcomes WHERE resolved_at IS NOT NULL "
                "ORDER BY resolved_at DESC LIMIT 25"
            ).fetchall()

        counts = {row[0]: row[1] for row in resolved}
        wins = counts.get("WIN", 0)
        losses = counts.get("LOSS", 0)
        holds_correct = counts.get("HOLD_CORRECT", 0)
        holds_miss = counts.get("HOLD_MISS", 0)
        hold_resolved = holds_correct + holds_miss

        pending_holds = 0
        pending_directional = 0
        earliest_pending_hold = None
        for action, n, earliest in pending:
            if action == "HOLD":
                pending_holds += n
                if earliest and (earliest_pending_hold is None or earliest < earliest_pending_hold):
                    earliest_pending_hold = earliest
            else:
                pending_directional += n

        eta = None
        if hold_resolved == 0 and earliest_pending_hold is not None:
            base = earliest_pending_hold
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            eta = (base + timedelta(days=RESOLVE_AFTER_DAYS)).isoformat()

        return {
            "resolved_counts": counts,
            "directional": {
                "wins": wins,
                "losses": losses,
                "flats": counts.get("FLAT", 0),
                "win_rate": round(wins / (wins + losses), 3) if (wins + losses) else None,
                "basis": "ex_flat_ex_hold",
            },
            "hold": {
                "resolved": hold_resolved,
                "correct": holds_correct,
                "miss": holds_miss,
                "accuracy": round(holds_correct / hold_resolved, 3) if hold_resolved else None,
                "pending": pending_holds,
                "first_resolution_eta": eta,
            },
            "pending_directional": pending_directional,
            "contract": {"horizon_days": RESOLVE_AFTER_DAYS, "band_pct": WIN_THRESHOLD_PCT},
            "recent": [
                {
                    "ticker": r[0], "action": r[1], "confidence": r[2],
                    "pnl_pct": r[3], "outcome": r[4], "cycle_id": r[5],
                    "created_at": r[6].isoformat() if r[6] else None,
                    "resolved_at": r[7].isoformat() if r[7] else None,
                }
                for r in recent
            ],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("[EvalTrust] hold-outcomes failed: %s", e)
        return {"error": str(e)}


@router.get("/goodhart")
async def goodhart_status():
    """Grounding-judge health over the recent window — the Goodhart tripwire.

    System-wide (champion pipeline) signal: if decisions start scoring well
    while hallucinating their evidence, the improvement is the metric being
    gamed, not skill. Faithfulness red cards are the judge's hallucination
    flags; the tripwire is rate-based so a busy week is not penalized.
    """
    try:
        since = datetime.now(timezone.utc) - timedelta(days=GOODHART_WINDOW_DAYS)
        with get_db() as db:
            rows = db.execute(
                "SELECT red_cards, evidence_gathering FROM decision_evaluations "
                "WHERE timestamp >= %s",
                [since.replace(tzinfo=None)],
            ).fetchall()

        evaluated = len(rows)
        faithfulness = relevancy = other = 0
        grounding_scores: list[float] = []
        for rc_json, evidence_json in rows:
            if rc_json:
                try:
                    rcs = json.loads(rc_json)
                    if isinstance(rcs, list):
                        for rc in rcs:
                            if "Faithfulness Failure" in rc:
                                faithfulness += 1
                            elif "Relevancy" in rc:
                                relevancy += 1
                            else:
                                other += 1
                except (json.JSONDecodeError, TypeError):
                    pass
            if evidence_json:
                try:
                    ev = json.loads(evidence_json)
                    gs = ev.get("grounding_score", ev.get("hf_rougeL"))
                    if isinstance(gs, (int, float)):
                        grounding_scores.append(float(gs))
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

        rate = (faithfulness / evaluated) if evaluated else None
        if evaluated == 0:
            status = "no_data"
        elif evaluated >= GOODHART_MIN_EVALS and rate >= GOODHART_TRIGGER_RATE:
            status = "triggered"
        elif faithfulness > 0:
            status = "warning"
        else:
            status = "clear"

        return {
            "status": status,
            "window_days": GOODHART_WINDOW_DAYS,
            "evaluated_decisions": evaluated,
            "faithfulness_red_cards": faithfulness,
            "relevancy_red_cards": relevancy,
            "other_red_cards": other,
            "faithfulness_rate": round(rate, 3) if rate is not None else None,
            "trigger_rule": (
                f"triggered when faithfulness rate >= {GOODHART_TRIGGER_RATE:.0%} "
                f"over >= {GOODHART_MIN_EVALS} judged decisions in {GOODHART_WINDOW_DAYS}d"
            ),
            "avg_grounding_score": (
                round(sum(grounding_scores) / len(grounding_scores), 3)
                if grounding_scores else None
            ),
            "basis": "grounding judge over champion-pipeline decisions (system-wide)",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("[EvalTrust] goodhart failed: %s", e)
        return {"status": "unavailable", "error": str(e)}


# ---------------------------------------------------------------------------
# Variance harness
# ---------------------------------------------------------------------------

# Single-flight guard: the harness replays the decision synthesizer (real LLM
# calls); one run at a time is plenty and caps worst-case load.
_ACTIVE: dict = {"running": False, "ticker": None, "runs": 0, "started_at": None}

MAX_VARIANCE_RUNS = 8


class VarianceRunRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=10)
    cycle_id: str | None = None
    runs: int = Field(default=6, ge=2, le=MAX_VARIANCE_RUNS)


@router.get("/variance")
async def variance_runs():
    """Persisted noise-floor runs + coverage + the documented baseline."""
    try:
        variance_mod._ensure_table()
        with get_db() as db:
            rows = db.execute(
                "SELECT id, cycle_id, ticker, runs, completed, actions, "
                "       majority_action, action_flip_rate, confidence_mean, "
                "       confidence_stdev, confidence_range, status, error, "
                "       created_at, finished_at "
                "FROM variance_runs ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            desks = db.execute(
                "SELECT DISTINCT ticker FROM shared_desk "
                "WHERE updated_at >= NOW() - INTERVAL '14 days' ORDER BY ticker"
            ).fetchall()

        def _j(val):
            try:
                return json.loads(val) if isinstance(val, str) else val
            except (json.JSONDecodeError, TypeError):
                return None

        runs = [
            {
                "id": r[0], "cycle_id": r[1], "ticker": r[2], "runs": r[3],
                "completed": r[4], "actions": _j(r[5]), "majority_action": r[6],
                "action_flip_rate": r[7], "confidence_mean": r[8],
                "confidence_stdev": r[9], "confidence_range": _j(r[10]),
                "status": r[11], "error": r[12],
                "created_at": r[13].isoformat() if r[13] else None,
                "finished_at": r[14].isoformat() if r[14] else None,
            }
            for r in rows
        ]
        return {
            "runs": runs,
            "measured_desks": sorted({r["ticker"] for r in runs if r["status"] == "done"}),
            "available_desks": [d[0] for d in desks],
            "in_progress": dict(_ACTIVE) if _ACTIVE["running"] else None,
            "baseline": variance_mod.DOCUMENTED_BASELINE,
            "confidence_noise_band_pts": variance_mod.NOISE_BAND_CONFIDENCE_PTS,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("[EvalTrust] variance list failed: %s", e)
        return {"runs": [], "error": str(e)}


@router.post("/variance/run")
async def start_variance_run(req: VarianceRunRequest):
    """Kick off a noise-floor measurement on a persisted desk (background).

    Guarded: single-flight, run count capped, desk must already exist —
    this can never create desks, trades, or analysis rows.
    """
    if _ACTIVE["running"]:
        raise HTTPException(
            status_code=409,
            detail=f"variance run already in progress ({_ACTIVE['ticker']})",
        )

    ticker = req.ticker.upper().strip()
    from app.v3.desk_persistence import load_desk, load_latest_desk_for_ticker
    desk = (
        load_desk(req.cycle_id, ticker) if req.cycle_id
        else load_latest_desk_for_ticker(ticker)
    )
    if desk is None:
        raise HTTPException(status_code=404, detail=f"no persisted desk for {ticker}")

    _ACTIVE.update(
        running=True, ticker=ticker, runs=req.runs,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    async def _background():
        try:
            report = await variance_mod.run_and_persist(req.cycle_id, ticker, req.runs)
            logger.info(
                "[EvalTrust] variance run done: %s flip_rate=%s stdev=%s",
                ticker, report.get("action_flip_rate"), report.get("confidence_stdev"),
            )
        except Exception as e:
            logger.warning("[EvalTrust] variance run failed for %s: %s", ticker, e)
        finally:
            _ACTIVE.update(running=False, ticker=None, runs=0, started_at=None)

    asyncio.create_task(_background())
    return {"started": True, "ticker": ticker, "runs": req.runs,
            "cycle_id": desk.cycle_id}
