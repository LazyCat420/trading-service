"""
data_readiness.py — deterministic data-readiness evaluator, SHADOW ONLY.

Nothing gates on this yet. It stamps `cycle_metadata["readiness"]` and that is
all — the hard gate (no agents dispatched, disposition DATA_GAP) is Phase 3 of
the plan of record, which requires the shadow to run for ~a week and be
compared against the Board's self-reported `conviction_vector.data_quality`
first. Saying so here because the file previously read as if it were enforcing.

WHAT THE 2026-09-01 AUDIT FOUND, now fixed:
  * the docstring advertised three rules (30 trading days of history, spot > 0,
    corrupted technicals) that were never implemented;
  * the technicals rule required the literal "No technical indicators", which
    NO producer emits — `technical_baseline.build_technical_baseline_block`
    writes "**NONE ON FILE** ... no verified RSI, SMA, ATR or Bollinger level".
    The rule could not fire in production, and its unit test passed only
    because the fixture fabricated the string the code was looking for;
  * the staleness threshold was > 5 trading days while the gate it shadows
    (HOLD_POLICY_BLOCKED_STALE_PRICE_DATA) blocks at > 3, so the shadow
    disagreed with its own subject by two days.

Rules as ACTUALLY implemented, matched against real producer output:
1. the pre-collect step failed outright, or produced no report at all;
2. the pinned price series is stale by the same threshold the policy gate uses;
3. no technical baseline is on file for the ticker;
4. price age could not be determined at all (the probe failed) — recorded as a
   distinct reason, because "unknown" is not "fresh".
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ReadinessResult:
    is_ready: bool
    quality_score: float  # 0.0 to 1.0
    missing_reasons: list[str]
    disposition: str  # "PROCEED" | "DATA_GAP"


#: Same threshold as HOLD_POLICY_BLOCKED_STALE_PRICE_DATA in
#: `orchestrator._apply_policy_gates`. A shadow that disagrees with the gate it
#: shadows measures a different question.
STALE_TRADING_DAYS = 3

#: The marker `technical_baseline.build_technical_baseline_block` actually
#: emits when a ticker has no stored indicator row. Asserted against the real
#: producer in tests/unit/test_data_readiness.py so a reword breaks the test
#: rather than silently disarming the rule.
NO_TECHNICAL_BASELINE_MARKER = "**NONE ON FILE**"


def evaluate_ticker_readiness(
    ticker: str,
    data_report: str,
    technical_context: Optional[str] = None,
    valuation_context: Optional[str] = None,
    price_age_trading_days: Optional[int] = None,
    stale_detection_failed: bool = False,
) -> ReadinessResult:
    """Evaluate if a ticker is data-ready for multi-agent debate. Shadow only."""
    reasons: list[str] = []

    if not data_report or "Failed to pre-collect stock data" in data_report:
        reasons.append("data_report_collection_failed")

    if price_age_trading_days is not None:
        if price_age_trading_days > STALE_TRADING_DAYS:
            reasons.append(f"price_history_stale_{price_age_trading_days}_days")
    elif stale_detection_failed:
        # Distinct from "fresh": the probe died, so the age is unknown. The
        # policy gate fails open on this by design; the shadow should still
        # count it, or the two disagree about what was measured.
        reasons.append("price_age_unknown")

    if technical_context and NO_TECHNICAL_BASELINE_MARKER in technical_context:
        reasons.append("missing_technical_baseline")

    if reasons:
        score = max(0.0, 1.0 - 0.35 * len(reasons))
        logger.warning(
            "[Readiness] %s DATA_GAP detected (score=%.2f): %s",
            ticker, score, ", ".join(reasons),
        )
        return ReadinessResult(
            is_ready=False,
            quality_score=score,
            missing_reasons=reasons,
            disposition="DATA_GAP",
        )

    return ReadinessResult(
        is_ready=True,
        quality_score=1.0,
        missing_reasons=[],
        disposition="PROCEED",
    )
