import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.connection import get_db
from app.utils.text_utils import format_db_section

logger = logging.getLogger(__name__)

async def build_ticker_data_report(ticker: str, emit: Any = None, cycle_id: str | None = None) -> str:
    """Collect core stock datasets in parallel and format them into a markdown report."""
    ticker = ticker.upper().strip()
    
    # Helper to emit telemetry if callback exists
    def _emit(step: str, msg: str, status: str = "ok"):
        if emit:
            emit("analyzing", f"v3_{step}_{ticker}", f"📥 {ticker}: {msg}", status=status)
            
    # 1. Run Collectors in Parallel
    from app.collectors.yfinance_collector import collect_price_history, collect_fundamentals
    from app.collectors.news_collector import collect_finnhub_news
    from app.collectors.reddit_collector import collect_for_ticker as collect_reddit
    from app.collectors.youtube_collector import collect_for_ticker as collect_youtube
    
    # Per-collector outcome tracking — feeds the one-line pre-collect summary
    # log and the cycle_run_summaries collector_* counters (which read 0
    # forever because nothing ever recorded them).
    _outcomes: dict[str, str] = {}   # name -> ok|error (pending names = timed out)

    async def run_with_telemetry(name: str, coroutine: Any):
        _emit(f"precollect_{name}_start", f"Scraping {name}...", "running")
        try:
            res = await coroutine
            _outcomes[name] = "ok"
            _emit(f"precollect_{name}_ok", f"Finished {name}", "ok")
            return res
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _outcomes[name] = "error"
            logger.warning("[V3][precollect] %s/%s failed: %s: %s",
                           ticker, name, type(e).__name__, e)
            _emit(f"precollect_{name}_err", f"Failed {name}: {e}", "error")
            return None

    # 1a. Prior research: ALWAYS seed from the latest stored thesis so research
    # builds on past work instead of starting from scratch. Within 48h the
    # fast-path additionally skips the heavy scrapers; older theses still get
    # injected (age-labeled) but fresh data is collected in full.
    previous_analysis_md = ""
    is_fast_path = False

    recent = None
    _mongo_hit = False
    try:
        from app.db import mongo_store
        if mongo_store.reads_mongo("analysis_results"):
            docs = mongo_store.find_docs(
                "analysis_results", {"ticker": ticker},
                sort=[("created_at", -1)], limit=1,
                projection={"_id": 0, "thesis_summary": 1, "created_at": 1},
            )
            if docs:
                ca = docs[0].get("created_at")
                # pymongo returns naive-UTC datetimes; compare like-with-like.
                _now = datetime.utcnow() if (ca is not None and ca.tzinfo is None) \
                    else datetime.now(timezone.utc)
                is_recent = bool(ca and ca >= _now - timedelta(hours=48))
                recent = (docs[0].get("thesis_summary"), ca, is_recent)
            _mongo_hit = True
    except Exception as me:
        logger.warning("[data_report] mongo thesis read failed, PG fallback: %s", me)
        _mongo_hit = False
    if not _mongo_hit:
        with get_db() as db:
            recent = db.execute(
                """
                SELECT thesis_summary, created_at,
                       created_at >= NOW() - INTERVAL '48 hours' AS is_recent
                FROM analysis_results
                WHERE ticker = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                [ticker]
            ).fetchone()

    if recent and recent[0]:
        if recent[2]:
            is_fast_path = True
            previous_analysis_md = (
                f"## 0. PREVIOUS ANALYSIS (FAST-PATH)\n"
                f"*This stock was recently analyzed on {recent[1]}. Build your new thesis ON TOP of this past summary using today's fresh news and price action:*\n\n"
                f"{recent[0]}\n\n"
            )
            _emit("precollect_fastpath", "Fast-Path engaged! Skipping heavy scrapers.", "ok")
        else:
            # Cap the stale thesis so it can't crowd fresh data out of the
            # 10k-char report budget.
            prior_text = recent[0]
            if len(prior_text) > 2500:
                prior_text = prior_text[:2500] + "\n[... prior thesis truncated ...]"
            previous_analysis_md = (
                f"## 0. PRIOR RESEARCH ON FILE (dated {recent[1]})\n"
                f"*This stock was researched before. The thesis below may be stale — verify its claims "
                f"against today's fresh data, note what changed, and build on it rather than starting over:*\n\n"
                f"{prior_text}\n\n"
            )
            _emit("precollect_prior", "Prior research found — seeding report with last thesis.", "ok")

    _FULL_COLLECTORS = ("yfinance_price", "yfinance_fund", "finnhub_news",
                        "multi_api_news", "reddit", "youtube")

    if is_fast_path:
        # Only run the fast, dynamic scrapers
        coros = {
            "yfinance_price": collect_price_history(ticker, period="6mo"),
            "finnhub_news": collect_finnhub_news(ticker, emit_cb=_emit),
        }
    else:
        from app.collectors.news_api_rotator import collect_from_all_apis

        coros = {
            "yfinance_price": collect_price_history(ticker, period="6mo"),
            "yfinance_fund": collect_fundamentals(ticker),
            "finnhub_news": collect_finnhub_news(ticker, emit_cb=_emit),
            "multi_api_news": collect_from_all_apis([ticker]),
            "reddit": collect_reddit(ticker),
            "youtube": collect_youtube(ticker),
        }

    # Execute all collection tasks in parallel with a hard deadline. asyncio.wait
    # (not wait_for+gather) so the collectors that DID finish keep their results
    # and we can name exactly which ones ran out of clock — the old code logged
    # a single "timed out after 45s" with zero attribution, which hid that
    # reddit/youtube were blowing the budget on nearly every non-fast-path ticker.
    t_collect = time.monotonic()
    task_map = {asyncio.create_task(run_with_telemetry(n, c)): n for n, c in coros.items()}
    # 45s lost reddit/youtube/multi-api on 4 of 5 tickers in the 07-23 cycle
    # audit — agents ran on finnhub+yfinance only. Configurable, default 90s.
    from app.config import settings as _settings
    _precollect_budget = float(getattr(_settings, "PRECOLLECT_TIMEOUT_SECONDS", 90))
    done, pending = await asyncio.wait(task_map.keys(), timeout=_precollect_budget)
    timed_out = sorted(task_map[t] for t in pending)
    # Don't cancel the stragglers — the report proceeds without them, but they
    # keep collecting in the background and land in the DB for the NEXT cycle
    # (reddit/youtube/multi-api news blow the 45s budget on almost every cold
    # ticker; cancelling threw that half-finished work away every time). A
    # watchdog still hard-cancels anything running past 5 minutes.
    for t in pending:
        name = task_map[t]

        def _late_done(task: "asyncio.Task", _name=name) -> None:
            if task.cancelled():
                logger.info("[V3][precollect] %s/%s cancelled by watchdog after 5m", ticker, _name)
                return
            exc = task.exception()
            if exc:
                logger.warning("[V3][precollect] %s/%s late-failed: %s: %s",
                               ticker, _name, type(exc).__name__, exc)
            else:
                logger.info("[V3][precollect] %s/%s finished late (%.0fs) — data warm for next cycle",
                            ticker, _name, time.monotonic() - t_collect)

        t.add_done_callback(_late_done)

    if pending:
        async def _watchdog(tasks=list(pending)):
            await asyncio.sleep(300)
            for task in tasks:
                if not task.done():
                    task.cancel()

        asyncio.create_task(_watchdog())

    ok = sorted(n for n, s in _outcomes.items() if s == "ok")
    errored = sorted(n for n, s in _outcomes.items() if s == "error")
    skipped = sorted(set(_FULL_COLLECTORS) - set(coros)) if is_fast_path else []
    collect_ms = int((time.monotonic() - t_collect) * 1000)
    logger.info(
        "[V3][precollect] %s in %dms: ok=%s%s%s%s",
        ticker, collect_ms, ",".join(ok) or "-",
        f" error={','.join(errored)}" if errored else "",
        f" timeout={','.join(timed_out)}" if timed_out else "",
        f" skipped(fast-path)={','.join(skipped)}" if skipped else "",
    )
    if timed_out:
        _emit("precollect_timeout",
              f"Timed out after {_precollect_budget:.0f}s: {', '.join(timed_out)}", "warning")

    from app.v3 import collector_stats
    collector_stats.record(cycle_id, ticker, ok=ok, errored=errored,
                           timed_out=timed_out, skipped=skipped)
        
    # 2. Fetch Formatted Markdown via existing tools
    from app.tools.finance_tools import get_market_data, get_finnhub_news, get_technical_indicators
    
    market_data_md = await get_market_data(ticker)
    news_md = await get_finnhub_news(ticker)
    tech_md = await get_technical_indicators(ticker)
    
    # 3. Query Database for Reddit & YouTube Markdown directly
    reddit_md = "No recent Reddit sentiment found."
    youtube_md = "No recent YouTube transcripts found."
    
    with get_db() as db:
        # Reddit formatting
        reddit_rows = db.execute(
            """
            SELECT subreddit, title, score, upvote_ratio, comment_count, sentiment_score, summary
            FROM reddit_posts
            WHERE ticker = %s
              AND collected_at > NOW() - INTERVAL '30 days'
            ORDER BY score DESC LIMIT 10
            """,
            [ticker]
        ).fetchall()
        if reddit_rows:
            reddit_md = format_db_section(
                "Top Reddit Posts", 
                reddit_rows, 
                ["Subreddit", "Title", "Score", "UpvoteRatio", "Comments", "Sentiment", "Summary"]
            )
            
        # YouTube formatting
        # Recency-windowed: without the filter, three-week-old transcripts (the
        # collector was starved by the 45s pre-collect cancel from 06-28 to
        # 07-17) were presented to agents as "Recent YouTube Analyses".
        yt_rows = db.execute(
            """
            SELECT channel, title, published_at, summary
            FROM youtube_transcripts
            WHERE ticker = %s
              AND published_at > NOW() - INTERVAL '21 days'
            ORDER BY published_at DESC LIMIT 5
            """,
            [ticker]
        ).fetchall()
        if yt_rows:
            youtube_md = format_db_section(
                "Recent YouTube Analyses",
                yt_rows,
                ["Channel", "Title", "Published", "Summary"]
            )

        # Institutional fund holdings (DB-only, no API call)
        institutional_md = "No institutional fund holdings data available."
        try:
            from app.collectors.fund_scanner import get_institutional_signal, get_fund_momentum
            inst_signal = get_institutional_signal(ticker)
            if inst_signal["fund_count"] > 0:
                inst_lines = []
                inst_lines.append(f"**{inst_signal['fund_count']}** tracked hedge fund(s) hold this stock.")
                inst_lines.append(f"Total institutional value: ${inst_signal['total_institutional_value']:,.0f}")
                inst_lines.append(f"Momentum: **{inst_signal['momentum']}**")
                if inst_signal["has_top_performer"]:
                    inst_lines.append(f"⭐ Top-performing fund(s): {', '.join(inst_signal['top_performer_names'])}")
                if inst_signal["has_new_position"]:
                    inst_lines.append("🆕 New position opened this quarter by at least one fund.")
                # Top 5 holders
                for h in inst_signal["holders"][:5]:
                    val_fmt = f"${h['value_usd']:,.0f}" if h['value_usd'] else '$0'
                    new_flag = ' 🆕' if h['is_new'] else ''
                    inst_lines.append(f"  - {h['fund']}: {h['shares']:,} shares ({val_fmt}){new_flag}")
                # Quarterly momentum
                momentum = get_fund_momentum(ticker)
                if momentum["direction"] != "NO_HISTORY":
                    inst_lines.append(f"Q/Q trend: {momentum['direction']} ({momentum['latest_quarter']} vs {momentum['previous_quarter']})")
                    if momentum["new_buyers"]:
                        inst_lines.append(f"  New buyers: {', '.join(momentum['new_buyers'][:3])}")
                    if momentum["exiters"]:
                        inst_lines.append(f"  Exited: {', '.join(momentum['exiters'][:3])}")
                institutional_md = "\n".join(inst_lines)
        except Exception as e:
            logger.warning("[V3] %s: Failed to build institutional section (non-fatal): %s", ticker, e)

    # 4. Construct Final Document — with size cap to prevent context overflow
    #
    # Priority order (highest to lowest): market data > technicals > news > reddit > youtube
    # If the full report exceeds _MAX_DATA_REPORT_CHARS, drop lower-priority sections first.
    _MAX_DATA_REPORT_CHARS = 10000

    # Watch Desk wake context: if this cycle was triggered by a watch tripping,
    # tell the agent exactly WHAT woke it so it focuses on the change, not a
    # from-scratch review.
    wake_context_md = ""
    try:
        from app.services.watch_desk import consume_wake_context
        _trip = consume_wake_context(ticker)
        if _trip:
            wake_context_md = (
                f"## 0. WHY YOU WOKE UP (WATCH DESK TRIGGER)\n"
                f"*A background watch condition tripped — this is the specific change to focus on. "
                f"Assess what it means for the prior thesis, then decide:*\n\n"
                f"**{_trip}**\n\n"
            )
    except Exception:
        pass

    # Realized-outcome + lesson feedback. The V3 agents saw the prior THESIS
    # (above) but never the prior P&L: get_ticker_outcome_context only fired
    # for legacy V2 agent names, and autoresearch lessons were stored +
    # embedded but had zero retrieval callers. Inject both here — the one
    # report every V3 agent reads.
    outcome_md = ""
    try:
        from app.agents.base_agent import get_ticker_outcome_context
        outcome_md = get_ticker_outcome_context(ticker) or ""
    except Exception:
        pass
    # Fleet-wide confidence calibration: stated conviction vs realized win
    # rate. Per-ticker history alone can't show an agent that its 80% claims
    # win 67% while its 90% claims win 59%.
    calibration_md = ""
    try:
        from app.agents.base_agent import get_confidence_calibration_context
        _cal = get_confidence_calibration_context()
        if _cal:
            calibration_md = _cal + "\n\n"
    except Exception:
        pass
    lessons_md = ""
    try:
        with get_db() as db:
            # NB: evolution_lessons.timestamp is TEXT (ISO strings) — a
            # NOW()-interval comparison type-errors. ISO strings sort
            # lexicographically, so ORDER BY alone gets the freshest.
            lrows = db.execute(
                "SELECT lesson_text FROM evolution_lessons "
                "WHERE status = 'audited' AND lesson_text IS NOT NULL "
                "AND length(trim(lesson_text)) > 20 "
                "ORDER BY timestamp DESC NULLS LAST LIMIT 12"
            ).fetchall()
        # The audit writes near-identical rephrasings of the same lesson on
        # consecutive cycles ("downstream engines must wait for
        # pre-collection" x3) — greedy Jaccard filter keeps 3 DISTINCT ones.
        picked: list[str] = []
        for r in lrows:
            text = str(r[0]).strip()
            words = set(text.lower().split())
            if any(
                len(words & set(p.lower().split())) / max(1, len(words | set(p.lower().split()))) > 0.6
                for p in picked
            ):
                continue
            picked.append(text)
            if len(picked) >= 3:
                break
        if picked:
            lessons_md = (
                "## 0.b LESSONS FROM RECENT CYCLES (autoresearch audit)\n"
                + "\n".join(f"- {p[:300]}" for p in picked)
                + "\n\n"
            )
    except Exception:
        pass

    header = (
        f"# Pre-Collected Ticker Data Report: {ticker}\n"
        f"Generated at: {datetime.now(timezone.utc).isoformat()}\n\n"
        f"{wake_context_md}"
        f"{previous_analysis_md}"
        f"{outcome_md}"
        f"{calibration_md}"
        f"{lessons_md}"
    )

    # Build sections in priority order (highest priority first)
    core_sections = (
        f"## 1. Market Data & Fundamentals\n"
        f"{market_data_md}\n\n"
        f"## 2. Technical Indicators\n"
        f"{tech_md}\n\n"
        f"## 3. Recent News & Sentiment\n"
        f"{news_md}\n\n"
    )

    social_sections = [
        (f"## 4. Institutional Fund Holdings\n{institutional_md}\n\n", "Institutional"),
        (f"## 5. Reddit Social Sentiment\n{reddit_md}\n\n", "Reddit"),
        (f"## 6. YouTube Mentions & Transcripts\n{youtube_md}\n", "YouTube"),
    ]

    report = header + core_sections
    budget_remaining = _MAX_DATA_REPORT_CHARS - len(report)

    if budget_remaining > 0:
        for section_text, section_name in social_sections:
            if len(section_text) <= budget_remaining:
                report += section_text
                budget_remaining -= len(section_text)
            else:
                # Partial fit — truncate this section
                truncated = section_text[: budget_remaining - 80]
                report += truncated + f"\n[... {section_name} TRUNCATED — data available via tools ...]\n"
                budget_remaining = 0
                break

    # Hard safety net — if core sections alone exceed the cap
    if len(report) > _MAX_DATA_REPORT_CHARS:
        logger.warning(
            "[V3] Data report for %s exceeded %d chars (%d) — hard-truncating",
            ticker, _MAX_DATA_REPORT_CHARS, len(report),
        )
        report = (
            report[: _MAX_DATA_REPORT_CHARS - 100]
            + "\n\n[... DATA REPORT TRUNCATED — full data available via tools ...]\n"
        )

    return report
