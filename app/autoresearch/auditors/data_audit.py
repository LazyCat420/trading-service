import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


from app.autoresearch.utils import _grade, _safe_iso


def _age_days(max_date) -> int | None:
    """Days since the newest row, tolerant of date/datetime/ISO-string columns."""
    if max_date is None:
        return None
    try:
        if isinstance(max_date, str):
            max_date = datetime.fromisoformat(max_date[:19])
        if isinstance(max_date, datetime):
            max_date = max_date.date()
        return (datetime.now(timezone.utc).date() - max_date).days
    except Exception:
        return None


def _freshness_multiplier(age: int | None, fresh_days: int, floor_days: int) -> float:
    """1.0 while data is <= fresh_days old, linear decay to 0.3 at floor_days.

    Completeness alone let a table full of week-old rows score ~99 — the audit
    said "great data" while agents analyzed stale prices.
    """
    if age is None:
        return 0.3
    if age <= fresh_days:
        return 1.0
    if age >= floor_days:
        return 0.3
    return 1.0 - 0.7 * (age - fresh_days) / (floor_days - fresh_days)


def _audit_price_history(db, ticker: str) -> dict:
    try:
        stats = db.execute(
            """
            SELECT COUNT(*), MIN(date), MAX(date),
                   SUM(CASE WHEN close IS NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN volume IS NULL OR volume = 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN open IS NULL OR high IS NULL OR low IS NULL THEN 1 ELSE 0 END)
            FROM price_history WHERE ticker = %s
            """,
            [ticker],
        ).fetchone()

        rows, min_d, max_d, null_close, zero_vol, null_ohlc = stats
        if rows == 0:
            return {"rows": 0, "quality": "critical", "quality_score": 0}

        gaps = db.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT date, LEAD(date) OVER (ORDER BY date) as next_date
                FROM price_history WHERE ticker = %s
            ) sub WHERE next_date::date - date::date > 4
            """,
            [ticker],
        ).fetchone()[0]

        latest = db.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM price_history WHERE ticker = %s
            ORDER BY date DESC LIMIT 1
            """,
            [ticker],
        ).fetchone()

        null_pct = (null_close + zero_vol + null_ohlc) / (rows * 3) if rows else 1
        gap_penalty = min(gaps * 0.05, 0.3)
        age = _age_days(max_d)
        # 3 days tolerates weekends + one missed collection; 10 = unusable.
        score = max(0, 1.0 - null_pct - gap_penalty) * _freshness_multiplier(age, 3, 10)

        return {
            "rows": rows,
            "date_range": [_safe_iso(min_d), _safe_iso(max_d)],
            "age_days": age,
            "quality": _grade(score),
            "quality_score": round(score, 3),
            "null_close": null_close,
            "zero_volume_days": zero_vol,
            "null_ohlc": null_ohlc,
            "gaps_over_4_days": gaps,
            "latest": {
                "date": _safe_iso(latest[0]),
                "close": round(latest[4], 2) if latest[4] else None,
                "volume": latest[5],
            } if latest else None,
        }
    except Exception as e:
        logger.warning("audit price_history failed for %s: %s", ticker, e)
        return {"rows": 0, "quality": "error", "error": str(e)}

