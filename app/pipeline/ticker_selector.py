"""
Ticker Selector - Smart algorithm for choosing the best N tickers to process
by blending Portfolio, Large Cap, Mid/Small Cap, and Random Discovery slots.

HARD TOTAL CAP: When max_tickers=N, exactly N total tickers are processed.
Positions get priority (filled first) but COUNT AGAINST the cap. This ensures
max_tickers=1 means "process 1 ticker total", not "1 plus however many
positions you have". Remaining slots after positions go to watchlist + discovery.
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List
from app.db.connection import get_db

logger = logging.getLogger(__name__)

def _has_material_change(ticker: str, db, cap: int = 50) -> bool:
    """Check if a ticker has a material change since its last analysis.

    Material change = price moved >5% OR >=3 new articles published.
    Used to override the 24h re-analysis cooldown.

    Returns True if re-analysis is warranted despite recent analysis.
    """
    try:
        from app.config import settings
        price_threshold = getattr(settings, "PRICE_CHANGE_THRESHOLD_PCT", 5.0)
        news_threshold = getattr(settings, "NEW_ARTICLES_THRESHOLD", 3)
    except ImportError:
        price_threshold = 5.0
        news_threshold = 3

    if cap is not None and cap < 10:
        price_threshold *= 2.0  # Make it stricter under small caps (e.g. 10.0%)
        news_threshold += 2     # Make it stricter under small caps (e.g. 5 articles)

    try:
        # Get last analysis price and timestamp
        row = db.execute(
            """
            SELECT price_at_analysis, created_at
            FROM analysis_results
            WHERE ticker = %s AND price_at_analysis IS NOT NULL
            ORDER BY created_at DESC LIMIT 1
            """,
            [ticker],
        ).fetchone()
        if not row:
            return False  # No prior analysis with price data — no comparison possible

        last_price = float(row[0])
        last_analyzed_at = row[1]

        # 1. Check price delta
        if last_price and last_price > 0:
            current_price_row = db.execute(
                "SELECT close FROM price_history WHERE ticker = %s ORDER BY date DESC LIMIT 1",
                [ticker],
            ).fetchone()
            if current_price_row and current_price_row[0]:
                current_price = float(current_price_row[0])
                delta_pct = abs(current_price - last_price) / last_price * 100
                if delta_pct >= price_threshold:
                    logger.info(
                        "[SELECTOR] %s: MATERIAL CHANGE — price moved %.1f%% (%.2f → %.2f)",
                        ticker, delta_pct, last_price, current_price,
                    )
                    return True

        # 2. Check new article count since last analysis
        if last_analyzed_at:
            news_row = db.execute(
                "SELECT COUNT(*) FROM news_articles WHERE ticker = %s AND published_at > %s",
                [ticker, last_analyzed_at],
            ).fetchone()
            new_articles = news_row[0] if news_row else 0
            if new_articles >= news_threshold:
                logger.info(
                    "[SELECTOR] %s: MATERIAL CHANGE — %d new articles since last analysis",
                    ticker, new_articles,
                )
                return True

    except Exception as e:
        logger.warning("[SELECTOR] Material change check failed for %s: %s", ticker, e)

    return False


@dataclass
class TickerSelectionResult:
    """Structured output from ticker selection so callers know the breakdown."""

    position_tickers: List[str] = field(default_factory=list)
    non_position_tickers: List[str] = field(default_factory=list)

    @property
    def all_tickers(self) -> List[str]:
        """Combined deduped list: positions first, then non-position names."""
        seen = set()
        out = []
        for t in self.position_tickers:
            if t not in seen:
                seen.add(t)
                out.append(t)
        for t in self.non_position_tickers:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out


class TickerSelector:
    @staticmethod
    def select_tickers_for_cycle(requested_tickers: List[str], cap: int) -> List[str]:
        """Convenience wrapper — returns a flat list for backward compatibility."""
        result = TickerSelector.select_tickers_for_cycle_v2(requested_tickers, cap)
        return result.all_tickers

    @staticmethod
    def select_tickers_for_cycle_v2(
        requested_tickers: List[str],
        cap: int,
        discovered_tickers: int | None = None,
    ) -> TickerSelectionResult:
        """
        Build the cycle ticker list with a hard total cap.

        `cap` is the HARD CEILING on total tickers processed. Positions get
        priority (filled first), then remaining slots go to non-position
        tickers (watchlist + discovery). This ensures that when the user
        sets max_tickers=1, exactly 1 ticker is processed — not 1 + N positions.

        Returns a TickerSelectionResult with separate position / non-position lists.
        """
        if cap is None or cap < 0:
            cap = 50

        requested = set(t.upper().strip() for t in requested_tickers if t.strip())

        # ── 1. Fetch open positions (ALWAYS included, outside cap) ──
        position_tickers: set[str] = set()
        with get_db() as db:
            try:
                # Resolve active bot for position filtering
                try:
                    from app.services.bot_manager import get_active_bot_id

                    bid = get_active_bot_id()
                except Exception:
                    from app.config import settings as _cfg

                    bid = _cfg.BOT_ID

                # Check if positions table exists
                tbl_check = db.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'positions'"
                ).fetchone()
                if tbl_check:
                    pos_rows = db.execute(
                        "SELECT ticker FROM position_lots WHERE status = 'open' AND bot_id = %s",
                        [bid],
                    ).fetchall()
                    for r in pos_rows:
                        position_tickers.add(r[0])
                    logger.info(
                        "[SELECTOR] Fetched %d position tickers for bot '%s'",
                        len(position_tickers),
                        bid,
                    )
            except Exception as e:
                logger.warning("[SELECTOR] Failed to fetch open positions: %s", e)

        # ── 1.2. Fetch banned tickers from database ──
        banned_tickers: set[str] = set()
        with get_db() as db:
            try:
                banned_rows = db.execute("SELECT ticker FROM ticker_bans").fetchall()
                banned_tickers = {r[0].upper().strip() for r in banned_rows}
            except Exception as e:
                logger.warning("[SELECTOR] Failed to fetch banned tickers: %s", e)

        # ── 1.5. Fetch 24-Hour Cooldown & Last Completed Cycle Tickers ──
        recent_analyzed: set[str] = set()
        material_change_overrides: set[str] = set()
        with get_db() as db:
            try:
                # 24-hour cooldown
                recent_rows = db.execute(
                    "SELECT DISTINCT ticker FROM decision_outcomes WHERE created_at > NOW() - INTERVAL '24 hours'"
                ).fetchall()
                analysis_rows = db.execute(
                    "SELECT DISTINCT ticker FROM analysis_results WHERE created_at > NOW() - INTERVAL '24 hours'"
                ).fetchall()
                for r in recent_rows:
                    recent_analyzed.add(r[0].upper().strip())
                for r in analysis_rows:
                    recent_analyzed.add(r[0].upper().strip())

                # Last completed cycles cooldown (last 5 if cap < 10, else last 1)
                n_cooldown_cycles = 5 if (cap is not None and cap < 10) else 1
                last_cycles = db.execute(
                    "SELECT cycle_id FROM cycle_benchmarks WHERE status = 'done' ORDER BY finished_at DESC, started_at DESC LIMIT %s",
                    [n_cooldown_cycles]
                ).fetchall()
                
                if last_cycles:
                    cycle_ids = [r[0] for r in last_cycles]
                    logger.info("[SELECTOR] Found last %d completed cycles for cooldown: %s", len(cycle_ids), cycle_ids)
                    ticker_rows = db.execute(
                        """
                        SELECT DISTINCT ticker FROM cycle_ticker_benchmarks WHERE cycle_id = ANY(%s)
                        UNION
                        SELECT DISTINCT ticker FROM analysis_results WHERE cycle_id = ANY(%s)
                        """,
                        [cycle_ids, cycle_ids]
                    ).fetchall()
                    for r in ticker_rows:
                        recent_analyzed.add(r[0].upper().strip())
                    logger.info(
                        "[SELECTOR] Added %d tickers from last completed cycles to cooldown",
                        len(ticker_rows)
                    )

                # ── Material Change Override ──
                # Check each recently-analyzed ticker for material changes
                # (price >5% move or 3+ new articles). If changed, override cooldown.
                for cooldown_ticker in list(recent_analyzed):
                    if _has_material_change(cooldown_ticker, db, cap):
                        material_change_overrides.add(cooldown_ticker)
                        recent_analyzed.discard(cooldown_ticker)

                if material_change_overrides:
                    logger.info(
                        "[SELECTOR] Material change overrides (re-analyzing despite 24h cooldown): %s",
                        ", ".join(sorted(material_change_overrides)),
                    )

            except Exception as e:
                logger.warning("[SELECTOR] Failed to fetch cooldown tickers: %s", e)

        # ── 1b. Enforce hard cap on positions themselves ──
        # Positions get priority but still count against the total cap.
        if len(position_tickers) > cap:
            logger.warning(
                "[SELECTOR] %d positions exceed hard cap %d — truncating positions!",
                len(position_tickers), cap,
            )
            position_tickers = set(list(position_tickers)[:cap])

        # Remaining slots for non-position tickers
        non_position_slots = cap - len(position_tickers)

        # ── 2. Build non-position set (requested + watchlist), capped ──
        non_position: set[str] = set()

        # Import blocklist for filtering all entry points
        from app.processors.ticker_extractor import FALSE_TICKERS as _BLOCKED

        if non_position_slots <= 0:
            logger.info(
                "[SELECTOR] All %d cap slots filled by positions — no room for non-position tickers",
                cap,
            )
        else:
            # Add manually requested tickers (minus any that are already positions)
            for t in requested:
                if t in _BLOCKED:
                    logger.info("[SELECTOR] Blocking requested ticker %s — in FALSE_TICKERS", t)
                    continue
                if t in banned_tickers:
                    logger.info("[SELECTOR] Blocking requested ticker %s — in ticker_bans", t)
                    continue
                if t not in position_tickers and len(non_position) < non_position_slots:
                    non_position.add(t)

            # Add active watchlist (minus positions), up to remaining slots
            # Triage handles the "is there new data?" question — selector just gathers candidates.
            with get_db() as db:
                try:
                    wl_rows = db.execute(
                        "SELECT ticker FROM watchlist WHERE status = 'active'"
                    ).fetchall()
                    for r in wl_rows:
                        t_val = r[0]
                        if t_val in _BLOCKED:
                            logger.info("[SELECTOR] Blocking watchlist ticker %s — in FALSE_TICKERS", t_val)
                            continue
                        if t_val in banned_tickers:
                            logger.info("[SELECTOR] Blocking watchlist ticker %s — in ticker_bans", t_val)
                            continue
                        if t_val not in position_tickers and len(non_position) < non_position_slots:
                            if t_val not in recent_analyzed:
                                non_position.add(t_val)
                            else:
                                logger.info("[SELECTOR] Skipping watchlist ticker %s due to 24-hour cooldown", t_val)
                except Exception as e:
                    logger.warning("[SELECTOR] Failed to fetch watchlist: %s", e)

        # ── 3. Discovery fill (only if non-position set is under its slot allocation) ──
        if len(non_position) < non_position_slots:
            remaining_slots = non_position_slots - len(non_position)
            if discovered_tickers is not None:
                remaining_slots = min(remaining_slots, max(0, discovered_tickers))

            large_slots = max(1, int(remaining_slots * 0.40))
            mid_slots = max(1, int(remaining_slots * 0.40))
            random_slots = remaining_slots - large_slots - mid_slots
            if random_slots < 0:
                large_slots = remaining_slots
                mid_slots = 0
                random_slots = 0

            # Exclude positions, already-selected non-position tickers, and recently analyzed
            exclude = position_tickers | non_position | recent_analyzed
            if not exclude:
                placeholders = "'___'"
                params: list = []
            else:
                placeholders = ",".join(["%s"] * len(exclude))
                params = list(exclude)

            base_query = """
                SELECT d.ticker, d.score, m.market_cap_tier, m.sp500,
                       COALESCE(MAX(a.created_at), '2000-01-01') as last_analyzed,
                       d.source, d.discovered_at
                FROM discovered_tickers d
                LEFT JOIN ticker_metadata m ON d.ticker = m.ticker
                LEFT JOIN analysis_results a ON d.ticker = a.ticker
                WHERE d.ticker NOT IN ({placeholders})
                  AND (d.validation_status IS NULL OR d.validation_status != 'quarantine')
                GROUP BY d.ticker, d.score, m.market_cap_tier, m.sp500, d.source, d.discovered_at
                ORDER BY
                     /* Freshly discovered (last 24h) tickers get top priority */
                     CASE
                        WHEN d.discovered_at > CURRENT_TIMESTAMP - INTERVAL '24 hours' THEN 2
                        WHEN d.discovered_at > CURRENT_TIMESTAMP - INTERVAL '72 hours' THEN 1
                        ELSE 0
                     END DESC,
                     /* High-value sources rank higher */
                     CASE
                        WHEN d.source LIKE 'news_discovery%%' THEN 3
                        WHEN d.source = 'macro_scout' THEN 2
                        WHEN d.source LIKE 'congress%%' THEN 1
                        ELSE 0
                     END DESC,
                     /* Skip recently-analyzed tickers */
                     CASE
                        WHEN COALESCE(MAX(a.created_at), '2000-01-01') > CURRENT_TIMESTAMP - INTERVAL '24 hours' THEN 0
                        ELSE 1
                     END DESC,
                     d.score DESC
                LIMIT 300
            """
            query = base_query.format(placeholders=placeholders)

            try:
                with get_db() as db:
                    candidates = db.execute(query, params).fetchall()
            except Exception as e:
                logger.warning("[SELECTOR] Failed to query DB for candidates: %s", e)
                candidates = []

            # Fast Ticker Validation (Discovery Only) ──
            # Strip out numbers, dashes, and known macro acronyms that cause YFinance to fail
            import re
            
            from app.processors.ticker_extractor import FALSE_TICKERS

            def is_valid_ticker_format(t: str) -> bool:
                if not t or len(t) > 5:
                    return False
                if bool(re.search(r"[0-9\.]", t)):
                    return False
                if t in FALSE_TICKERS:
                    return False
                if t in banned_tickers:
                    return False
                return True

            large_candidates: list[str] = []
            mid_small_candidates: list[str] = []
            mystery_candidates: list[str] = []

            for row in candidates:
                t_ticker = row[0]
                if not is_valid_ticker_format(t_ticker):
                    continue

                tier = row[2]
                sp500 = row[3]
                if sp500 or tier in ("mega", "large"):
                    large_candidates.append(t_ticker)
                elif tier in ("mid", "small", "micro"):
                    mid_small_candidates.append(t_ticker)
                else:
                    mystery_candidates.append(t_ticker)

            def fill_bucket(bucket_list, required_slots):
                picks = []
                while len(picks) < required_slots and bucket_list:
                    picks.append(bucket_list.pop(0))
                return picks

            discovery: list[str] = []
            # If slot limits are small, shuffle candidates to ensure discovery diversity
            if non_position_slots is not None and non_position_slots <= 5:
                random.shuffle(large_candidates)
                random.shuffle(mid_small_candidates)

            large_picks = fill_bucket(large_candidates, large_slots)
            discovery.extend(large_picks)
            mid_picks = fill_bucket(mid_small_candidates, mid_slots)
            discovery.extend(mid_picks)

            leftovers = large_candidates + mid_small_candidates + mystery_candidates
            shortfall = remaining_slots - len(discovery)
            random_picks: list[str] = []
            if shortfall > 0 and leftovers:
                random.shuffle(leftovers)
                random_picks = fill_bucket(leftovers, shortfall)
                discovery.extend(random_picks)

            for t in discovery:
                non_position.add(t)
        else:
            large_picks = []
            mid_picks = []
            random_picks = []

        # ── 4. Apply hard cap — non-position tickers fill remaining slots only ──
        capped_non_position = list(non_position)[:non_position_slots]

        total = len(position_tickers) + len(capped_non_position)
        logger.info(
            "[TICKER SELECTOR] HARD CAP: %d total. "
            "Positions: %d, Non-position: %d/%d slots. "
            "Large/SP500: %d, Mid/Small: %d, Random: %d. "
            "Final total: %d",
            cap,
            len(position_tickers),
            len(capped_non_position),
            non_position_slots,
            len(large_picks),
            len(mid_picks),
            len(random_picks),
            total,
        )
        assert total <= cap, (
            f"[SELECTOR BUG] Total tickers {total} exceeds hard cap {cap}! "
            f"positions={len(position_tickers)}, non_position={len(capped_non_position)}"
        )

        # Check consecutive cycle overlap for logging
        if cap is not None and cap < 10:
            with get_db() as db:
                try:
                    last_cycle = db.execute(
                        "SELECT cycle_id FROM cycle_benchmarks WHERE status = 'done' ORDER BY finished_at DESC, started_at DESC LIMIT 1"
                    ).fetchone()
                    if last_cycle:
                        last_tickers_rows = db.execute(
                            """
                            SELECT DISTINCT ticker FROM cycle_ticker_benchmarks WHERE cycle_id = %s
                            UNION
                            SELECT DISTINCT ticker FROM analysis_results WHERE cycle_id = %s
                            """,
                            [last_cycle[0], last_cycle[0]]
                        ).fetchall()
                        last_tickers = {r[0].upper().strip() for r in last_tickers_rows}
                        overlap = set(position_tickers | set(capped_non_position)) & last_tickers
                        if overlap:
                            logger.info(
                                "[SELECTOR] CONSECUTIVE OVERLAP DETECTED under small cap (%d): %s are appearing in consecutive cycles",
                                cap, ", ".join(sorted(overlap))
                            )
                except Exception as overlap_err:
                    logger.debug("Overlap check failed (non-fatal): %s", overlap_err)

        logger.info(
            "[SELECTOR-DETAILED-LOG] cap=%s, discovered_tickers=%s, position_tickers=%s, non_position_tickers=%s, total=%d, material_change_overrides=%s",
            cap,
            discovered_tickers,
            list(position_tickers),
            capped_non_position,
            total,
            list(material_change_overrides),
        )

        return TickerSelectionResult(
            position_tickers=list(position_tickers),
            non_position_tickers=capped_non_position,
        )
