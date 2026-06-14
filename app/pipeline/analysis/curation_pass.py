"""
LLM Curation Pass — smart discovery → watchlist promotion.

Pass 2.7 in the data pipeline. The LLM reviews discovered tickers
(from scrapers + user additions) and picks the best ones for deep analysis.

Discovery = the single input pool (user + scrapers).
Watchlist  = the LLM's output   (bot's picks for analysis).
"""

import json
import logging
import re
from typing import Callable

from app.config import settings
from app.db.connection import get_db
from app.services.vllm_client import llm, Priority
from app.services.prism_agent_caller import call_prism_agent

logger = logging.getLogger(__name__)

# The system prompt has been moved to app/agents/custom/curation_pass.py
# for a cleaner modular Lego piece architecture.


def _build_user_prompt(
    discovered: list[dict],
    watchlist: list[str],
    rejections: list[dict],
    positions: list[str],
) -> str:
    """Build the user prompt with all context for the LLM."""
    # Discovered tickers section
    disc_lines = []
    for d in discovered:
        source = d.get("source", "unknown")
        context = d.get("context", "")[:200]
        score = d.get("score", 0)
        disc_lines.append(
            f"  {d['ticker']} (source={source}, score={score:.1f})"
            f"{f' — {context}' if context else ''}"
        )
    disc_section = "\n".join(disc_lines) if disc_lines else "  (none)"

    # Watchlist section
    wl_section = ", ".join(watchlist) if watchlist else "(empty)"

    # Rejection history
    rej_lines = []
    for r in rejections[:15]:
        reason = r.get("reason", r.get("status", "removed"))
        rej_lines.append(f"  {r['ticker']} — {reason}")
    rej_section = "\n".join(rej_lines) if rej_lines else "  (none recently)"

    # Portfolio positions
    pos_section = ", ".join(positions) if positions else "(no positions)"

    return f"""DISCOVERED TICKERS ({len(discovered)} candidates from scrapers + user):
{disc_section}

CURRENT WATCHLIST (already tracking — don't duplicate):
{wl_section}

RECENTLY REJECTED BY USER (banned/removed in last 7 days):
{rej_section}

CURRENT PORTFOLIO (already holding):
{pos_section}

Which tickers should be promoted for deep analysis%s"""


def _fetch_discovered_details(tickers: list[str]) -> list[dict]:
    """Pull source + context from discovered_tickers table."""
    with get_db() as db:
        details = []
        for t in tickers:
            try:
                row = db.execute(
                    "SELECT ticker, source, context, score FROM discovered_tickers "
                    "WHERE ticker = %s",
                    [t],
                ).fetchone()
                if row:
                    details.append(
                        {
                            "ticker": row[0],
                            "source": row[1] or "unknown",
                            "context": row[2] or "",
                            "score": float(row[3]) if row[3] else 0.0,
                        }
                    )
                else:
                    details.append(
                        {"ticker": t, "source": "unknown", "context": "", "score": 0.0}
                    )
            except Exception:
                details.append(
                    {"ticker": t, "source": "unknown", "context": "", "score": 0.0}
                )
        return details


def _fetch_rejections() -> list[dict]:
    """Get tickers the user recently removed/banned (last 7 days)."""
    with get_db() as db:
        try:
            rows = db.execute(
                "SELECT ticker, status FROM watchlist "
                "WHERE status IN ('removed', 'banned') "
                "ORDER BY added_at DESC LIMIT 20"
            ).fetchall()
            return [{"ticker": r[0], "reason": r[1]} for r in rows]
        except Exception:
            return []


