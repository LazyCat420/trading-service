"""
LLM Purge Pass — smart watchlist cleanup at the end of each cycle.

The deterministic health scoring engine (watchlist_health.py) identifies
candidates. This module sends them to the LLM for a final judgment call
before actually removing them.

Why not just purge by score alone%s
  The LLM might know context the math doesn't:
  - "earnings next week, keep it"
  - "new FDA catalyst discovered this cycle"
  - "user's manual pick, respect intent"

Runs as the final pass in the pipeline cycle.
"""

import logging
from typing import Callable

from app.config import settings
from app.db.connection import get_db
from app.services.vllm_client import llm, Priority
from app.services.prism_agent_caller import call_prism_agent
from app.utils.pipeline_utils import noop as _noop

logger = logging.getLogger(__name__)


# Purge system prompt moved to app/agents/custom/purge_pass.py


def _build_purge_prompt(
    candidates: list[dict],
    watchlist_size: int,
    positions: list[str],
) -> str:
    """Build the user prompt with candidate details."""
    lines = []
    for c in candidates:
        ticker = c["ticker"]
        score = c["score"]
        breakdown = c.get("breakdown", {})

        # Build a human-readable summary of why the score is low
        reasons = []
        if breakdown.get("data_richness", 25) < 8:
            reasons.append("very few articles/posts")
        if breakdown.get("data_freshness", 15) < 5:
            reasons.append("multiple zero-data cycles")
        if breakdown.get("coll_reliability", 10) < 4:
            reasons.append("collection failures")
        if breakdown.get("confidence", 20) < 5:
            reasons.append("low avg confidence")
        if breakdown.get("action_signals", 15) < 5:
            reasons.append("all HOLDs, no actionable signals")
        reason_str = ", ".join(reasons) if reasons else "low overall quality"

        lines.append("  {} (score: {}/100) -- {}".format(ticker, score, reason_str))

    candidates_section = "\n".join(lines) if lines else "  (none)"
    positions_section = ", ".join(positions) if positions else "(no positions)"

    # Get source info for each candidate
    with get_db() as db:
        source_info = []
        for c in candidates:
            try:
                row = db.execute(
                    "SELECT source FROM watchlist WHERE ticker = %s",
                    [c["ticker"]],
                ).fetchone()
                if row:
                    src = row[0] or "unknown"
                    source_info.append("  {}: added via {}".format(c["ticker"], src))
            except Exception:
                pass
        source_section = "\n".join(source_info) if source_info else "  (unknown)"

        prompt = (
            "PURGE CANDIDATES (ranked worst-first, scored by health engine):\n"
            + candidates_section
            + "\n\nTICKER SOURCES:\n"
            + source_section
            + "\n\nCURRENT WATCHLIST SIZE: {} tickers".format(watchlist_size)
            + "\nOPEN POSITIONS (immune from purge): "
            + positions_section
            + f"\nMAXIMUM TICKERS TO PURGE: {settings.WATCHLIST_MAX_PURGE}"
            + "\n\nWhich tickers should be PURGED from the watchlist?"
        )
        return prompt


def _parse_purge_response(content: str, valid_tickers: list[str]) -> dict:
    """Parse LLM JSON response, return validated purge dict.

    Uses the battle-tested parse_json_response from text_utils which handles:
    - <think> tag stripping (Qwen3 models)
    - Markdown code block extraction
    - Balanced brace-counting for nested JSON
    - Multiple fallback strategies
    """
    from app.utils.text_utils import parse_json_response
    import re

    # Check for raw DATA_MISSING pattern in content string
    if "DATA_MISSING" in content:
        matches = re.findall(r"DATA_MISSING:\s*([\w_, ]+)", content)
        missing_fields = []
        if matches:
            for m in matches:
                missing_fields.extend([f.strip() for f in m.split(",")])
        return {
            "status": "DATA_MISSING",
            "purge": [],
            "missing_fields": missing_fields or ["context"]
        }

    data = parse_json_response(content)
    if not data:
        logger.warning("[PIPELINE] purge_pass: no JSON found in LLM response")
        return {"status": "DATA_MISSING", "purge": [], "missing_fields": ["json_parse_failure"]}

    if data.get("status") == "DATA_MISSING" or data.get("proceed") is False:
        return {
            "status": "DATA_MISSING",
            "purge": [],
            "missing_fields": data.get("missing_fields", ["context"])
        }

    purged = data.get("purge", [])
    if not isinstance(purged, list):
        logger.warning("[PIPELINE] purge_pass: 'purge' is not a list")
        return {"status": "DATA_MISSING", "purge": [], "missing_fields": ["purge_not_a_list"]}

    valid_set = {t.upper() for t in valid_tickers}
    validated = [t.upper().strip() for t in purged if t.upper().strip() in valid_set]

    # Log reasoning
    reasoning = data.get("reasoning", {})
    if isinstance(reasoning, dict):
        for ticker, reason in reasoning.items():
            logger.info("[PIPELINE] purge_pass: %s -- %s", ticker, reason)

    return {
        "status": "ok",
        "purge": validated,
        "missing_fields": []
    }