def _audit_technicals(db, ticker: str) -> dict:
    INDICATORS = [
        "rsi_14", "macd", "macd_signal", "macd_hist", "sma_20", "sma_50", "sma_200",
        "ema_12", "ema_26", "bb_upper", "bb_mid", "bb_lower", "atr_14", "adx_14",
        "stoch_k", "stoch_d", "obv", "vwap", "support", "resistance"
    ]
    try:
        stats = db.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM technicals WHERE ticker = %s",
            [ticker]
        ).fetchone()
        rows, min_d, max_d = stats

        if rows == 0:
            return {"rows": 0, "quality": "critical", "quality_score": 0, "indicators_computed": 0}

        indicator_health = {}
        total_nulls = 0
        indicators_ok = 0

        for col in INDICATORS:
            try:
                ind = db.execute(
                    f"""
                    SELECT COUNT({col}), SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END),
                           MIN({col}), MAX({col}), AVG({col})
                    FROM technicals WHERE ticker = %s
                    """,
                    [ticker],
                ).fetchone()

                non_null, nulls, min_v, max_v, avg_v = ind
                null_pct = nulls / rows if rows else 0
                total_nulls += nulls

                latest_val = db.execute(
                    f"SELECT {col} FROM technicals WHERE ticker = %s ORDER BY date DESC LIMIT 1",
                    [ticker],
                ).fetchone()

                status = "ok" if null_pct < 0.1 else "degraded" if null_pct < 0.5 else "poor"
                if non_null > 0:
                    indicators_ok += 1

                indicator_health[col] = {
                    "status": status,
                    "latest": round(latest_val[0], 4) if latest_val and latest_val[0] is not None else None,
                    "range": [round(min_v, 4) if min_v is not None else None, round(max_v, 4) if max_v is not None else None],
                    "nulls": nulls,
                    "null_pct": round(null_pct * 100, 1),
                }
            except Exception:
                indicator_health[col] = {"status": "error", "latest": None, "nulls": rows}

        total_cells = rows * len(INDICATORS) or 1
        age = _age_days(max_d)
        score = max(0, 1.0 - (total_nulls / total_cells)) * _freshness_multiplier(age, 3, 10)

        return {
            "rows": rows,
            "date_range": [_safe_iso(min_d), _safe_iso(max_d)],
            "age_days": age,
            "quality": _grade(score),
            "quality_score": round(score, 3),
            "indicators_computed": indicators_ok,
            "indicators_total": len(INDICATORS),
            "indicators_with_nulls": sum(1 for v in indicator_health.values() if v.get("nulls", 0) > 0),
            "indicator_health": indicator_health,
        }
    except Exception as e:
        logger.warning("audit technicals failed for %s: %s", ticker, e)
        return {"rows": 0, "quality": "error", "error": str(e)}

def _audit_fundamentals(db, ticker: str) -> dict:
    try:
        stats = db.execute(
            "SELECT COUNT(*), MIN(snapshot_date), MAX(snapshot_date) FROM fundamentals WHERE ticker = %s",
            [ticker]
        ).fetchone()
        rows, min_d, max_d = stats

        if rows == 0:
            return {"rows": 0, "quality": "critical", "quality_score": 0}

        key_fields = ["market_cap", "pe_ratio", "revenue", "profit_margin", "debt_to_equity"]
        latest = db.execute(
            "SELECT * FROM fundamentals WHERE ticker = %s ORDER BY snapshot_date DESC LIMIT 1",
            [ticker]
        ).fetchone()
        
        cols = [d[0] for d in db.execute("SELECT * FROM fundamentals LIMIT 0").description]
        data = dict(zip(cols, latest)) if latest else {}

        non_null_key = sum(1 for f in key_fields if data.get(f) is not None)
        age = _age_days(max_d)
        # Fundamentals turn over quarterly: 60d fresh, 180d = two missed quarters.
        score = (non_null_key / len(key_fields) if key_fields else 0) * _freshness_multiplier(age, 60, 180)

        key_values = {}
        for f in key_fields:
            v = data.get(f)
            key_values[f] = round(v, 4) if isinstance(v, float) else v

        return {
            "rows": rows,
            "date_range": [_safe_iso(min_d), _safe_iso(max_d)],
            "age_days": age,
            "quality": _grade(score),
            "quality_score": round(score, 3),
            "key_fields": key_values,
            "key_fields_present": f"{non_null_key}/{len(key_fields)}",
        }
    except Exception as e:
        logger.warning("audit fundamentals failed for %s: %s", ticker, e)
        return {"rows": 0, "quality": "error", "error": str(e)}

