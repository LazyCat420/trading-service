"""
News-Driven Ticker Discovery Engine — Phase 0.

Scans news_articles, reddit_posts, and congress_trades already in the DB
(collected by scraper-service) and uses an LLM to extract actionable ticker
opportunities. Populates the discovered_tickers table so the TickerSelector
has fresh leads every cycle.

Usage:
    from app.pipeline.analysis.news_discovery import run_news_discovery
    discovered = await run_news_discovery(emit=callback)
"""

import json
import logging
import re
import time
from typing import Callable

from app.db.connection import get_db
from app.utils.text_utils import sanitize_ascii

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Data Gathering (pure DB reads — no network calls)
# ─────────────────────────────────────────────────────────────────────


def _gather_recent_news(hours: int = 48, limit: int = 30) -> str:
    """Pull recent news articles from DB."""
    with get_db() as db:
        lines = []
        try:
            rows = db.execute(
                """
                SELECT ticker, title, publisher, published_at,
                       COALESCE(llm_summary, summary) AS best_summary
                FROM news_articles
                WHERE published_at > CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
                ORDER BY published_at DESC
                LIMIT %s
                """,
                [hours, limit],
            ).fetchall()
            if rows:
                lines.append(f"## Recent News Articles ({len(rows)} articles, last {hours}h)")
                for ticker, title, publisher, pub_at, summary in rows:
                    snippet = (summary or "")[:200]
                    ticker_tag = f"[{ticker}] " if ticker else ""
                    lines.append(f"  - {ticker_tag}[{publisher}] {title} ({pub_at})")
                    if snippet:
                        lines.append(f"    {snippet}")
        except Exception as e:
            logger.warning("[DISCOVERY] News query failed: %s", e)
    return "\n".join(lines)


def _gather_recent_reddit(hours: int = 48, limit: int = 20) -> str:
    """Pull top Reddit posts from investing subs."""
    with get_db() as db:
        lines = []
        try:
            rows = db.execute(
                """
                SELECT subreddit, title, body, score
                FROM reddit_posts
                WHERE created_utc > CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
                ORDER BY score DESC
                LIMIT %s
                """,
                [hours, limit],
            ).fetchall()
            if rows:
                lines.append(f"## Top Reddit Posts ({len(rows)} posts, last {hours}h)")
                for sub, title, body, score in rows:
                    snippet = (body or "")[:150]
                    lines.append(f"  - r/{sub} (score {score}): {title}")
                    if snippet:
                        lines.append(f"    {snippet}")
        except Exception as e:
            logger.warning("[DISCOVERY] Reddit query failed: %s", e)
    return "\n".join(lines)


def _gather_congress_trades(days: int = 30, limit: int = 20) -> str:
    """Pull recent congressional trades."""
    with get_db() as db:
        lines = []
        try:
            rows = db.execute(
                """
                SELECT politician, party, ticker, transaction_type,
                       amount_range, trade_date
                FROM congress_trades
                WHERE trade_date > CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                ORDER BY trade_date DESC
                LIMIT %s
                """,
                [days, limit],
            ).fetchall()
            if rows:
                lines.append(f"## Congressional Trades ({len(rows)} trades, last {days}d)")
                for pol, party, tkr, txn, amt, dt in rows:
                    lines.append(f"  - {pol} ({party}): {txn} {tkr} [{amt}] ({dt})")
        except Exception as e:
            logger.warning("[DISCOVERY] Congress query failed: %s", e)
    return "\n".join(lines)


def _gather_exclusions() -> tuple[set[str], str]:
    """Get tickers to exclude (already held, on watchlist, or recently discovered)."""
    exclude: set[str] = set()
    lines = []

    with get_db() as db:
        # Current positions
        try:
            from app.services.bot_manager import get_active_bot_id
            bid = get_active_bot_id()
        except Exception:
            from app.config import settings as _cfg
            bid = _cfg.BOT_ID

        try:
            pos_rows = db.execute(
                "SELECT ticker FROM position_lots WHERE status = 'open' AND bot_id = %s",
                [bid],
            ).fetchall()
            pos_tickers = {r[0] for r in pos_rows}
            exclude.update(pos_tickers)
            if pos_tickers:
                lines.append(f"## Currently Held Positions (DO NOT suggest these)")
                lines.append(f"  {', '.join(sorted(pos_tickers))}")
        except Exception:
            pass

        # Active watchlist
        try:
            wl_rows = db.execute(
                "SELECT ticker FROM watchlist WHERE status = 'active'"
            ).fetchall()
            wl_tickers = {r[0] for r in wl_rows}
            exclude.update(wl_tickers)
            if wl_tickers:
                lines.append(f"## Active Watchlist (already tracking, avoid duplicates)")
                lines.append(f"  {', '.join(sorted(wl_tickers))}")
        except Exception:
            pass

        # Recently analyzed (last 24h)
        try:
            recent_rows = db.execute(
                "SELECT DISTINCT ticker FROM analysis_results WHERE created_at > NOW() - INTERVAL '24 hours'"
            ).fetchall()
            recent = {r[0] for r in recent_rows}
            exclude.update(recent)
        except Exception:
            pass

    return exclude, "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# LLM-Powered Extraction