async def run_purge_pass(
    watchlist: list[str],
    cycle_results: list[dict],
    emit: Callable | None = None,
    cycle_id: str = "",
) -> list[str]:
    """Run the purge pass — identify and remove weak watchlist tickers.

    Steps:
      1. Score all tickers (deterministic)
      2. Get purge candidates (below threshold, no positions)
      3. Ask LLM for final judgment
      4. Execute purge (set status='removed', reason='auto_purge')

    Args:
        watchlist: Current active watchlist tickers
        cycle_results: Analysis results from this cycle
        emit: Pipeline event emitter
        cycle_id: Current cycle ID for audit

    Returns:
        List of tickers that were purged.
    """
    if emit is None:
        emit = _noop

    if not settings.WATCHLIST_PURGE_ENABLED:
        logger.info("[PIPELINE] purge_pass: disabled via config")
        emit("purge", "disabled", "Watchlist purge: disabled", status="skipped")
        return []

    # Step 1: Get candidates from health scoring engine
    from app.pipeline.watchlist_health import get_purge_candidates, score_all_active

    emit(
        "purge",
        "scoring",
        "Scoring all watchlist tickers for health...",
        status="running",
    )

    all_scores = score_all_active()
    candidates = get_purge_candidates()

    if not candidates:
        msg = "Watchlist health: all {} tickers above {}/100 -- no purge needed".format(
            len(all_scores), settings.WATCHLIST_PURGE_MIN_SCORE
        )
        emit(
            "purge", "no_candidates", msg, status="ok", data={"scored": len(all_scores)}
        )
        logger.info("[PIPELINE] purge_pass: no candidates below threshold")
        return []

    # Format candidates for log
    cand_strs = ["{}({})".format(c["ticker"], c["score"]) for c in candidates]
    emit(
        "purge",
        "candidates",
        "Found {} purge candidates: {}".format(len(candidates), ", ".join(cand_strs)),
        status="running",
        data={"candidates": [c["ticker"] for c in candidates]},
    )

    # Step 2: Check Jetson availability for LLM judgment
    jetson_ok = await llm.health()
    if not jetson_ok:
        # Fallback: purge by score alone (no LLM)
        logger.warning(
            "[PIPELINE] purge_pass: Jetson unreachable, using score-only purge"
        )
        emit(
            "purge",
            "llm_offline",
            "Jetson unreachable -- purging by score only",
            status="error",
        )
        purge_list = [c["ticker"] for c in candidates]
    else:
        # Step 3: Ask LLM for final judgment
        with get_db() as db:
            try:
                from app.services.bot_manager import get_active_bot_id

                bid = get_active_bot_id()
            except Exception:
                from app.config import settings as _cfg

                bid = _cfg.BOT_ID
            try:
                pos_rows = db.execute(
                    "SELECT DISTINCT ticker FROM positions WHERE qty > 0 AND bot_id = %s",
                    [bid],
                ).fetchall()
                positions = [r[0] for r in pos_rows]
            except Exception:
                positions = []

            # System prompt is now loaded dynamically by Prism
            user_prompt = _build_purge_prompt(
                candidates=candidates,
                watchlist_size=len(watchlist),
                positions=positions,
            )

            try:
                content, tokens, elapsed = await call_prism_agent(
                    agent_id="CUSTOM_PURGE_PASS_AGENT",
                    user_message=user_prompt,
                    fallback_system_prompt="",
                    fallback_agent_name="purge_pass",
                    temperature=0.3,
                    max_tokens=512,
                    priority=Priority.LOW,
                    cycle_id=cycle_id,
                )

                candidate_tickers = [c["ticker"] for c in candidates]
                parse_result = _parse_purge_response(content, candidate_tickers)
                if parse_result.get("status") == "DATA_MISSING":
                    logger.warning("[PIPELINE] purge_pass: DATA_MISSING status returned from LLM parsing.")
                    purge_list = []
                else:
                    purge_list = parse_result.get("purge", [])

                emit(
                    "purge",
                    "llm_decision",
                    "LLM approved {}/{} for purge ({} tokens, {}ms)".format(
                        len(purge_list), len(candidates), tokens, elapsed
                    ),
                    status="ok",
                    data={"purged": purge_list, "tokens": tokens},
                )

            except Exception as e:
                logger.error("[PIPELINE] purge_pass: LLM call failed: %s", e)
                emit(
                    "purge",
                    "llm_error",
                    "LLM purge call failed ({}) -- skipping purge".format(e),
                    status="error",
                )
                return []

    # Step 4: Execute purge
    from app.trading.watchlist import auto_purge_ticker

    purged = []
    for ticker in purge_list:
        # Find the candidate details for the reason string
        candidate = next((c for c in candidates if c["ticker"] == ticker), None)
        score = candidate["score"] if candidate else 0
        reason = "health_score={}/100, auto-purged by bot".format(score)

        ok = auto_purge_ticker(ticker, reason=reason)
        if ok:
            purged.append(ticker)
            emit(
                "purge",
                "purged_{}".format(ticker),
                "Purged {} (score: {}/100)".format(ticker, score),
                status="ok",
                data={"ticker": ticker, "score": score},
            )
            logger.info("[PIPELINE] purge_pass: PURGED %s (score=%d)", ticker, score)

    if purged:
        emit(
            "purge",
            "complete",
            "Purge complete: removed {} tickers ({})".format(
                len(purged), ", ".join(purged)
            ),
            status="ok",
            data={"purged": purged, "count": len(purged)},
        )
    else:
        emit(
            "purge",
            "complete",
            "Purge pass: LLM opted to keep all candidates",
            status="ok",
        )

    return purged
