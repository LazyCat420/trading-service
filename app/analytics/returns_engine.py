"""
Real return/alpha computation for smart-money disclosures.

Replaces two fabricated metrics:
  - congress "estimated return" = 8.0 + (buys - sells) * 0.5   (routers/data.py)
  - 13F fund return            = 0.15 baseline + hardcoded alpha (performance_engine.py)

Neither touched a price. This module runs an event study against real OHLCV.

METHOD
  Entry is the first close on or after the date the trade became PUBLIC, not the
  date it happened. For congress that is disclosure_date (up to 45 days after the
  trade); for a 13F it is the filing date (~45 days after quarter end). Scoring
  from the trade date would credit actors with returns nobody could have acted
  on, which is the single most common way these trackers overstate performance.

  Forward returns at 1m/3m/6m/1y are benchmarked against SPY over the identical
  window. We report ALPHA (excess vs SPY) as the headline number — in a bull
  market raw return makes every actor look brilliant.

  A SELL is scored inverted: selling before a drop is skill, so
  alpha_sell = -(stock_excess). A sell that dodged a 20% underperformance scores
  +20, not -20.

RESULTS ARE MATERIALIZED into two tables so the dashboard and the agent tools
read the SAME numbers — a UI that disagrees with what an agent was told is worse
than no UI at all.
"""

import logging
from datetime import datetime, timezone

from app.db.connection import get_db
from app.analytics.amount_parser import parse_amount_range

logger = logging.getLogger(__name__)

BENCHMARK_TICKER = "SPY"

# Forward windows, in calendar days. Approximate month lengths are fine — we take
# the first available close at or after the horizon, so weekends/holidays absorb.
WINDOWS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365}

# Below this many scored trades an actor's average is noise, not signal. We still
# store the row (so the UI can show "insufficient data") but flag it so nothing
# ranks an actor to the top of a leaderboard off three lucky trades.
MIN_SCORED_FOR_RANKING = 5