# ─────────────────────────────────────────────────────────────────────


async def _call_discovery_llm(data_snapshot: str) -> str:
    """Call Prism agent to extract ticker opportunities from news data."""
    from app.services.prism_agent_caller import call_prism_agent
    from app.services.vllm_client import Priority

    user_prompt = "--- TODAY'S DATA SNAPSHOT:\n" + data_snapshot

    text, tokens, ms = await call_prism_agent(
        agent_id="CUSTOM_DISCOVERY_AGENT",
        user_message=user_prompt,
        fallback_system_prompt="",  # Loaded dynamically from app.agents.custom
        fallback_agent_name="news_discovery",
        temperature=0.3,
        max_tokens=8192,
        priority=Priority.LOW,
    )

    logger.info(
        "[DISCOVERY] LLM responded: %d chars, %d tokens, %dms",
        len(text), tokens, ms,
    )
    return text


def _parse_discovery_response(raw: str) -> list[dict]:
    """Parse the LLM JSON array response into a list of ticker suggestions."""
    logger.info("[DISCOVERY] Parsing raw response: %s", raw)
    from app.utils.text_utils import parse_json_list_response
    try:
        return parse_json_list_response(raw)
    except Exception as e:
        logger.warning("[DISCOVERY] JSON parse failed: %s | Raw content: %s", e, raw)
        return []


def _validate_ticker(ticker: str, banned: set[str], exclude: set[str]) -> bool:
    """Validate a ticker symbol format and check against blocklists."""
    if not ticker or not isinstance(ticker, str):
        return False
    ticker = ticker.upper().strip()
    if len(ticker) > 5 or len(ticker) < 1:
        return False
    if re.search(r"[0-9\-]", ticker):
        return False
    if ticker in exclude:
        return False
    if ticker in banned:
        return False

    from app.processors.ticker_extractor import FALSE_TICKERS
    if ticker in FALSE_TICKERS:
        return False

    return True


def _save_discovered_tickers(tickers: list[dict]) -> int:
    """Insert validated tickers into discovered_tickers table."""
    if not tickers:
        return 0

    added = 0
    with get_db() as db:
        for item in tickers:
            ticker = item.get("ticker", "").upper().strip()
            source = item.get("source", "news_discovery")[:200]
            reason = item.get("reason", "")[:500]
            conviction = item.get("conviction", "MEDIUM")
            score = 0.8 if conviction == "HIGH" else 0.7

            try:
                db.execute(
                    """
                    INSERT INTO discovered_tickers
                    (ticker, source, context, score, discovered_at)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (ticker) DO UPDATE SET
                        score = GREATEST(discovered_tickers.score, EXCLUDED.score),
                        context = EXCLUDED.context,
                        discovered_at = CURRENT_TIMESTAMP
                    """,
                    [ticker, f"news_discovery: {source}", reason, score],
                )
                added += 1
            except Exception as e:
                logger.warning("[DISCOVERY] Failed to save %s: %s", ticker, e)

    return added


# ─────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────


