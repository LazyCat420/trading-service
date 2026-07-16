import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.services.pipeline_state import PipelineStateDB
from app.v3.orchestrator import run_v3_pipeline
from app.telemetry import send_system_log
from app.utils.us_ticker_resolver import (
    is_us_tradeable,
    resolve_to_us_ticker,
    resolve_tickers_batch,
    resolve_tickers_batch_async,
)

logger = logging.getLogger(__name__)


class _ExplicitTickersPinned(Exception):
    """Control-flow sentinel: an explicit ticker list was requested, so the
    discovery/scoring/freshness/gatekeeper funnel is skipped entirely."""


# no_trade_reason vocabulary. These strings are simultaneously (a) persisted
# per-row in result_json, (b) control-flow keys (trigger registration), and
# (c) counted by summarize_ticker_results — set and match them ONLY through
# these constants or the buckets silently drop to zero on a rename.
POLICY_BLOCKED_PREFIX = "HOLD_POLICY_BLOCKED"          # minted by orchestrator policy gates
REASON_CONFIDENCE_BLOCKED = "CONFIDENCE_BELOW_THRESHOLD"
REASON_WATCH_ONLY = "AGENT_SIZE_ZERO_WATCH_ONLY"
REASON_DRAWDOWN_BREAKER = "DRAWDOWN_BREAKER"
REASON_TRADE_DISABLED = "TRADE_DISABLED"
REASON_NO_POSITION = "SELL_NO_POSITION"
TRADE_ERROR_PREFIX = "TRADE_ERROR:"


def resolve_no_trade_reason(trade_res: dict) -> str:
    """Map a paper-trader refusal dict to a no_trade_reason tag."""
    if trade_res.get("reason_code") == REASON_DRAWDOWN_BREAKER or "drawdown_pct" in trade_res:
        return REASON_DRAWDOWN_BREAKER
    return f"{TRADE_ERROR_PREFIX} {str(trade_res.get('error'))[:200]}"


def resolve_buy_size_pct(
    agent_size_pct: Any,
    confidence: int | float,
    max_position_size_pct: float,
) -> float | None:
    """Resolve the BUY position size (fraction of cash) from the agents' decision.

    Deferred-item 8.1 decision (2026-07-15): an EXPLICIT position_size_pct <= 0
    from the board/synthesizer means "watch, don't trade" — returns None and no
    trade is attempted. Only a missing/non-numeric size falls back to the
    confidence formula.
    """
    if isinstance(agent_size_pct, bool):
        agent_size_pct = None  # bools are ints in Python; never a size
    if isinstance(agent_size_pct, (int, float)):
        if agent_size_pct <= 0:
            return None  # deliberate watch-only directive
        return min(agent_size_pct / 100.0, max_position_size_pct)
    # No agent-decided size — confidence-scaled fallback
    return max(0.02, min(max_position_size_pct, confidence / 100.0 * 0.10))


def resolve_trigger_registration(
    policy_action: str,
    action: str,
    trade_executed: bool,
    position_held: bool,
    watch_only: bool,
) -> dict[str, bool]:
    """Decide which price triggers may be registered for this decision.

    Deferred-item 8.3 decision (2026-07-15): policy-blocked trades register
    NOTHING — a gate refusal must not leave standing orders behind. For
    allowed decisions, SELL-side triggers (stop-loss / take-profit) require an
    actual position (bought this cycle or already held); the dynamic re-analysis
    trigger is the designed "watch for entry" mechanism and stays available,
    including for an explicit size-0 watch-only decision (it spawns a fresh
    analysis cycle, never a blind trade).
    """
    if policy_action.startswith("HOLD_POLICY_BLOCKED"):
        return {"sell_side": False, "dynamic": False}
    has_position = position_held or (trade_executed and action == "BUY")
    if action == "SELL" and trade_executed:
        has_position = False  # position just closed — SELL-side orders are stale
    return {"sell_side": has_position, "dynamic": True}


def summarize_ticker_results(results) -> dict:
    """Aggregate per-ticker result dicts (from _process_ticker) into the
    action/trade counts recorded in cycle_run_summaries. Non-dict entries
    (None from skipped tickers, Exceptions from gather) are ignored."""
    rs = [r for r in (results or []) if isinstance(r, dict)]
    actions = [(r.get("action") or "").upper() for r in rs]
    reasons = [str(r.get("no_trade_reason") or "") for r in rs]
    return {
        "analysis_results_count": len(actions),
        "buy_count": actions.count("BUY"),
        "sell_count": actions.count("SELL"),
        "hold_count": actions.count("HOLD"),
        "trade_attempted": sum(1 for r in rs if r.get("trade_attempted")),
        "trade_executed": sum(1 for r in rs if r.get("trade_executed")),
        "trade_failed": sum(1 for r in rs if r.get("trade_failed")),
        # A BUY/SELL that never traded is not a HOLD — bucket the reasons so
        # the dashboard/auditor can tell "no signal" from "signal, but blocked".
        "policy_blocked": sum(1 for x in reasons if x.startswith(POLICY_BLOCKED_PREFIX)),
        "confidence_blocked": reasons.count(REASON_CONFIDENCE_BLOCKED),
        "watch_only": reasons.count(REASON_WATCH_ONLY),
        "breaker_blocked": reasons.count(REASON_DRAWDOWN_BREAKER),
        "no_position_blocked": reasons.count(REASON_NO_POSITION),
        "trade_errors": sum(1 for x in reasons if x.startswith(TRADE_ERROR_PREFIX)),
    }