def _audit_news(db, ticker: str) -> dict:
    try:
        stats = db.execute(
            """
            SELECT COUNT(*), MIN(published_at), MAX(published_at), COUNT(DISTINCT source),
                   COUNT(*) FILTER (WHERE published_at > CURRENT_TIMESTAMP - INTERVAL '7 days')
            FROM news_articles WHERE ticker = %s
            """,
            [ticker]
        ).fetchone()
        rows, min_d, max_d, sources, recent = stats

        source_list = []
        if rows > 0:
            src = db.execute(
                "SELECT source, COUNT(*) FROM news_articles WHERE ticker = %s GROUP BY source",
                [ticker]
            ).fetchall()
            source_list = [{"source": r[0], "count": r[1]} for r in src]

        # Score on articles from the last 7 days — the lifetime count let a
        # ticker with 50 stale articles and zero current coverage score 1.0.
        score = min(1.0, recent / 5) if recent else 0
        return {
            "rows": rows,
            "recent_7d": recent,
            "date_range": [_safe_iso(min_d), _safe_iso(max_d)],
            "age_days": _age_days(max_d),
            "quality": _grade(score),
            "quality_score": round(score, 3),
            "source_count": sources,
            "sources": source_list,
        }
    except Exception as e:
        return {"rows": 0, "quality": "error", "error": str(e)}

def _audit_data_quality(tickers: list[str]) -> dict:
    if not tickers:
        return {"avg_score": 0, "gaps": [], "per_ticker": {}}
    from app.trading.watchlist import _snapshot_market_data, ban_ticker

    per_ticker, gaps, scores, purged_tickers = {}, [], [], []
    with get_db() as db:
        for ticker in tickers:
            try:
                cats = [
                    _audit_price_history(db, ticker),
                    _audit_technicals(db, ticker),
                    _audit_fundamentals(db, ticker),
                    _audit_news(db, ticker),
                ]
                # Missing categories count as 0 — the old average silently
                # dropped them, so a ticker with no fundamentals and no news
                # could still score ~1.0 off prices+technicals alone.
                cat_scores = [
                    c.get("quality_score", 0) if isinstance(c.get("quality_score"), (int, float)) else 0
                    for c in cats
                ]
                avg = sum(cat_scores) / len(cats) if cats else 0
                scores.append(avg)
                per_ticker[ticker] = {"score": round(avg, 3)}
                missing = []
                for name, cat in zip(["price_history", "technicals", "fundamentals", "news"], cats):
                    if cat.get("rows", 0) == 0:
                        missing.append(name)
                if missing:
                    market_cap, price, volume = _snapshot_market_data(ticker)

                    is_junk = False
                    junk_reason = ""
                    if price is not None and price < 1.00:
                        is_junk = True
                        junk_reason = f"Penny stock (Price: ${price:.4f})"
                    elif market_cap is not None and market_cap > 0 and market_cap < 50_000_000:
                        is_junk = True
                        junk_reason = f"Micro-cap (Cap: ${market_cap:,.0f})"
                    elif price is not None and volume is not None and volume == 0:
                        is_junk = True
                        junk_reason = "Zero volume"

                    if is_junk:
                        ban_ticker(ticker, f"AutoResearch Context-Aware Pruning: {junk_reason}")
                        purged_tickers.append({"ticker": ticker, "reason": junk_reason})
                    else:
                        gaps.append({
                            "ticker": ticker,
                            "missing_sources": missing,
                            "recommendation": f"Re-collect {', '.join(missing)} for {ticker}",
                        })
            except Exception as e:
                scores.append(0)
                per_ticker[ticker] = {"score": 0, "error": str(e)}

    return {
        "avg_score": round(sum(scores) / len(scores), 3) if scores else 0,
        "gaps": gaps,
        "purged_tickers": purged_tickers,
        "per_ticker": per_ticker,
    }