async def run_news_discovery(
    emit: Callable | None = None,
) -> list[str]:
    """Run the news-driven ticker discovery engine.

    Gathers recent news/reddit/congress data from DB, sends to LLM,
    parses suggested tickers, validates and saves them to discovered_tickers.

    Returns:
        List of newly discovered ticker symbols.
    """
    from app.utils.pipeline_utils import noop as _noop
    if emit is None:
        emit = _noop

    start = time.monotonic()
    logger.info("[DISCOVERY] " + "=" * 60)
    logger.info("[DISCOVERY] NEWS-DRIVEN TICKER DISCOVERY: Starting Phase 0...")
    logger.info("[DISCOVERY] " + "=" * 60)

    emit(
        "collecting", "discovery_start",
        "Phase 0: News-Driven Ticker Discovery starting...",
        status="running",
    )

    # ── 1. Gather data from DB ──
    t0 = time.monotonic()
    sections = []
    sections.append(_gather_recent_news(hours=48, limit=30))
    sections.append(_gather_recent_reddit(hours=48, limit=20))
    sections.append(_gather_congress_trades(days=30, limit=20))

    exclude, exclusion_text = _gather_exclusions()
    if exclusion_text:
        sections.append(exclusion_text)

    data_snapshot = "\n\n".join(s for s in sections if s)
    gather_ms = int((time.monotonic() - t0) * 1000)

    if not data_snapshot.strip() or len(data_snapshot) < 100:
        emit(
            "collecting", "discovery_skip",
            "Phase 0: No recent news/social data in DB — skipping discovery",
            status="skipped",
            elapsed_ms=gather_ms,
        )
        logger.info("[DISCOVERY] No data available for discovery — skipping")
        return []

    emit(
        "collecting", "discovery_data",
        f"Phase 0: Gathered {len(data_snapshot):,} chars of news/social data",
        status="ok",
        elapsed_ms=gather_ms,
    )

    # ── 2. Call LLM for extraction ──
    t0 = time.monotonic()
    emit(
        "collecting", "discovery_llm",
        "Phase 0: LLM extracting ticker opportunities...",
        status="running",
    )

    try:
        import asyncio
        raw_response = await asyncio.wait_for(
            _call_discovery_llm(data_snapshot),
            timeout=300.0,
        )
    except asyncio.TimeoutError:
        llm_ms = int((time.monotonic() - t0) * 1000)
        logger.warning("[DISCOVERY] LLM call timed out after 300s")
        emit(
            "collecting", "discovery_timeout",
            "Phase 0: Discovery LLM timed out (300s) — continuing without new discoveries",
            status="warning",
            elapsed_ms=llm_ms,
        )
        return []
    except Exception as e:
        llm_ms = int((time.monotonic() - t0) * 1000)
        logger.error("[DISCOVERY] LLM call failed: %s", e)
        emit(
            "collecting", "discovery_error",
            f"Phase 0: Discovery LLM failed — {e}",
            status="error",
            elapsed_ms=llm_ms,
        )
        return []

    llm_ms = int((time.monotonic() - t0) * 1000)

    # ── 3. Parse and validate ──
    suggestions = _parse_discovery_response(raw_response)
    if not suggestions:
        emit(
            "collecting", "discovery_empty",
            "Phase 0: LLM found no actionable tickers in today's data",
            status="ok",
            elapsed_ms=llm_ms,
        )
        logger.info("[DISCOVERY] No suggestions from LLM")
        return []

    # Load banned tickers
    banned: set[str] = set()
    try:
        with get_db() as db:
            banned_rows = db.execute("SELECT ticker FROM ticker_bans").fetchall()
            banned = {r[0].upper().strip() for r in banned_rows}
    except Exception:
        pass

    # Validate each suggestion
    valid_suggestions = []
    for item in suggestions:
        ticker = (item.get("ticker") or "").upper().strip()
        if _validate_ticker(ticker, banned, exclude):
            item["ticker"] = ticker
            valid_suggestions.append(item)
        else:
            logger.debug("[DISCOVERY] Rejected ticker: %s (blocked/invalid/duplicate)", ticker)

    if not valid_suggestions:
        emit(
            "collecting", "discovery_filtered",
            f"Phase 0: LLM suggested {len(suggestions)} tickers but all were filtered out (duplicates/blocked)",
            status="ok",
            elapsed_ms=llm_ms,
        )
        return []

    # ── 4. Save to DB ──
    saved = _save_discovered_tickers(valid_suggestions)
    discovered_symbols = [s["ticker"] for s in valid_suggestions]

    total_ms = int((time.monotonic() - start) * 1000)

    emit(
        "collecting", "discovery_done",
        f"Phase 0: Discovered {len(discovered_symbols)} new tickers: {', '.join(discovered_symbols)} ({total_ms / 1000:.1f}s)",
        status="ok",
        data={"tickers": discovered_symbols, "saved": saved},
        elapsed_ms=total_ms,
    )

    logger.info("[DISCOVERY] " + "=" * 60)
    logger.info("[DISCOVERY] DISCOVERY COMPLETE: %d tickers in %dms", len(discovered_symbols), total_ms)
    for s in valid_suggestions:
        logger.info("[DISCOVERY]   %s — %s (%s)", s["ticker"], s.get("reason", ""), s.get("conviction", ""))
    logger.info("[DISCOVERY] " + "=" * 60)

    return discovered_symbols
