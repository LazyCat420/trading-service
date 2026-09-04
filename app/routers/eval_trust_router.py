"""Eval-Trust Router — read-only observability for the eval-trust machinery.

Pure MongoDB implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.autoresearch.outcome_tracker import RESOLVE_AFTER_DAYS, WIN_THRESHOLD_PCT
from app.autoresearch import variance as variance_mod
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/eval-trust", tags=["EvalTrust"])

E_VALUE_PROMOTION_THRESHOLD = 20  # alpha 0.05, anytime-valid
E_VALUE_STRONG_THRESHOLD = 100    # alpha 0.01

GOODHART_WINDOW_DAYS = 7
GOODHART_TRIGGER_RATE = 0.10   # ≥10% hallucination rate with enough volume
GOODHART_MIN_EVALS = 10

# A noise-floor measurement older than this is reported as stale. Two weeks is
# twice the weekly refresh cadence, so one missed run is tolerated and two are
# not.
VARIANCE_STALE_AFTER_DAYS = 14


def _read_spec_raw() -> tuple[dict | None, str | None]:
    """Active spec with env-over-file precedence."""
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
    """Active experiment metadata + promotion gate."""
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
                # A finished experiment is not the same as no experiment. With
                # only `active: false` to go on, the panel fell through to
                # "collecting — not enough evidence yet" and silently lost a
                # concluded verdict, which is the one thing an experiment is
                # for. Carry the conclusion so a stopped run reads as stopped.
                "stopped_at": spec.get("stopped_at"),
                "verdict": spec.get("verdict"),
                "verdict_detail": spec.get("verdict_detail"),
            }
            payload["concluded"] = bool(spec.get("verdict"))
            row = mongo_query.agg_row('challenger_decisions', {'spec_label': spec.get("label")}, [('min', 'created_at'), ('max', 'created_at'), ('count', None)])
            if row:
                payload["first_pair_at"] = row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]) if row[0] else None
                payload["last_pair_at"] = row[1].isoformat() if hasattr(row[1], "isoformat") else str(row[1]) if row[1] else None
                payload["pairs_logged"] = row[2] or 0
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        return payload
    except Exception as e:
        logger.warning("[EvalTrust] experiment failed: %s", e)
        return {"active": False, "error": str(e)}


@router.get("/hold-outcomes")
async def hold_outcomes():
    """HOLD calibration cohort + directional splits."""
    try:
        resolved = mongo_query.group_rows('decision_outcomes', {'resolved_at': {'$ne': None}, 'outcome': {'$ne': None}}, ['outcome'], [('count', None)], [('key', 'outcome'), ('agg', 0)])
        pending = mongo_query.group_rows('decision_outcomes', {'resolved_at': None}, ['action'], [('count', None), ('min', 'created_at')], [('key', 'action'), ('agg', 0), ('agg', 1)])
        recent = mongo_query.find_rows('decision_outcomes', {'resolved_at': {'$ne': None}}, ['ticker', 'action', 'confidence', 'pnl_pct', 'outcome', 'cycle_id', 'created_at', 'resolved_at'], sort=[('resolved_at', -1)], limit=25)

        counts = {row[0]: row[1] for row in resolved}
        wins = counts.get("WIN", 0)
        losses = counts.get("LOSS", 0)
        holds_correct = counts.get("HOLD_CORRECT", 0)
        holds_avoided = counts.get("HOLD_AVOIDED_DECLINE", 0)
        holds_miss = counts.get("HOLD_MISS", 0)
        holds_right = holds_correct + holds_avoided
        hold_resolved = holds_right + holds_miss

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
            if hasattr(base, "tzinfo") and base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            if hasattr(base, "isoformat"):
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
                "avoided_decline": holds_avoided,
                "right": holds_right,
                "miss": holds_miss,
                "accuracy": round(holds_right / hold_resolved, 3) if hold_resolved else None,
                "accuracy_basis": "direction_aware_long_only",
                "pending": pending_holds,
                "first_resolution_eta": eta,
            },
            "pending_directional": pending_directional,
            "contract": {"horizon_days": RESOLVE_AFTER_DAYS, "band_pct": WIN_THRESHOLD_PCT},
            "recent": [
                {
                    "ticker": r[0], "action": r[1], "confidence": r[2],
                    "pnl_pct": r[3], "outcome": r[4], "cycle_id": r[5],
                    "created_at": r[6].isoformat() if hasattr(r[6], "isoformat") else str(r[6]) if r[6] else None,
                    "resolved_at": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]) if r[7] else None,
                }
                for r in recent
            ],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("[EvalTrust] hold-outcomes failed: %s", e)
        return {"error": str(e)}


@router.get("/provider-failures")
async def provider_failures(days: int = 2):
    """Every failed LLM call for the trading project."""
    days = max(1, min(int(days), 30))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    try:
        from app.db.mongo import get_mongo_client

        req = get_mongo_client()["prism"]["requests"]
        base = {"createdAt": {"$gte": cutoff}, "project": "vllm-trading-bot"}

        by_class = list(req.aggregate([
            {"$match": {**base, "success": False}},
            {"$project": {
                "klass": {"$substrCP": [{"$ifNull": ["$errorMessage", "(no errorMessage)"]}, 0, 60]},
                "agent": "$agent",
            }},
            {"$group": {"_id": {"k": "$klass", "a": "$agent"}, "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
            {"$limit": 50},
        ]))
        by_day = list(req.aggregate([
            {"$match": {**base, "success": False}},
            {"$project": {"day": {"$substrCP": ["$createdAt", 0, 10]}}},
            {"$group": {"_id": "$day", "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]))
        totals = list(req.aggregate([
            {"$match": base},
            {"$group": {"_id": "$success", "n": {"$sum": 1}}},
        ]))
        counts = {str(t["_id"]): t["n"] for t in totals}
        ok = counts.get("True", 0)
        failed = counts.get("False", 0)
        stranded = counts.get("None", 0)

        return {
            "window_days": days,
            "ok": ok,
            "failed": failed,
            "stranded_pending": stranded,
            "failure_rate": round(failed / (ok + failed), 4) if (ok + failed) else None,
            "by_class": [
                {"class": r["_id"]["k"], "agent": r["_id"].get("a"), "n": r["n"]}
                for r in by_class
            ],
            "by_day": [{"day": r["_id"], "n": r["n"]} for r in by_day],
            "source": "prism.requests (string-date filtered)",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("[EvalTrust] provider-failures failed: %s", e)
        return {"error": str(e)}


@router.get("/goodhart")
async def goodhart_status():
    """Grounding-judge health over the recent window."""
    try:
        since = datetime.now(timezone.utc) - timedelta(days=GOODHART_WINDOW_DAYS)
        rows = mongo_query.find_rows('decision_evaluations', {'timestamp': {'$gte': since.replace(tzinfo=None)}}, ['red_cards', 'evidence_gathering'])

        evaluated = len(rows)
        faithfulness = relevancy = other = 0
        grounding_scores: list[float] = []
        for rc_json, evidence_json in rows:
            if rc_json:
                try:
                    rcs = json.loads(rc_json) if isinstance(rc_json, str) else rc_json
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
                    ev = json.loads(evidence_json) if isinstance(evidence_json, str) else evidence_json
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
        rows = mongo_query.find_rows('variance_runs', {}, ['id', 'cycle_id', 'ticker', 'runs', 'completed', 'actions', 'majority_action', 'action_flip_rate', 'confidence_mean', 'confidence_stdev', 'confidence_range', 'status', 'error', 'created_at', 'finished_at'], sort=[('created_at', -1)], limit=20)

        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        desk_docs = mongo_store.find_docs("shared_desk", {"updated_at": {"$gte": cutoff}})
        desks = sorted(list({d.get("ticker") for d in desk_docs if d.get("ticker")}))

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
                "created_at": r[13].isoformat() if hasattr(r[13], "isoformat") else str(r[13]) if r[13] else None,
                "finished_at": r[14].isoformat() if hasattr(r[14], "isoformat") else str(r[14]) if r[14] else None,
            }
            for r in rows
        ]
        # How old the newest real measurement is. The noise floor is what every
        # experiment result is measured against, and until the weekly job was
        # registered nothing refreshed it: on 2026-09-04 the only row in
        # variance_runs was a single 4-run measurement from 2026-07-20. A
        # 46-day-old baseline is a constant wearing a measurement's name, so
        # say the age rather than leaving the reader to compute it.
        done = [r for r in runs if r["status"] == "done" and r["created_at"]]
        age_days = None
        if done:
            try:
                newest = datetime.fromisoformat(done[0]["created_at"])
                if newest.tzinfo is None:
                    newest = newest.replace(tzinfo=timezone.utc)
                age_days = round((datetime.now(timezone.utc) - newest).total_seconds() / 86400, 1)
            except ValueError:
                age_days = None

        return {
            "runs": runs,
            "measured_desks": sorted({r["ticker"] for r in runs if r["status"] == "done"}),
            "available_desks": desks,
            "in_progress": dict(_ACTIVE) if _ACTIVE["running"] else None,
            "baseline": variance_mod.DOCUMENTED_BASELINE,
            "confidence_noise_band_pts": variance_mod.NOISE_BAND_CONFIDENCE_PTS,
            "newest_run_age_days": age_days,
            "refresh_schedule": "weekly, Sunday 03:10 local (variance_noise_floor_weekly)",
            "stale": age_days is None or age_days > VARIANCE_STALE_AFTER_DAYS,
            "stale_after_days": VARIANCE_STALE_AFTER_DAYS,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("[EvalTrust] variance list failed: %s", e)
        return {"runs": [], "error": str(e)}


@router.post("/variance/run")
async def start_variance_run(req: VarianceRunRequest):
    """Kick off a noise-floor measurement on a persisted desk (background)."""
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