def _ensure_tables():
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS smart_money_trade_scores (
                trade_key       TEXT PRIMARY KEY,
                actor_type      TEXT NOT NULL,
                actor_id        TEXT NOT NULL,
                actor_name      TEXT,
                ticker          TEXT NOT NULL,
                direction       TEXT NOT NULL,
                event_date      DATE NOT NULL,
                size_est_usd    DOUBLE PRECISION,
                size_confidence TEXT,
                entry_price     DOUBLE PRECISION,
                ret_1m DOUBLE PRECISION, ret_3m DOUBLE PRECISION,
                ret_6m DOUBLE PRECISION, ret_1y DOUBLE PRECISION,
                alpha_1m DOUBLE PRECISION, alpha_3m DOUBLE PRECISION,
                alpha_6m DOUBLE PRECISION, alpha_1y DOUBLE PRECISION,
                computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS smart_money_performance (
                actor_type      TEXT NOT NULL,
                actor_id        TEXT NOT NULL,
                actor_name      TEXT,
                horizon         TEXT NOT NULL,
                trade_count     INTEGER,
                scored_count    INTEGER,
                coverage_pct    DOUBLE PRECISION,
                avg_return      DOUBLE PRECISION,
                avg_alpha       DOUBLE PRECISION,
                median_alpha    DOUBLE PRECISION,
                win_rate        DOUBLE PRECISION,
                total_size_est  DOUBLE PRECISION,
                rankable        BOOLEAN DEFAULT FALSE,
                computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (actor_type, actor_id, horizon)
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sm_scores_actor ON smart_money_trade_scores (actor_type, actor_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sm_scores_ticker ON smart_money_trade_scores (ticker, event_date)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sm_perf_rank ON smart_money_performance (horizon, rankable, avg_alpha)"
        )


def _price_lookup_sql(alias: str, ticker_expr: str, offset_days: int | None) -> str:
    """First available close at/after the horizon, as a LATERAL subquery.

    Doing this in SQL rather than per-trade Python matters: 30k congress trades x
    10 price lookups is 300k round trips in a loop, versus one planned query.
    """
    if offset_days is None:
        date_expr = "t.event_date"
    else:
        date_expr = f"t.event_date + INTERVAL '{offset_days} days'"
    return f"""
        LEFT JOIN LATERAL (
            SELECT p.close
            FROM price_history p
            WHERE p.ticker = {ticker_expr}
              AND p.date >= {date_expr}
            ORDER BY p.date
            LIMIT 1
        ) {alias} ON TRUE
    """


def _build_scoring_query(source_cte: str) -> str:
    """Wrap a trade-source CTE with entry/forward/benchmark price joins.

    The CTE must yield: trade_key, actor_id, actor_name, ticker, direction,
    event_date, size_est_usd, size_confidence, size_raw.
    """
    joins = [_price_lookup_sql("entry", "t.ticker", None)]
    selects = ["entry.close AS entry_price"]

    for label, days in WINDOWS.items():
        joins.append(_price_lookup_sql(f"fwd_{label}", "t.ticker", days))
        selects.append(f"fwd_{label}.close AS px_{label}")

    joins.append(_price_lookup_sql("bench_entry", f"'{BENCHMARK_TICKER}'", None))
    selects.append("bench_entry.close AS bench_entry_price")

    for label, days in WINDOWS.items():
        joins.append(_price_lookup_sql(f"bench_{label}", f"'{BENCHMARK_TICKER}'", days))
        selects.append(f"bench_{label}.close AS bench_px_{label}")

    return f"""
        WITH t AS ({source_cte})
        SELECT
            t.trade_key, t.actor_id, t.actor_name, t.ticker, t.direction,
            t.event_date, t.size_est_usd, t.size_confidence, t.size_raw,
            {', '.join(selects)}
        FROM t
        {' '.join(joins)}
    """


CONGRESS_SOURCE = """
    SELECT
        ct.id                       AS trade_key,
        COALESCE(ct.bioguide_id, ct.politician) AS actor_id,
        ct.politician               AS actor_name,
        ct.ticker                   AS ticker,
        LOWER(ct.transaction_type)  AS direction,
        ct.disclosure_date          AS event_date,
        NULL::DOUBLE PRECISION      AS size_est_usd,
        NULL::TEXT                  AS size_confidence,
        ct.amount_range             AS size_raw
    FROM congress_trades ct
    WHERE ct.ticker ~ '^[A-Z]{1,5}$'
      AND ct.disclosure_date IS NOT NULL
      AND LOWER(ct.transaction_type) IN ('buy', 'sell')
"""

# 13F gives us a position snapshot per quarter, not transactions. We derive
# directional events by diffing consecutive quarters PER FILER — comparing
# against the latest quarter globally would read every fund that simply hasn't
# filed yet as a total liquidation.
FUND_SOURCE = """
    WITH filer_start AS (
        -- The first quarter we ever observed a filer in is an inventory
        -- snapshot, not a set of decisions. Without this guard every position
        -- in that quarter diffs against NULL and reads as a fresh buy — which
        -- put 594 phantom buys and zero sells into the first flow bucket.
        SELECT cik, MIN(filing_quarter) AS first_quarter
        FROM sec_13f_holdings
        WHERE cik NOT LIKE 'yf_%'
        GROUP BY cik
    ),
    ranked AS (
        SELECT
            h.cik, h.ticker, h.filing_quarter, h.shares, h.value_usd,
            fs.first_quarter,
            LAG(h.shares) OVER (
                PARTITION BY h.cik, h.ticker ORDER BY h.filing_quarter
            ) AS prev_shares
        FROM sec_13f_holdings h
        JOIN filer_start fs ON fs.cik = h.cik
        WHERE h.ticker ~ '^[A-Z]{1,5}$'
          AND h.cik NOT LIKE 'yf_%'
    )
    SELECT
        r.cik || ':' || r.ticker || ':' || r.filing_quarter AS trade_key,
        r.cik        AS actor_id,
        f.filer_name AS actor_name,
        r.ticker     AS ticker,
        CASE
            WHEN r.filing_quarter = r.first_quarter THEN 'initial'
            WHEN r.prev_shares IS NULL              THEN 'buy'
            WHEN r.shares > r.prev_shares           THEN 'buy'
            WHEN r.shares < r.prev_shares           THEN 'sell'
            ELSE 'hold'
        END AS direction,
        -- 13Fs are due 45 days after quarter end; that filing date is the first
        -- moment the position was public and therefore actionable.
        (
            (SUBSTRING(r.filing_quarter, 1, 4) || '-' ||
             LPAD((CAST(SUBSTRING(r.filing_quarter, 6, 1) AS INTEGER) * 3)::TEXT, 2, '0') ||
             '-01')::DATE + INTERVAL '1 month' + INTERVAL '45 days'
        )::DATE AS event_date,
        r.value_usd  AS size_est_usd,
        'reported'   AS size_confidence,
        NULL::TEXT   AS size_raw
    FROM ranked r
    JOIN sec_13f_filers f ON f.cik = r.cik
"""


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / old * 100.0


def _score_rows(rows, actor_type: str) -> list[tuple]:
    scored = []
    for r in rows:
        (
            trade_key, actor_id, actor_name, ticker, direction, event_date,
            size_est, size_conf, size_raw, entry_price,
            *prices,
        ) = r

        n = len(WINDOWS)
        fwd = prices[0:n]
        bench_entry = prices[n]
        bench_fwd = prices[n + 1: n + 1 + n]

        if entry_price is None or entry_price == 0:
            continue

        # 'hold' rows exist only so the diff is complete, and 'initial' is a
        # first-sighting snapshot. Neither is a decision, so neither is scored.
        if direction in ("hold", "initial"):
            continue

        # Congress discloses a bracket string; 13F reports an exact dollar value.
        if size_raw is not None:
            size_est, size_conf = parse_amount_range(size_raw)

        rets, alphas = [], []
        for i, _label in enumerate(WINDOWS):
            stock_ret = _pct(fwd[i], entry_price)
            bench_ret = _pct(bench_fwd[i], bench_entry)

            if stock_ret is None:
                rets.append(None)
                alphas.append(None)
                continue

            rets.append(stock_ret)

            if bench_ret is None:
                # No benchmark for this window — we can report the raw return but
                # NOT alpha. Emitting stock_ret as alpha here would silently
                # inflate every score during any SPY gap.
                alphas.append(None)
                continue

            excess = stock_ret - bench_ret
            # Selling ahead of underperformance is a good decision, so invert.
            alphas.append(-excess if direction == "sell" else excess)

        scored.append(
            (
                trade_key, actor_type, str(actor_id), actor_name, ticker, direction,
                event_date, size_est, size_conf, entry_price,
                *rets, *alphas,
            )
        )
    return scored


def _persist_scores(scored: list[tuple]):
    if not scored:
        return
    with get_db() as db:
        db.executemany(
            """
            INSERT INTO smart_money_trade_scores (
                trade_key, actor_type, actor_id, actor_name, ticker, direction,
                event_date, size_est_usd, size_confidence, entry_price,
                ret_1m, ret_3m, ret_6m, ret_1y,
                alpha_1m, alpha_3m, alpha_6m, alpha_1y, computed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CURRENT_TIMESTAMP)
            ON CONFLICT (trade_key) DO UPDATE SET
                ret_1m = EXCLUDED.ret_1m, ret_3m = EXCLUDED.ret_3m,
                ret_6m = EXCLUDED.ret_6m, ret_1y = EXCLUDED.ret_1y,
                alpha_1m = EXCLUDED.alpha_1m, alpha_3m = EXCLUDED.alpha_3m,
                alpha_6m = EXCLUDED.alpha_6m, alpha_1y = EXCLUDED.alpha_1y,
                entry_price = EXCLUDED.entry_price,
                size_est_usd = EXCLUDED.size_est_usd,
                size_confidence = EXCLUDED.size_confidence,
                computed_at = CURRENT_TIMESTAMP
            """,
            scored,
        )


def _aggregate(actor_type: str):
    """Roll per-trade scores up to an actor leaderboard, one row per horizon."""
    with get_db() as db:
        for horizon in WINDOWS:
            db.execute(
                f"""
                INSERT INTO smart_money_performance (
                    actor_type, actor_id, actor_name, horizon,
                    trade_count, scored_count, coverage_pct,
                    avg_return, avg_alpha, median_alpha, win_rate,
                    total_size_est, rankable, computed_at
                )
                SELECT
                    actor_type,
                    actor_id,
                    MAX(actor_name),
                    %s AS horizon,
                    COUNT(*)                                   AS trade_count,
                    COUNT(alpha_{horizon})                     AS scored_count,
                    ROUND((COUNT(alpha_{horizon})::NUMERIC / NULLIF(COUNT(*), 0)) * 100, 1),
                    AVG(ret_{horizon}),
                    AVG(alpha_{horizon}),
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY alpha_{horizon}),
                    -- A real win rate: share of scored positions that actually
                    -- beat the benchmark. Not a restatement of average return.
                    ROUND(
                        (COUNT(*) FILTER (WHERE alpha_{horizon} > 0)::NUMERIC
                         / NULLIF(COUNT(alpha_{horizon}), 0)) * 100, 1
                    ),
                    SUM(size_est_usd),
                    COUNT(alpha_{horizon}) >= %s               AS rankable,
                    CURRENT_TIMESTAMP
                FROM smart_money_trade_scores
                WHERE actor_type = %s
                GROUP BY actor_type, actor_id
                ON CONFLICT (actor_type, actor_id, horizon) DO UPDATE SET
                    actor_name    = EXCLUDED.actor_name,
                    trade_count   = EXCLUDED.trade_count,
                    scored_count  = EXCLUDED.scored_count,
                    coverage_pct  = EXCLUDED.coverage_pct,
                    avg_return    = EXCLUDED.avg_return,
                    avg_alpha     = EXCLUDED.avg_alpha,
                    median_alpha  = EXCLUDED.median_alpha,
                    win_rate      = EXCLUDED.win_rate,
                    total_size_est = EXCLUDED.total_size_est,
                    rankable      = EXCLUDED.rankable,
                    computed_at   = CURRENT_TIMESTAMP
                """,
                (horizon, MIN_SCORED_FOR_RANKING, actor_type),
            )


def compute_all() -> dict:
    """Full recompute of both actor types. Idempotent."""
    _ensure_tables()
    stats = {}

    for actor_type, source in (("congress", CONGRESS_SOURCE), ("fund", FUND_SOURCE)):
        logger.info("[returns] scoring %s...", actor_type)
        with get_db() as db:
            rows = db.execute(_build_scoring_query(source)).fetchall()

        scored = _score_rows(rows, actor_type)
        _persist_scores(scored)
        _aggregate(actor_type)

        stats[actor_type] = {"candidates": len(rows), "scored": len(scored)}
        logger.info("[returns] %s: %s", actor_type, stats[actor_type])

    stats["computed_at"] = datetime.now(timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(compute_all())
