"""
Result Builder — constructs V1-compatible result dicts.

Extracted from runner.py._build_v1_compatible_result() so that
multiple ticker pipeline steps can build results without importing
the 1816-line god object.

The result dict matches the shape expected by:
  - trading_phase.py (Phase 5)
  - post_cycle_hooks.py
  - report_service.py / ticker_report_generator.py
  - Frontend analysis display
"""

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def build_v1_compatible_result(
    *,
    ticker: str,
    action: str,
    confidence: int | float,
    rationale: str,
    cycle_id: str,
    total_tokens: int,
    elapsed: float,
    stages: list[str],
    config_used: str,
    thesis: Any = None,
    sufficiency: Any = None,
    memory_context: dict[str, Any] | None = None,
    debate_result: Any = None,
    agent_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a result dict matching V1's analyze_ticker() output shape.

    This ensures trading_phase, post_cycle_hooks, report_service, and
    the frontend all work without modification.
    """
    v2_meta: dict[str, Any] = {
        "stages_completed": stages,
        "sufficiency_status": sufficiency.status if sufficiency else None,
        "thesis_action": thesis.action if thesis else None,
        "thesis_confidence": thesis.confidence if thesis else None,
        "thesis_weaknesses": thesis.weaknesses if thesis else [],
        "memory_episodes": (
            memory_context.get("episode_count", 0) if memory_context else 0
        ),
        "memory_rules": (memory_context.get("rule_count", 0) if memory_context else 0),
    }

    # Include debate metadata if available
    if debate_result:
        v2_meta["debate"] = {
            "judge_action": debate_result.judge_action,
            "judge_confidence": debate_result.judge_confidence,
            "winning_side": debate_result.winning_side,
            "integrity_status": debate_result.integrity_status,
            "bull_claims_verified": f"{len(debate_result.verified_bull_claims)}/{len(debate_result.bull_claims)}",
            "bear_claims_verified": f"{len(debate_result.verified_bear_claims)}/{len(debate_result.bear_claims)}",
            "unverified_claims": len(debate_result.unverified_claims),
            "key_deciding_factor": debate_result.key_deciding_factor,
            "transcript": debate_result.transcript,
            "total_tokens": debate_result.total_tokens,
            "original_thesis_status": getattr(debate_result, "original_thesis_status", "NOT_HELD"),
            "original_thesis_explanation": getattr(debate_result, "original_thesis_explanation", ""),
        }

    return {
        "ticker": ticker,
        "action": action,
        "confidence": int(confidence),
        "rationale": rationale,
        "config_used": config_used,
        "triage_tier": sufficiency.status if sufficiency else "standard",
        "escalated": debate_result is not None,
        "agent_results": agent_results or {},
        "c_result": {
            "action": action,
            "confidence": int(confidence),
            "rationale": rationale,
        },
        "d_result": {
            "action": debate_result.judge_action,
            "confidence": debate_result.judge_confidence,
            "original_thesis_status": getattr(debate_result, "original_thesis_status", "NOT_HELD"),
            "original_thesis_explanation": getattr(debate_result, "original_thesis_explanation", ""),
        }
        if debate_result
        else None,
        "human_review": False,
        "agent_tokens": 0,
        "rlm_tokens": total_tokens,
        "total_tokens": total_tokens,
        "total_time_s": round(elapsed, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # V2-specific metadata (ignored by V1 consumers, useful for debugging)
        "v2_metadata": v2_meta,
    }