class PipelineService:
    _state = PipelineStateDB.default_state()
    _cycle_task = None
    _stop_requested = False

    @classmethod
    def load_state(cls, summary_only: bool = False):
        cls._state = PipelineStateDB.get_state(summary_only)

    @classmethod
    def save_state(cls):
        PipelineStateDB.save_state(cls._state)

    @classmethod
    def get_current_state(cls, summary_only: bool = False) -> dict:
        return PipelineStateDB.get_state(summary_only)

    @classmethod
    async def start_cycle(cls, tickers: list[str], **kwargs):
        # Read from DB for dedup — in-memory _state can be stale after
        # force-reset or container restart.
        db_state = PipelineStateDB.get_state(summary_only=True)
        db_status = db_state.get("status", "idle")
        if db_status in ("running", "starting", "stopping"):
            # Orphan detection: DB says active but in-memory task is gone.
            # This happens when the container restarts while a cycle is running,
            # or when an exception kills the task without cleaning up state.
            if cls._cycle_task is None or cls._cycle_task.done():
                logger.warning(
                    "[PipelineService] ORPHANED STATE DETECTED: DB says '%s' but "
                    "no in-memory task exists. Checking started_at for auto-clear.",
                    db_status,
                )
                started_at = db_state.get("started_at")
                is_stale = False
                if started_at:
                    if isinstance(started_at, str):
                        try:
                            from dateutil.parser import parse as parse_date
                            started_at = parse_date(started_at)
                        except Exception:
                            pass
                    if isinstance(started_at, datetime):
                        if started_at.tzinfo is None:
                            started_at = started_at.replace(tzinfo=timezone.utc)
                        delta = datetime.now(timezone.utc) - started_at
                        if delta.total_seconds() > 1800: # 30 minutes
                            is_stale = True
                
                if is_stale:
                    logger.warning(
                        "[PipelineService] Auto-clearing orphaned state older than 30 minutes (started_at=%s).",
                        started_at,
                    )
                    await cls.force_reset()
                else:
                    return {
                        "status": "error",
                        "message": (
                            f"Pipeline state is stuck at '{db_status}' from a previous "
                            f"crashed cycle (cycle_id={db_state.get('cycle_id', '?')}). "
                            f"Error: {db_state.get('error', 'unknown')}. "
                            f"Use Force Reset to clear the stuck state before starting a new cycle."
                        ),
                    }
            else:
                return {"status": "deduplicated", "message": f"Cycle already {db_status}"}
        # Also check in-memory task to catch race where DB was reset but task is still running
        elif cls._cycle_task and not cls._cycle_task.done():
            return {"status": "deduplicated", "message": "Cycle task still running"}

        # Reset the SDK kill switch so requests can flow on the new cycle
        try:
            from lazycat.llm import PrismClient
            # Assuming trading-service uses a singleton prism_client from somewhere
            # Let's import it from prism_agent_caller
            from app.services.prism_agent_caller import prism_client
            prism_client.reset_kill_switch()
        except Exception as e:
            logger.error("[PipelineService] Failed to reset VLLM kill switch: %s", e)

        cycle_id = kwargs.get("cycle_id") or f"cycle-v3-{int(time.time())}"
        max_tickers = kwargs.get("max_tickers")  # None → auto (gatekeeper default)
        agent_locale = kwargs.get("agent_locale") or "default"
        prism_overrides = kwargs.get("prism_overrides") or {}

        # Payload knobs must not silently no-op (2026-07-15 audit: typo'd or
        # unsupported keys vanished without a trace).
        _known_keys = {
            "cycle_id", "tickers", "max_tickers", "trade", "analyze", "collect",
            "start_fresh", "agent_locale", "prism_overrides", "pipeline_version",
            "benchmark_group", "discovered_tickers",
        }
        _unknown = set(kwargs) - _known_keys
        if _unknown:
            logger.warning(
                "[PipelineService] Unknown START_CYCLE payload keys (ignored): %s",
                sorted(_unknown),
            )
        _unknown_ov = set(prism_overrides) - {"prism_auto_approve", "tool_domain_blocklist"}
        if _unknown_ov:
            logger.warning(
                "[PipelineService] Unknown prism_overrides keys (ignored): %s",
                sorted(_unknown_ov),
            )
        if prism_overrides.get("tool_domain_blocklist"):
            logger.warning(
                "[PipelineService] tool_domain_blocklist only filters dynamically "
                "discovered tools — V3 agents use static whitelists, so it has no "
                "effect on their tool set."
            )

        cls._state.update({
            "status": "starting",
            "cycle_id": cycle_id,
            "agent_locale": agent_locale,
            "prism_overrides": prism_overrides,
            "progress": f"Screening watchlist for top {max_tickers or 'auto'} setups...",
            # Persist the requested flags so /status reflects this cycle's payload
            # instead of whatever fossil values the columns held (they had no writer).
            "collect_flag": bool(kwargs.get("collect", True)),
            "analyze_flag": bool(kwargs.get("analyze", True)),
            "trade_flag": bool(kwargs.get("trade", True)),
            "requested_pipeline_version": str(kwargs.get("pipeline_version", "v3")),
        })
        cls.save_state()
        cls._stop_requested = False

        # ── US Ticker Gate: resolve foreign tickers before they enter the pipeline ──
        if tickers:
            original_tickers = list(tickers)
            tickers = resolve_tickers_batch(tickers)
            dropped = set(original_tickers) - set(tickers)
            if dropped:
                logger.warning(
                    "[PipelineService] US Ticker Gate dropped/resolved foreign tickers at entry: %s → %s",
                    original_tickers, tickers,
                )

        clean_kwargs = {k: v for k, v in kwargs.items() if k not in ("cycle_id", "tickers", "max_tickers", "agent_locale")}
        try:
            cls._cycle_task = asyncio.create_task(cls._run_all_v3(cycle_id, tickers, max_tickers, agent_locale=agent_locale, **clean_kwargs))
        except Exception as e:
            logger.error("[PipelineService] Failed to spawn cycle task: %s", e)
            cls._state.update({"status": "error", "error": str(e)})
            cls.save_state()
            raise
        return {"status": "starting", "cycle_id": cycle_id, "message": "V3 pipeline started"}

    @classmethod
    async def _run_all_v3(cls, cycle_id: str, tickers: list[str], max_tickers: int | None = None, agent_locale: str = "default", **kwargs):
        # Captured up-front so summaries can be written even when the cycle
        # fails or is cancelled before reaching the success path.
        t0 = time.monotonic()
        requested_tickers = list(tickers or [])
        collect_flag = bool(kwargs.get("collect", True))
        trade_flag = bool(kwargs.get("trade", True))

        def _persist_summary(status: str, tickers_final, results=None, error: str | None = None,
                             report_generated: bool = False):
            """Write the cycle_run_summaries row. Counts come from the per-ticker
            result dicts returned by _process_ticker (None entries are skipped)."""
            try:
                from app.log_manager import log_manager

                summary = {
                    "report_generated": report_generated,
                    "trigger_type": "v3",
                    "started_at": cls._state.get("started_at"),
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "status": status,
                    "tickers_requested": requested_tickers or list(tickers_final or []),
                    "tickers": list(tickers_final or []),
                    "tickers_final": list(tickers_final or []),
                    "collect_flag": collect_flag,
                    "analyze_flag": True,
                    "trade_flag": trade_flag,
                    "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    "no_trade_reason": None if trade_flag else "trade_disabled",
                    "primary_failure_reason": error,
                    **summarize_ticker_results(results),
                }
                # The dedicated column readers (debug_cycle.py, audits) need the
                # buckets outside summary_json too.
                summary["trade_skip_categories"] = {
                    k: summary.get(k, 0)
                    for k in ("policy_blocked", "confidence_blocked", "watch_only",
                              "breaker_blocked", "no_position_blocked", "trade_errors")
                }
                # A trade-enabled cycle where every verdict was HOLD and nothing
                # was attempted is a 'hold_only' cycle — leaving the reason NULL
                # made it indistinguishable from an unexplained drop.
                if (
                    trade_flag
                    and summary.get("analysis_results_count")
                    and not summary.get("trade_attempted")
                    and summary.get("hold_count") == summary.get("analysis_results_count")
                ):
                    summary["no_trade_reason"] = "hold_only"
                log_manager.log_cycle_summary(cycle_id, summary)
                return summary
            except Exception as sum_err:
                logger.error("[PipelineService] Failed to persist cycle summary: %s", sum_err)
                return None

        try:
            # ── Set prism_client.url ONCE for the entire cycle ──
            # This prevents a race condition where concurrent agent calls
            # stomp on the global singleton URL. All agents in a V3 cycle
            # use the same harness_provider, so we resolve it here.
            # Mirror the boot_service.py logic exactly:
            #   PRISM_ENABLED=True  → PRISM_URL (which may include /prism-proxy)
            #   PRISM_ENABLED=False → bare http://{host}:7778
            from lazycat.llm import prism_client
            from app.config.config import settings as _cfg
            if _cfg.PRISM_ENABLED:
                prism_client.url = _cfg.PRISM_URL
            else:
                prism_client.url = f"http://{_cfg.DEFAULT_HOST}:7778"
            # Cycle boundary: drop all cached sessions/conversations so a new
            # cycle can never silently continue a previous cycle's conversation
            # (the no-session_id group_key is content-hashed and collides when
            # first messages repeat across cycles).
            prism_client.cleanup_all_sessions()
            logger.info("[PipelineService] Cycle %s: prism_client.url set to %s (PRISM_ENABLED=%s)", cycle_id, prism_client.url, _cfg.PRISM_ENABLED)

            def emit(phase: str, step: str, detail: str, **kwargs):
                event = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "phase": phase,
                    "step": step,
                    "detail": detail,
                    "status": kwargs.pop("status", "running"),
                    "data": kwargs.pop("data", {}),
                    "elapsed_ms": kwargs.pop("elapsed_ms", 0),
                }
                event.update(kwargs)
                logger.info(f"[{cycle_id}][{phase}][{step}] {detail}")
                PipelineStateDB.append_events(cycle_id, [event])
                
                try:
                    send_system_log("AGENT", detail)
                except Exception as sys_log_err:
                    logger.warning(f"[PipelineService] Failed to send system log: {sys_log_err}")
                
                try:
                    current_status = cls._state.get("status", "")
                    if current_status in ("error", "stopped", "done", "idle"):
                        return
                    cls._state.update({
                        "status": "running",
                        "progress": f"[{phase.upper()}] {detail}",
                        "phase": phase
                    })
                    cls.save_state()
                except Exception as db_sync_err:
                    logger.warning("[PipelineService] Failed to sync progress to DB: %s", db_sync_err)

            # 1. Run Gatekeeper

            try:
                from app.trading.watchlist import get_active
                from app.utils.batch_screener import get_watchlist_snapshots
                from app.agents.base_agent import run_agent
                from app.v3.agents.portfolio_manager import SYSTEM_PROMPT, AGENT_NAME
                import json
                
                if tickers:
                    # Explicit ticker request: honor it exactly. The discovery/
                    # scoring/freshness/gatekeeper funnel below treats requested
                    # tickers as mere candidates and can replace the whole list,
                    # so it is skipped for explicit requests.
                    if max_tickers:
                        tickers = list(tickers)[:max_tickers]
                    emit(
                        "gatekeeper", "explicit_tickers",
                        f"🎯 Explicit ticker request honored: {tickers} (discovery & gatekeeper bypassed)",
                        status="ok",
                    )
                    raise _ExplicitTickersPinned()

                base_tickers = [t["ticker"] for t in get_active()]

                # --- DISCOVERY ENGINE ---
                active_ticker_dicts = []
                
                # Dynamic scraper run at the start of auto-discovery
                if not tickers:
                    def discovery_emit(step: str, detail: str, status: str = "running"):
                        event = {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "phase": "discovery",
                            "step": step,
                            "detail": detail,
                            "status": status,
                            "data": {}
                        }
                        logger.info(f"[{cycle_id}][discovery][{step}] {detail}")
                        PipelineStateDB.append_events(cycle_id, [event])
                        try:
                            send_system_log("AGENT", detail)
                        except Exception as sys_log_err:
                            logger.warning(f"[PipelineService] Failed to send system log: {sys_log_err}")
                        
                    async def run_scraper_sync():
                        try:
                            from app.collectors.news_collector import collect_all
                            total_scraped = await collect_all(limit_feeds=10, emit_cb=discovery_emit)
                            discovery_emit("scraper_done", f"✅ News scraper sweep complete: collected {total_scraped} articles", "ok")
                        except Exception as e:
                            logger.error(f"[PipelineService] Discovery scraping failed: {e}")
                            discovery_emit("scraper_err", f"❌ Scraper sweep failed: {e}", "error")

                    discovery_emit("scraper_start", "📡 Starting news scraper sweep... This will take 1-2 minutes.", "running")
                    await run_scraper_sync()
                # Find trending tickers from the last 24h (News, Reddit, YouTube) that aren't in the static watchlist
                try:
                    from app.db.connection import get_db
                    from app.processors.ticker_extractor import FALSE_TICKERS
                    with get_db() as db:
                        # 1. Pull Trending from each source independently
                        news_trends = db.execute("""
                            SELECT ticker, COUNT(*) as mentions FROM news_articles 
                            WHERE ticker IS NOT NULL AND published_at > NOW() - INTERVAL '24 hours'
                            GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 10
                        """).fetchall()
                        reddit_trends = db.execute("""
                            SELECT ticker, COUNT(*) as mentions FROM reddit_posts 
                            WHERE ticker IS NOT NULL AND created_utc > NOW() - INTERVAL '24 hours'
                            GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 10
                        """).fetchall()
                        youtube_trends = db.execute("""
                            SELECT ticker, COUNT(*) as mentions FROM youtube_transcripts 
                            WHERE ticker IS NOT NULL AND published_at > NOW() - INTERVAL '24 hours'
                            GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 5
                        """).fetchall()
                        
                        # 2. Phase 4A: Cross-reference — track source count per ticker
                        source_tracker: dict[str, dict] = {}  # ticker -> {"sources": set, "mentions": int}
                        for rows, source_label in [
                            (news_trends, "News"),
                            (reddit_trends, "Reddit"),
                            (youtube_trends, "YouTube"),
                        ]:
                            for row in rows:
                                tkr = row[0].upper().strip()
                                if not tkr or tkr in base_tickers:
                                    continue
                                # Phase 4A: FALSE_TICKERS pre-filter
                                if tkr in FALSE_TICKERS:
                                    logger.debug("[PipelineService] Filtered out FALSE_TICKER: %s from %s", tkr, source_label)
                                    continue
                                # Phase 4E: Foreign ticker filter — reject non-US tickers from discovery
                                if not is_us_tradeable(tkr):
                                    logger.debug("[PipelineService] Filtered foreign ticker from discovery: %s from %s", tkr, source_label)
                                    continue
                                if tkr not in source_tracker:
                                    source_tracker[tkr] = {"sources": set(), "mentions": 0}
                                source_tracker[tkr]["sources"].add(source_label)
                                source_tracker[tkr]["mentions"] += row[1] if len(row) > 1 else 1
                        
                        # 3. Phase 4A: Build trending_discovered with source counts
                        trending_discovered = {}
                        for tkr, info in source_tracker.items():
                            source_count = len(info["sources"])
                            source_label = f"Trending {'+'.join(sorted(info['sources']))}"
                            if source_count >= 2:
                                source_label += f" ({source_count} sources)"
                            trending_discovered[tkr] = {
                                "label": source_label,
                                "source_count": source_count,
                                "total_mentions": info["mentions"],
                            }
                        
                        # Phase 4C: Institutional Discovery — tickers with hedge fund consensus
                        try:
                            from app.collectors.fund_scanner import get_top_conviction_tickers
                            institutional_leads = get_top_conviction_tickers(min_funds=2, max_results=20)
                            for lead in institutional_leads:
                                tkr = lead["ticker"]
                                if tkr in base_tickers:
                                    continue  # already in watchlist
                                if tkr not in source_tracker:
                                    source_tracker[tkr] = {"sources": set(), "mentions": 0}
                                source_tracker[tkr]["sources"].add("Institutional")
                                source_tracker[tkr]["mentions"] += lead["fund_count"]
                                # Also add to trending_discovered if not already there
                                if tkr not in trending_discovered:
                                    sc = len(source_tracker[tkr]["sources"])
                                    src_label = f"Institutional ({lead['fund_count']} funds)"
                                    if sc >= 2:
                                        src_label = f"Trending {'+'.join(sorted(source_tracker[tkr]['sources']))} ({sc} sources)"
                                    trending_discovered[tkr] = {
                                        "label": src_label,
                                        "source_count": sc,
                                        "total_mentions": source_tracker[tkr]["mentions"],
                                    }
                            if institutional_leads:
                                logger.info(
                                    "[PipelineService] Institutional Discovery: %d conviction leads (top: %s)",
                                    len(institutional_leads),
                                    [l["ticker"] for l in institutional_leads[:5]],
                                )
                        except Exception as e:
                            logger.warning("[PipelineService] Institutional discovery failed (non-fatal): %s", e)

                        all_pool = {t: {"label": "Watchlist", "source_count": 0, "total_mentions": 0} for t in base_tickers}
                        all_pool.update(trending_discovered)
                        
                        # 4. Fetch Last Analysis Date for all
                        if all_pool:
                            placeholders = ','.join(['%s'] * len(all_pool))
                            last_analysis_rows = db.execute(f"""
                                SELECT ticker, MAX(created_at) as last_date 
                                FROM analysis_results 
                                WHERE ticker IN ({placeholders}) 
                                GROUP BY ticker
                            """, list(all_pool.keys())).fetchall()
                            
                            last_analysis_map = {r[0]: r[1] for r in last_analysis_rows}
                        else:
                            last_analysis_map = {}
                            
                        # 5. Construct dictionary structure
                        for tkr, info in all_pool.items():
                            last_date = last_analysis_map.get(tkr)
                            if last_date:
                                if last_date.tzinfo is None:
                                    last_date = last_date.replace(tzinfo=timezone.utc)
                                days_ago = (datetime.now(timezone.utc) - last_date).days
                                dsa_str = f"{days_ago} days ago" if days_ago > 0 else "Today"
                            else:
                                dsa_str = "Never"
                                
                            active_ticker_dicts.append({
                                "ticker": tkr,
                                "source": info["label"],
                                "days_since_analysis": dsa_str,
                                "source_count": info["source_count"],
                                "total_mentions": info["total_mentions"],
                            })
                            
                        if trending_discovered:
                            multi_source = [t for t, i in trending_discovered.items() if i["source_count"] >= 2]
                            logger.info(
                                "[PipelineService] Discovery Engine: %d trending leads (%d multi-source: %s)",
                                len(trending_discovered), len(multi_source), multi_source[:5],
                            )
                except Exception as e:
                    logger.error(f"[PipelineService] Discovery Engine failed to fetch trends: {e}")
                # ------------------------

                if not active_ticker_dicts:
                    logger.warning("[PipelineService] Watchlist is empty, falling back to default.")
                    tickers = ["AAPL"]
                else:
                    _, raw_results = await get_watchlist_snapshots(active_ticker_dicts)
                    
                    if not raw_results:
                        logger.warning("[PipelineService] No valid data returned from yfinance screener.")
                        tickers = ["AAPL"]
                    else:
                        # --- SCORING ENGINE ---
                        scored_results = []
                        # Build a lookup for source_count from active_ticker_dicts
                        source_count_map = {d["ticker"]: d.get("source_count", 0) for d in active_ticker_dicts}
                        
                        # Phase 4D: Pre-fetch institutional signals for scoring boost
                        inst_signal_cache = {}
                        try:
                            from app.collectors.fund_scanner import get_institutional_signal
                            for t, px, chg, rvol, sma, rsi, src, dsa in raw_results:
                                inst_signal_cache[t] = get_institutional_signal(t)
                        except Exception as e:
                            logger.warning("[PipelineService] Institutional signal pre-fetch failed (non-fatal): %s", e)
                        
                        # raw_results format: (t, px, chg, rvol, sma, rsi, src, dsa)
                        for t, px, chg, rvol, sma, rsi, src, dsa in raw_results:
                            score = rvol * 10.0
                            
                            if "Trending" in src:
                                score += 15.0
                            
                            # Phase 4A: Multi-source cross-reference boost
                            sc = source_count_map.get(t, 0)
                            if sc >= 2:
                                score += (sc - 1) * 10.0  # +10 per additional source
                            
                            # Phase 4D: Institutional conviction boost
                            inst = inst_signal_cache.get(t, {})
                            inst_fund_count = inst.get("fund_count", 0)
                            if inst_fund_count >= 3:
                                score += 20.0  # Strong consensus
                            elif inst_fund_count >= 2:
                                score += 10.0  # Moderate consensus
                            if inst.get("has_new_position"):
                                score += 15.0  # Fresh institutional interest
                            if inst.get("has_top_performer"):
                                score += 10.0  # Top-performer conviction
                                
                            # Recency penalty: penalize score if analyzed in the last 3 days
                            last_date = last_analysis_map.get(t)
                            if last_date:
                                if last_date.tzinfo is None:
                                    last_date = last_date.replace(tzinfo=timezone.utc)
                                days_ago = (datetime.now(timezone.utc) - last_date).days
                                if days_ago <= 0:
                                    score -= 30.0
                                elif days_ago == 1:
                                    score -= 20.0
                                elif days_ago == 2:
                                    score -= 10.0

                            scored_results.append({
                                "ticker": t, "price": px, "chg": chg, "rvol": rvol, 
                                "sma": sma, "rsi": rsi, "src": src, "dsa": dsa, "score": score,
                                "inst_funds": inst_fund_count,
                            })
                            
                        # Sort by score descending and take top 20
                        scored_results.sort(key=lambda x: x["score"], reverse=True)
                        top_scorers = scored_results[:20]
                        
                        logger.info(f"[PipelineService] Scoring Engine top picks: {[s['ticker'] for s in top_scorers]}")
                        
                        # Phase 4B: Fetch past verdicts for top 20
                        placeholders = ','.join(['%s'] * len(top_scorers))
                        with get_db() as db:
                            past_results_rows = db.execute(f"""
                                SELECT DISTINCT ON (ticker) ticker, action, confidence, reasoning, created_at
                                FROM trade_results
                                WHERE ticker IN ({placeholders})
                                ORDER BY ticker, created_at DESC
                            """, [s['ticker'] for s in top_scorers]).fetchall()
                            past_results_map = {r[0]: {"action": r[1], "conf": r[2], "reason": r[3]} for r in past_results_rows}
                        
                        # ── FRESHNESS GATE: classify stocks as NEW/CHANGED/STALE ──
                        from app.services.freshness_gate import run_freshness_gate
                        gate_result = run_freshness_gate(
                            top_scorers=top_scorers,
                            last_analysis_map=last_analysis_map,
                            emit=emit,
                        )
                        eligible_stocks = gate_result["eligible"]
                        stale_stocks = gate_result["stale"]

                        # Log stale skips to pipeline events
                        if stale_stocks:
                            PipelineStateDB.append_events(cycle_id, [{
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "phase": "freshness_gate",
                                "step": "STALE_SKIPPED",
                                "detail": f"Auto-skipped {len(stale_stocks)} stale stocks: {[s['ticker'] for s in stale_stocks]}",
                                "status": "filtered",
                                "data": {t["ticker"]: {"delta": t.get("delta_score", 0), "reason": t.get("skip_reason", "")} for t in stale_stocks},
                            }])

                        # ── DISCOVERY MODE: if < 3 eligible, find new leads ──
                        if len(eligible_stocks) < 3:
                            logger.info("[PipelineService] Only %d eligible stocks — triggering Discovery Mode", len(eligible_stocks))
                            from app.services.discovery_mode import run_discovery
                            discoveries = await run_discovery(
                                existing_tickers=[s["ticker"] for s in top_scorers],
                                emit=emit,
                            )
                            if discoveries:
                                eligible_stocks.extend(discoveries)
                                logger.info("[PipelineService] Discovery Mode added %d leads: %s",
                                            len(discoveries), [d["ticker"] for d in discoveries])
                                PipelineStateDB.append_events(cycle_id, [{
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    "phase": "discovery_mode",
                                    "step": "NEW_LEADS",
                                    "detail": f"Discovery Mode found {len(discoveries)} new leads: {[d['ticker'] for d in discoveries]}",
                                    "status": "discovered",
                                }])

                        # Build markdown table for Gatekeeper with ONLY eligible stocks
                        pm_stocks = eligible_stocks if eligible_stocks else top_scorers[:5]  # Fallback: top 5 if nothing eligible
                        md_lines = [
                            "| Ticker | Score | Source | Freshness | Price | Change % | Rel Vol | SMA-20 | RSI | Inst. Funds | Past Verdict | Past Reason |",
                            "|--------|-------|--------|-----------|-------|----------|---------|--------|-----|-------------|--------------|-------------|"
                        ]
                        for s in pm_stocks:
                            sma_rel = ((s["price"] - s["sma"]) / s["sma"]) * 100 if s.get("sma", 0) > 0 else 0
                            past = past_results_map.get(s["ticker"])
                            past_verdict = f"{past['action']} ({past['conf']}%)" if past else "N/A"
                            past_reason = (past['reason'][:100] + "...").replace('|', '') if past and past.get('reason') else "N/A"
                            inst_str = f"{s.get('inst_funds', 0)}" if s.get('inst_funds', 0) > 0 else "-"
                            freshness_str = s.get("freshness", "NEW")
                            md_lines.append(f"| {s['ticker']} | {s.get('score', 0):.1f} | {s.get('src', 'N/A')} | {freshness_str} | ${s.get('price', 0):.2f} | {s.get('chg', 0):+.2f}% | {s.get('rvol', 0):.2f}x | {sma_rel:+.2f}% | {s.get('rsi', 50):.1f} | {inst_str} | {past_verdict} | {past_reason} |")
                            
                        snapshot_table = "\n".join(md_lines)
                        # -----------------------
                    
                    max_tickers = max_tickers or 15
                    min_tickers = min(5, max_tickers)
                    system_prompt = SYSTEM_PROMPT.replace("{min_tickers}", str(min_tickers)).replace("{max_tickers}", str(max_tickers))
                    stock_count = len(pm_stocks)
                    user_prompt = f"Here are {stock_count} stocks that passed our Freshness Gate (all have new data or material changes):\n\n{snapshot_table}\n\nIMPORTANT: You must output ONLY a valid JSON object. Do NOT output any conversational text or formatting blocks. Your response must begin with {{ and end with }}."
                    
                    from app.services.bot_manager import get_active_bot_id
                    active_bot_id = get_active_bot_id()
                    
                    from app.utils.text_utils import parse_json_response
                    # Wrap gatekeeper in a timeout to prevent indefinite hangs
                    try:
                        result = await asyncio.wait_for(
                            run_agent(
                                agent_name=AGENT_NAME,
                                ticker="WATCHLIST",
                                cycle_id=cycle_id,
                                bot_id=active_bot_id,
                                system_prompt=system_prompt,
                                user_prompt=user_prompt,
                                enable_tools=False, # DISABLED tools so it strictly outputs JSON!

                            ),
                            timeout=180.0,
                        )
                    except asyncio.TimeoutError:
                        logger.error("[PipelineService] Gatekeeper LLM call timed out after 180s — falling back to top scorers")
                        fallback_tickers = [s["ticker"] for s in top_scorers[:max_tickers]]
                        logger.warning("[PipelineService] Timeout fallback: using top %d scorers: %s", len(fallback_tickers), fallback_tickers)
                        result = {"response": json.dumps({"selected_tickers": fallback_tickers, "rationale": "Gatekeeper timed out — auto-selected by scoring engine"})}
                    
                    final_text = result.get("response", "{}")
                    logger.info("[PipelineService] Raw gatekeeper response: %s", final_text)
                    parsed = parse_json_response(final_text)
                    logger.info("[PipelineService] Parsed gatekeeper JSON: %s", parsed)
                    if not parsed:
                        parsed = {}
                        
                    selected = parsed.get("selected_tickers", [])
                    rationale = parsed.get("rationale", "")
                    
                    # Validate: drop any tickers not in the known pool
                    if selected and all_pool:
                        valid_selected = [t for t in selected if t in all_pool]
                        invalid = set(selected) - set(valid_selected)
                        if invalid:
                            logger.warning("[PipelineService] Gatekeeper hallucinated tickers (dropped): %s", invalid)
                        selected = valid_selected
                    
                    if selected:
                        # Hard cap: the prompt asks the gatekeeper for at most
                        # max_tickers, but LLM output isn't guaranteed to comply.
                        if len(selected) > max_tickers:
                            logger.warning(
                                "[PipelineService] Gatekeeper over-selected (%d > max %d) — truncating: %s",
                                len(selected), max_tickers, selected[max_tickers:],
                            )
                            selected = selected[:max_tickers]
                        # ── US Ticker Gate: resolve any foreign tickers the gatekeeper selected ──
                        pre_resolve = list(selected)
                        selected = resolve_tickers_batch(selected)
                        resolved_diff = set(pre_resolve) - set(selected)
                        if resolved_diff:
                            logger.warning(
                                "[PipelineService] US Ticker Gate resolved gatekeeper selections: %s → %s",
                                pre_resolve, selected,
                            )
                        tickers = selected
                        logger.info("[PipelineService] Gatekeeper selected: %s. Rationale: %s", tickers, rationale)
                    else:
                        logger.info("[PipelineService] Gatekeeper chose 0 tickers. Ending cycle early. Rationale: %s", rationale)
                        PipelineStateDB.append_events(cycle_id, [{
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "phase": "gatekeeper",
                            "step": "GATEKEEPER_SKIPPED",
                            "detail": f"Gatekeeper found no compelling setups. {rationale}",
                            "status": "skipped",
                            "data": {"rationale": rationale}
                        }])
                        cls._state.update({"status": "idle", "progress": "Gatekeeper bypassed."})
                        cls.save_state()
                        return
            except _ExplicitTickersPinned:
                pass  # explicit ticker list already in `tickers`
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[PipelineService] Portfolio screener failed, falling back to AAPL: %s", e)
                tickers = ["AAPL"]

            # Build snapshot map for Freshness Gate baselines
            _ticker_snapshot_map = {}
            try:
                for sr in scored_results:
                    _ticker_snapshot_map[sr["ticker"]] = {
                        "price": sr.get("price", 0),
                        "rsi": sr.get("rsi", 0),
                        "fund_count": sr.get("inst_funds", 0),
                    }
            except NameError:
                pass  # scored_results not defined (e.g. fallback path)

            # Set status to running now that gatekeeper is done
            cls._state.update({
                "status": "running",
                "tickers": tickers,
                "progress": f"Starting V3 cycle for {len(tickers)} tickers",
                "phase": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "error": None
            })
            cls.save_state()

            if cls._stop_requested:
                raise asyncio.CancelledError()

            cls._state["progress"] = f"Processing {len(tickers)} tickers concurrently"
            cls.save_state()

            async def _process_ticker(i: int, ticker_name: str):
                if cls._stop_requested:
                    logger.info("[PipelineService] V3 Cycle stopped by user request (ticker=%s).", ticker_name)
                    return None

                agent_locale = cls._state.get("agent_locale", "default")
                prism_overrides = cls._state.get("prism_overrides", {})
                result = await run_v3_pipeline(ticker=ticker_name, cycle_id=cycle_id, emit=emit, agent_locale=agent_locale, prism_overrides=prism_overrides)

                # Execute Trade — gated by the cycle's trade flag and confidence threshold
                action = result.get("action", "HOLD")
                confidence = result.get("confidence", 0)
                result["trade_attempted"] = False
                result["trade_executed"] = False

                if not trade_flag and action in ("BUY", "SELL"):
                    # Tag before the save so the row explains itself.
                    result["no_trade_reason"] = REASON_TRADE_DISABLED
                    logger.info(
                        "[PipelineService] %s: %s decision NOT executed — cycle started with trade=false",
                        ticker_name, action,
                    )

                # Save verdict to DB (re-saved after trade handling below if
                # the trade outcome mutated the result)
                from app.services.result_saver import save_analysis_result
                save_analysis_result(
                    ticker_name, cycle_id, result,
                    snapshot=_ticker_snapshot_map.get(ticker_name),
                )

                if not trade_flag:
                    return result

                trade_failed = False
                try:
                    from app.config import settings as _cfg
                    from app.trading.paper_trader import buy, sell
                    from app.services.bot_manager import get_active_bot_id
                    active_bot_id = get_active_bot_id()

                    if confidence is None:
                        logger.warning(
                            "[PipelineService] %s: confidence is None — defaulting to 0, skipping trade",
                            ticker_name,
                        )
                        confidence = 0

                    policy_action = str(result.get("policy_action") or "")
                    if action in ("BUY", "SELL") and policy_action.startswith("HOLD_POLICY_BLOCKED"):
                        # The orchestrator's policy gates (jury veto, unmitigated
                        # risk flags, missing regime, low confidence) are binding.
                        logger.warning(
                            "[PipelineService] %s: %s blocked by policy gate → %s",
                            ticker_name, action, policy_action,
                        )
                        result["no_trade_reason"] = policy_action
                    elif action in ("BUY", "SELL") and confidence < _cfg.ANALYSIS_CONFIDENCE_THRESHOLD:
                        logger.warning(
                            "[PipelineService] %s: %s blocked — confidence %d%% < threshold %d%%",
                            ticker_name, action, confidence, _cfg.ANALYSIS_CONFIDENCE_THRESHOLD,
                        )
                        result["no_trade_reason"] = REASON_CONFIDENCE_BLOCKED
                    elif action == "BUY":
                        # Situational sizing: honor the board/synthesizer's reasoned
                        # position_size_pct (percent units, capped); the confidence
                        # formula is only the fallback when no size was decided.
                        # An EXPLICIT size <= 0 is a board "watch, don't trade"
                        # directive and skips the trade entirely (deferred item 8.1).
                        agent_size_pct = (result.get("estimate") or {}).get("position_size_pct")
                        size_pct = resolve_buy_size_pct(
                            agent_size_pct, confidence, _cfg.MAX_POSITION_SIZE_PCT
                        )
                        if size_pct is None:
                            result["no_trade_reason"] = REASON_WATCH_ONLY
                            logger.info(
                                "[PipelineService] %s: BUY with explicit position_size_pct=%s — "
                                "board watch-only directive, no trade attempted",
                                ticker_name, agent_size_pct,
                            )
                        else:
                            result["trade_attempted"] = True
                            logger.info(
                                "[PipelineService] %s: sizing %s → %.1f%% of cash",
                                ticker_name,
                                "from agent decision" if isinstance(agent_size_pct, (int, float)) and agent_size_pct > 0 else "via confidence fallback",
                                size_pct * 100,
                            )
                            trade_res = await buy(bot_id=active_bot_id, ticker=ticker_name, size_pct=size_pct, cycle_id=cycle_id)
                            if isinstance(trade_res, dict) and trade_res.get("error"):
                                result["no_trade_reason"] = resolve_no_trade_reason(trade_res)
                                logger.warning("[PipelineService] %s: BUY not executed: %s", ticker_name, trade_res["error"])
                            else:
                                result["trade_executed"] = True
                    elif action == "SELL":
                        # Pre-attempt position check: a SELL on an unheld
                        # ticker is a guaranteed refusal at the paper trader
                        # (no shorting) — tag it as its own category instead
                        # of burning a trade_attempted slot on a dead call.
                        sell_held = True  # fail open: let the paper trader decide
                        try:
                            from app.tools.portfolio_tools import get_position_context
                            pos_ctx = get_position_context(ticker_name, active_bot_id)
                            sell_held = bool(pos_ctx and pos_ctx.get("held"))
                        except Exception as pos_err:
                            logger.warning(
                                "[PipelineService] %s: pre-SELL position check failed (%s) — deferring to paper trader",
                                ticker_name, pos_err,
                            )
                        if not sell_held:
                            result["no_trade_reason"] = REASON_NO_POSITION
                            logger.warning(
                                "[PipelineService] %s: SELL skipped — no open position (agents decided "
                                "EXECUTE_SELL on an unheld ticker)", ticker_name,
                            )
                        else:
                            result["trade_attempted"] = True
                            trade_res = await sell(bot_id=active_bot_id, ticker=ticker_name, cycle_id=cycle_id, qty_pct=1.0)
                            if isinstance(trade_res, dict) and trade_res.get("error"):
                                result["no_trade_reason"] = resolve_no_trade_reason(trade_res)
                                logger.warning("[PipelineService] %s: SELL not executed: %s", ticker_name, trade_res["error"])
                            else:
                                result["trade_executed"] = True

                    # Handle Triggers (limit orders). Policy-blocked decisions
                    # register NOTHING; SELL-side triggers need a real position
                    # (deferred item 8.3 — see resolve_trigger_registration).
                    decision = result.get("estimate", {})
                    stop_loss = decision.get("stop_loss")
                    take_profit = decision.get("take_profit")
                    dynamic_trigger = decision.get("dynamic_trigger")
                    if stop_loss or take_profit or dynamic_trigger:
                        position_held = False
                        if not result.get("trade_executed"):
                            try:
                                from app.tools.portfolio_tools import get_position_context
                                pos_ctx = get_position_context(ticker_name, active_bot_id)
                                position_held = bool(pos_ctx and pos_ctx.get("held"))
                            except Exception as pos_err:
                                logger.warning(
                                    "[PipelineService] %s: position check for trigger registration failed: %s",
                                    ticker_name, pos_err,
                                )
                        allowed = resolve_trigger_registration(
                            policy_action=policy_action,
                            action=action,
                            trade_executed=bool(result.get("trade_executed")),
                            position_held=position_held,
                            watch_only=result.get("no_trade_reason") == REASON_WATCH_ONLY,
                        )
                        if not any(allowed.values()):
                            logger.info(
                                "[PipelineService] %s: triggers NOT registered (policy=%s)",
                                ticker_name, policy_action,
                            )
                        from app.trading.order_triggers import create_trigger
                        if stop_loss and allowed["sell_side"]:
                            await create_trigger(bot_id=active_bot_id, ticker=ticker_name, trigger_type="stop_loss", trigger_price=float(stop_loss), action="SELL", qty_pct=1.0, created_by="pipeline")
                        if take_profit and allowed["sell_side"]:
                            await create_trigger(bot_id=active_bot_id, ticker=ticker_name, trigger_type="take_profit", trigger_price=float(take_profit), action="SELL", qty_pct=1.0, created_by="pipeline")
                        if dynamic_trigger and isinstance(dynamic_trigger, dict) and allowed["dynamic"]:
                            dt_type = dynamic_trigger.get("type")
                            dt_val = dynamic_trigger.get("value")
                            if dt_type:
                                await create_trigger(bot_id=active_bot_id, ticker=ticker_name, trigger_type="dynamic", trigger_price=0.0, action="BUY", qty_pct=1.0, dynamic_trigger_type=dt_type, dynamic_trigger_value=dt_val, created_by="pipeline", reason=f"Dynamic Buy Trigger: {dt_type}")
                except Exception as e:
                    logger.error("[PipelineService] Trade execution failed for %s: %s", ticker_name, e)
                    trade_failed = True

                if trade_failed:
                    result["trade_failed"] = True

                # Re-save when trade handling mutated the result: no_trade_reason
                # and the trade flags are set AFTER the first save, and
                # save_analysis_result is a delete+insert upsert — without this,
                # a policy/breaker-blocked BUY persists as indistinguishable from
                # an executed one. Plain HOLDs mutate nothing; skip the rewrite.
                if (
                    result.get("no_trade_reason")
                    or result.get("trade_attempted")
                    or result.get("trade_executed")
                    or trade_failed
                ):
                    save_analysis_result(
                        ticker_name, cycle_id, result,
                        snapshot=_ticker_snapshot_map.get(ticker_name),
                    )

                return result

            # Build tasks and execute concurrently
            # We use standard asyncio.gather here because the underlying LLM calls
            # (inside _run_agent_with_circuit_breaker) are globally throttled by the AdaptiveConcurrencyController.
            # return_exceptions=True ensures one crashed ticker doesn't kill the whole batch.
            tasks = [_process_ticker(i, t) for i, t in enumerate(tickers)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for t, r in zip(tickers, results):
                if isinstance(r, Exception):
                    logger.error("[PipelineService] Ticker %s failed: %s", t, r, exc_info=r)

            if cls._stop_requested:
                raise asyncio.CancelledError("Cycle stopped by user")

            from app.services.bot_manager import get_active_bot_id
            active_bot_id = get_active_bot_id()

            from app.v3.debate_coordinator import run_battle_royale
            report_written = bool(await run_battle_royale(cycle_id=cycle_id, bot_id=active_bot_id))

            # Persist the cycle summary and enqueue post-cycle autoresearch.
            # cycle_run_summaries feeds the autoresearch audit and the
            # /autoresearch/run endpoint's "latest cycle" lookup.
            cycle_summary = _persist_summary("done", tickers, results, report_generated=report_written)
            try:
                if cycle_summary:
                    import uuid as _uuid
                    from app.db.connection import get_db
                    job_id = f"job_{_uuid.uuid4().hex[:8]}"
                    with get_db() as db:
                        db.execute(
                            "INSERT INTO system_commands (id, command_type, payload, status) "
                            "VALUES (%s, 'AUTORESEARCH', %s, 'pending')",
                            [job_id, json.dumps({"cycle_id": cycle_id, "cycle_summary": cycle_summary})],
                        )
                    logger.info(
                        "[PipelineService] Cycle summary saved; autoresearch enqueued (%s)", job_id
                    )
            except Exception as ar_err:
                logger.error("[PipelineService] Post-cycle autoresearch enqueue failed: %s", ar_err)

            # Whiteboard retention — boards were never deleted before (the
            # default_cycle accumulator and superseded versions grew forever).
            try:
                from app.agents.whiteboard import whiteboard as _wb
                _wb.cleanup_old_entries()
            except Exception as wb_err:
                logger.warning("[PipelineService] Whiteboard retention failed: %s", wb_err)

            # Fire and forget the post-cycle evolution and evaluation
            try:
                from app.cognition.evolution.evaluator import run_post_cycle_evaluation
                from app.cognition.evolution.evolution_runner import run_evolution_loop
                
                def make_done_callback(name):
                    def callback(t):
                        try:
                            t.result()
                        except asyncio.CancelledError:
                            logger.info(f"[PipelineService] Background task {name} cancelled.")
                        except Exception as e:
                            logger.error(f"[PipelineService] Background task {name} failed: {e}", exc_info=True)
                    return callback

                # Run the LLM reviewer
                t1 = asyncio.create_task(run_post_cycle_evaluation(cycle_id))
                t1.add_done_callback(make_done_callback("run_post_cycle_evaluation"))
                
                # Run the quant strategy generator
                t2 = asyncio.create_task(run_evolution_loop(data_path="data/latest_market_data.csv"))
                t2.add_done_callback(make_done_callback("run_evolution_loop"))
                
                if not hasattr(cls, "_background_tasks"):
                    cls._background_tasks = set()
                cls._background_tasks.add(t1)
                cls._background_tasks.add(t2)
                t1.add_done_callback(cls._background_tasks.discard)
                t2.add_done_callback(cls._background_tasks.discard)

                logger.info("[PipelineService] Triggered post-cycle evolution tasks.")
            except Exception as ev_err:
                logger.error(f"[PipelineService] Failed to trigger evolution: {ev_err}")

            cls._state.update({
                "status": "done",
                "progress": "V3 cycle complete",
                "finished_at": datetime.now(timezone.utc).isoformat()
            })
        except asyncio.CancelledError:
            logger.info("[PipelineService] V3 Cycle CANCELLED — pipeline aborted")

            cls._state.update({
                "status": "stopped",
                "progress": "Cycle stopped by user",
                "finished_at": datetime.now(timezone.utc).isoformat()
            })
            _persist_summary("stopped", tickers, error="Cycle stopped/cancelled")
            # Do NOT re-raise — let the finally block clean up and let
            # stop_cycle() see the task as done.
        except Exception as e:
            logger.error("[PipelineService] V3 Cycle failed: %s", e)
            cls._state.update({
                "status": "error",
                "error": str(e),
                "finished_at": datetime.now(timezone.utc).isoformat()
            })
            _persist_summary("error", tickers, error=str(e))
        finally:
            cls.save_state()
            cls._cycle_task = None

    @classmethod
    def request_stop(cls):
        cls._stop_requested = True
        cls._state.update({"status": "stopping", "progress": "Stopping V3 cycle..."})
        cls.save_state()
        
        # Arm kill switch to instantly abort any running HTTP streams
        try:
            import asyncio
            from app.services.prism_agent_caller import prism_client, llm
            prism_client.arm_kill_switch()
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(llm.abort_active_requests())
            except RuntimeError:
                pass
        except Exception as e:
            logger.error("[PipelineService] Failed to arm kill switch: %s", e)
            
        if cls._cycle_task and not cls._cycle_task.done():
            cls._cycle_task.cancel()
        return {"status": "stopping"}

    @classmethod
    async def stop_cycle(cls, _stop_t1=None):
        cls.request_stop()
        if cls._cycle_task and not cls._cycle_task.done():
            try:
                await asyncio.wait_for(cls._cycle_task, timeout=5.0)
            except (Exception, asyncio.CancelledError):
                pass

        cls._state.update({
            "status": "stopped",
            "progress": "Cycle stopped by user",
            "finished_at": datetime.now(timezone.utc).isoformat()
        })
        cls.save_state()
        return {"status": "stopped"}

    @classmethod
    async def force_reset(cls):
        """Nuclear reset: cancel everything and return to idle.

        Called by FORCE_RESET command. Unlike stop_cycle() which sets
        status to 'stopped', this resets to 'idle' so a new cycle can
        start immediately without the frontend needing another action.
        """
        logger.warning("[PipelineService] FORCE_RESET — cancelling task and resetting to idle")
        cls._stop_requested = True
        if cls._cycle_task and not cls._cycle_task.done():
            cls._cycle_task.cancel()
            try:
                await asyncio.wait_for(cls._cycle_task, timeout=3.0)
            except (Exception, asyncio.CancelledError):
                pass
        # Nuclear kill: force-close all TCP connections to VLLM endpoints
        try:
            from app.services.prism_agent_caller import prism_client, llm
            prism_client.arm_kill_switch()
        except Exception as e:
            logger.error("[PipelineService] Failed to arm kill switch during force_reset: %s", e)

        # Reset all in-memory state
        cls._cycle_task = None
        cls._stop_requested = False
        cls._state = PipelineStateDB.default_state()
        cls.save_state()

        # Reset all kill switches so that future cycles are unblocked immediately
        try:
            prism_client.reset_kill_switch()
            llm.reset_kill_switch()
        except Exception as e:
            logger.error("[PipelineService] Failed to reset kill switches in force_reset: %s", e)

        return {"status": "idle"}


pipeline_service = PipelineService()
