"""
Step: Cache Fast-Track.

Checks if a recent thesis (under 24 hours old) exists for the ticker AND 
that no new news, reddit, or youtube data has been scraped since then.
If both conditions are met, it sets `ctx.fast_track_cache = True` to skip 
the entire LLM pipeline and save tokens.
"""

import logging
import time
import datetime
import json

from app.ticker_pipeline.context import TickerContext
from app.db.connection import get_db

logger = logging.getLogger(__name__)


async def run_cache_step(ctx: TickerContext) -> TickerContext:
    """Check if the ticker can skip the LLM pipeline."""
    t0 = time.monotonic()
    ctx.fast_track_cache = False

    try:
        with get_db() as db:
            # 1. Get the most recent analysis
            latest = db.execute(
                """
                SELECT id, action, confidence, thesis_summary, result_json, created_at 
                FROM analysis_results 
                WHERE ticker = %s AND action IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                [ctx.ticker],
            ).fetchone()

            if not latest:
                return ctx

            latest_id, action, confidence, rationale, result_json_str, created_at = latest

            # Ensure created_at has tzinfo
            if isinstance(created_at, str):
                try:
                    created_at = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except ValueError:
                    return ctx
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=datetime.timezone.utc)

            # 2. Check age (< 24 hours)
            age_seconds = (datetime.datetime.now(datetime.timezone.utc) - created_at).total_seconds()
            if age_seconds > 86400:
                logger.debug("[V2] Cache skip rejected for %s: last report is too old (%.1fh)", ctx.ticker, age_seconds / 3600)
                return ctx

            # 3. Check for new scraped data
            new_news = db.execute(
                "SELECT 1 FROM news_articles WHERE ticker = %s AND created_at > %s LIMIT 1",
                [ctx.ticker, created_at]
            ).fetchone()
            if new_news:
                return ctx

            new_reddit = db.execute(
                "SELECT 1 FROM reddit_posts WHERE ticker = %s AND created_at > %s LIMIT 1",
                [ctx.ticker, created_at]
            ).fetchone()
            if new_reddit:
                return ctx

            new_yt = db.execute(
                "SELECT 1 FROM youtube_transcripts WHERE ticker = %s AND created_at > %s LIMIT 1",
                [ctx.ticker, created_at]
            ).fetchone()
            if new_yt:
                return ctx

            # ── CACHE HIT ──
            ctx.fast_track_cache = True
            ctx.final_action = action
            ctx.final_confidence = confidence
            ctx.final_rationale = rationale

            try:
                res_dict = json.loads(result_json_str) if result_json_str else {}
                ctx.agent_results = res_dict.get("agent_results", {})
                if "c_result" in res_dict and isinstance(res_dict["c_result"], dict):
                    c_res = res_dict["c_result"]
                    # Fake a thesis object for downstream stages just in case
                    class _FakeThesis:
                        action = c_res.get("thesis_action")
                        confidence = c_res.get("thesis_confidence", 0)
                        rationale = rationale
                        core_claims = []
                        weaknesses = []
                    ctx.thesis = _FakeThesis()
            except Exception as e:
                logger.warning("[V2] Failed to parse cached result_json for %s: %s", ctx.ticker, e)

            ms = ctx.elapsed_ms(t0)
            ctx.add_stage("cache_hit", ms)
            
            logger.info("[V2] ⚡ FAST-TRACK CACHE HIT for %s — skipping LLM pipeline", ctx.ticker)
            ctx.safe_emit(
                "analyzing", f"v2_cache_hit_{ctx.ticker}",
                f"⚡ {ctx.ticker}: Cache hit! Using recent report from {created_at.strftime('%Y-%m-%d %H:%M:%S UTC')} (no new data)",
                status="ok",
                elapsed_ms=ms,
            )

    except Exception as e:
        logger.warning("[V2] Cache check failed for %s (non-fatal): %s", ctx.ticker, e)

    return ctx