def _fetch_positions() -> list[str]:
    """Get tickers we currently hold in portfolio."""
    with get_db() as db:
        try:
            rows = db.execute(
                "SELECT DISTINCT ticker FROM portfolio WHERE shares > 0"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []


def _parse_curation_response(content: str, valid_tickers: list[str]) -> dict:
    """Parse LLM JSON response, return validated promote dict.

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
            "promote": [],
            "missing_fields": missing_fields or ["context"]
        }

    data = parse_json_response(content)
    if not data:
        logger.warning("[PIPELINE] curation: no JSON found in LLM response")
        return {"status": "DATA_MISSING", "promote": [], "missing_fields": ["json_parse_failure"]}

    if data.get("status") == "DATA_MISSING" or data.get("proceed") is False:
        return {
            "status": "DATA_MISSING",
            "promote": [],
            "missing_fields": data.get("missing_fields", ["context"])
        }

    promoted = data.get("promote", [])
    if not isinstance(promoted, list):
        logger.warning("[PIPELINE] curation: 'promote' is not a list")
        return {"status": "DATA_MISSING", "promote": [], "missing_fields": ["promote_not_a_list"]}

    # Validate: only return tickers that were in the discovered list
    valid_set = {t.upper() for t in valid_tickers}
    validated = [t.upper().strip() for t in promoted if t.upper().strip() in valid_set]

    # Log reasoning if available
    reasoning = data.get("reasoning", {})
    if isinstance(reasoning, dict):
        for ticker, reason in reasoning.items():
            logger.info("[PIPELINE] curation: %s — %s", ticker, reason)

    return {
        "status": "ok",
        "promote": validated,
        "missing_fields": []
    }


async def curate_discoveries(
    discovered_tickers: list[str],
    current_watchlist: list[str],
    emit: Callable,
    cycle_id: str = "",
) -> list[str]:
    """Ask the LLM which discovered tickers are worth promoting.

    Args:
        discovered_tickers: tickers that passed all gates (market cap, ban)
        current_watchlist: already active watchlist tickers
        emit: pipeline event emitter
        cycle_id: current pipeline cycle ID for audit trail

    Returns:
        List of tickers the LLM chose to promote.
        On failure, returns all discovered_tickers (fallback).
    """
    if not discovered_tickers:
        return []

    if not settings.LLM_CURATION_ENABLED:
        logger.info(
            "[PIPELINE] curation: disabled via config, passing all %d tickers",
            len(discovered_tickers),
        )
        return discovered_tickers

    max_promote = settings.LLM_CURATION_MAX_PROMOTE

    # Check if Jetson is reachable
    jetson_ok = await llm.health()
    if not jetson_ok:
        fallback = settings.LLM_CURATION_FALLBACK
        logger.warning("[PIPELINE] curation: Jetson unreachable, fallback=%s", fallback)
        emit(
            "collecting",
            "llm_curation",
            f"Jetson unreachable — fallback: {fallback}",
            status="error",
        )
        if fallback == "pass_all":
            return discovered_tickers
        else:
            return []

    # Build context
    details = _fetch_discovered_details(discovered_tickers)
    rejections = _fetch_rejections()
    positions = _fetch_positions()

    user_prompt = _build_user_prompt(
        discovered=details,
        watchlist=current_watchlist,
        rejections=rejections,
        positions=positions,
    )

    logger.info(
        "[PIPELINE] curation: sending %d candidates to LLM (max_promote=%d)",
        len(discovered_tickers),
        max_promote,
    )

    max_retries = 3
    promoted = []

    for attempt in range(max_retries):
        try:
            content, tokens, elapsed = await call_prism_agent(
                agent_id="CUSTOM_CURATION_PASS_AGENT",
                user_message=user_prompt,
                fallback_system_prompt="",  # Loaded dynamically from app.agents.custom
                fallback_agent_name="curation_pass",
                temperature=0.3,
                max_tokens=512,
                priority=Priority.LOW,
                cycle_id=cycle_id,
            )

            parse_result = _parse_curation_response(content, discovered_tickers)
            if parse_result.get("status") == "DATA_MISSING":
                logger.warning("[PIPELINE] curation: DATA_MISSING status returned from LLM parsing.")
                promoted = []
            else:
                promoted = parse_result.get("promote", [])

            # Enforce max promote limit
            if len(promoted) > max_promote:
                promoted = promoted[:max_promote]

            # Persist promoted tickers into watchlist so they survive across cycles
            if promoted:
                with get_db() as db:
                    for ticker in promoted:
                        try:
                            db.execute(
                                """
                                INSERT INTO watchlist
                                (ticker, status, source, added_at)
                                VALUES (%s, 'active', 'llm_curation', CURRENT_TIMESTAMP)
                                ON CONFLICT (ticker) DO NOTHING
                            """,
                                [ticker],
                            )
                        except Exception as e:
                            logger.warning(
                                "[PIPELINE] curation: failed to add %s to watchlist: %s",
                                ticker,
                                e,
                              )
                    logger.info(
                        "[PIPELINE] curation: promoted %d tickers to watchlist",
                        len(promoted),
                    )

            logger.info(
                "curation: LLM promoted %d/%d tickers in %dms (%d tokens)",
                len(promoted),
                len(discovered_tickers),
                elapsed,
                tokens,
            )

            return promoted

        except ValueError as ve:
            logger.warning("[PIPELINE] curation: Parse error on attempt %d: %s", attempt + 1, ve)
            if attempt == max_retries - 1:
                logger.error("[PIPELINE] curation: All parsing attempts failed.")
                fallback = settings.LLM_CURATION_FALLBACK
                emit(
                    "collecting",
                    "llm_curation",
                    f"LLM parsing failed ({ve}) — fallback: {fallback}",
                    status="error",
                )
                if fallback == "pass_all":
                    return discovered_tickers
                else:
                    return []
            
            # Add a strong reminder to output JSON for the next attempt
            user_prompt += "\n\nCRITICAL: Your previous response was not valid JSON. You MUST return ONLY valid JSON matching the format requested."

        except Exception as e:
            logger.error("[PIPELINE] curation: LLM call failed: %s", e)
            fallback = settings.LLM_CURATION_FALLBACK
            emit(
                "collecting",
                "llm_curation",
                f"LLM call failed ({e}) — fallback: {fallback}",
                status="error",
            )
            if fallback == "pass_all":
                return discovered_tickers
            else:
                return []


async def run_curation(ticker: str, cycle_id: str, bot_id: str) -> dict:
    """Worker entry point for LLM curation pass of a single ticker."""
    from app.utils.payload_gate import gate_check, InsufficientDataError

    logger.info("[PIPELINE] curation_pass: running curation for %s", ticker)
    try:
        # Fetch details to validate input
        details = _fetch_discovered_details([ticker])
        if not details or not details[0].get("ticker"):
            raise InsufficientDataError("curation_input", ["ticker"])

        # Validate the detail structure
        gate_check(details[0], "market_data")  # validate basic fields if any

        promoted = await curate_discoveries(
            discovered_tickers=[ticker],
            current_watchlist=[],
            emit=lambda *a, **kw: None,
            cycle_id=cycle_id,
        )
        return {
            "status": "ok",
            "promote": promoted,
            "missing_fields": [],
        }
    except InsufficientDataError as e:
        logger.warning("[PIPELINE] curation_pass: gate blocked for %s: %s", ticker, e)
        return {
            "status": "ABORTED",
            "reason": str(e),
            "missing_fields": e.missing,
            "agent": "curation_pass",
            "cio_called": False,
        }
    except Exception as e:
        logger.error("[PIPELINE] curation_pass: execution error for %s: %s", ticker, e)
        return {
            "status": "ABORTED",
            "reason": f"Execution error: {e}",
            "missing_fields": ["execution_error"],
            "agent": "curation_pass",
            "cio_called": False,
        }

