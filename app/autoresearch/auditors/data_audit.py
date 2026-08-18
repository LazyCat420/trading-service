import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator


logger = logging.getLogger(__name__)


from app.autoresearch.utils import _grade, _safe_iso
from app.db import mongo_query, mongo_store


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


def _audit_price_history(ticker: str) -> dict:
    try:
        # The three SUM(CASE ...) columns are multi-field OR predicates, which
        # agg_row's (op, field) vocabulary cannot express, so they are counted
        # with $cond in one $group — still one round trip, and still server-side
        # (price_history is the 15.7M-row table; pulling it to count in Python
        # would replace a fast query with a slow one).
        _null = lambda f: {"$or": [{"$eq": [f"${f}", None]},
                                   {"$eq": [{"$type": f"${f}"}, "missing"]}]}
        agg = mongo_store.aggregate('price_history', [
            {"$match": {"ticker": ticker}},
            {"$group": {
                "_id": None,
                "n": {"$sum": 1},
                "min_d": {"$min": "$date"},
                "max_d": {"$max": "$date"},
                "null_close": {"$sum": {"$cond": [_null("close"), 1, 0]}},
                "zero_vol": {"$sum": {"$cond": [
                    {"$or": [_null("volume"), {"$eq": ["$volume", 0]}]}, 1, 0]}},
                "null_ohlc": {"$sum": {"$cond": [
                    {"$or": [_null("open"), _null("high"), _null("low")]}, 1, 0]}},
            }},
        ])
        if not agg:
            return {"rows": 0, "quality": "critical", "quality_score": 0}
        s = agg[0]
        rows, min_d, max_d = s["n"], s["min_d"], s["max_d"]
        null_close, zero_vol, null_ohlc = s["null_close"], s["zero_vol"], s["null_ohlc"]
        if rows == 0:
            return {"rows": 0, "quality": "critical", "quality_score": 0}

        # LEAD(date) OVER (ORDER BY date): a window function has no Mongo
        # equivalent below server 5.0's $setWindowFields, and this only needs
        # the date column, so the gap scan is done over a projected, sorted
        # date list — one field, not whole rows.
        dates = [d[0] for d in mongo_query.find_rows(
            'price_history', {'ticker': ticker}, ['date'], sort=[('date', 1)])]
        gaps = 0
        for prev, nxt in zip(dates, dates[1:]):
            try:
                delta = (nxt - prev)
                # `next_date::date - date::date > 4` is a difference in whole
                # DAYS. A BSON date subtraction gives a timedelta, whose .days
                # is that same whole-day count — using .total_seconds() here
                # would compare seconds against 4 and count every gap.
                if delta.days > 4:
                    gaps += 1
            except TypeError:
                continue

        latest = mongo_query.find_row('price_history', {'ticker': ticker}, ['date', 'open', 'high', 'low', 'close', 'volume'], sort=[('date', -1)])

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

def _audit_technicals(ticker: str) -> dict:
    INDICATORS = [
        "rsi_14", "macd", "macd_signal", "macd_hist", "sma_20", "sma_50", "sma_200",
        "ema_12", "ema_26", "bb_upper", "bb_mid", "bb_lower", "atr_14", "adx_14",
        "stoch_k", "stoch_d", "obv", "vwap", "support", "resistance"
    ]
    try:
        stats = mongo_query.agg_row('technicals', {'ticker': ticker}, [('count', None), ('min', 'date'), ('max', 'date')])
        rows, min_d, max_d = stats

        if rows == 0:
            return {"rows": 0, "quality": "critical", "quality_score": 0, "indicators_computed": 0}

        indicator_health = {}
        total_nulls = 0
        indicators_ok = 0

        # One pass per indicator became 40 round trips; the same numbers come
        # from a single $group over the ticker's rows. COUNT(col) skips NULLs
        # and the SUM(CASE) counts them, exactly as agg_row's count/count_null.
        group: dict = {"_id": None}
        for col in INDICATORS:
            group[f"{col}__n"] = {"$sum": {"$cond": [{"$eq": [f"${col}", None]}, 0, 1]}}
            group[f"{col}__nulls"] = {"$sum": {"$cond": [{"$eq": [f"${col}", None]}, 1, 0]}}
            group[f"{col}__min"] = {"$min": f"${col}"}
            group[f"{col}__max"] = {"$max": f"${col}"}
        agg = mongo_store.aggregate(
            'technicals', [{"$match": {"ticker": ticker}}, {"$group": group}])
        stats_doc = agg[0] if agg else {}
        # The latest row, once, for every indicator's "latest" value.
        latest_doc = mongo_query.find_row(
            'technicals', {'ticker': ticker}, INDICATORS, sort=[('date', -1)])

        for idx, col in enumerate(INDICATORS):
            try:
                non_null = stats_doc.get(f"{col}__n") or 0
                nulls = stats_doc.get(f"{col}__nulls") or 0
                min_v = stats_doc.get(f"{col}__min")
                max_v = stats_doc.get(f"{col}__max")
                null_pct = nulls / rows if rows else 0
                total_nulls += nulls

                latest_val = (latest_doc[idx],) if latest_doc else None

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

def _audit_fundamentals(ticker: str) -> dict:
    try:
        stats = mongo_query.agg_row('fundamentals', {'ticker': ticker}, [('count', None), ('min', 'snapshot_date'), ('max', 'snapshot_date')])
        rows, min_d, max_d = stats

        if rows == 0:
            return {"rows": 0, "quality": "critical", "quality_score": 0}

        key_fields = ["market_cap", "pe_ratio", "revenue", "profit_margin", "debt_to_equity"]
        # `SELECT *` + a cursor.description introspection existed only to turn
        # the positional row into a dict keyed by column name. A Mongo document
        # already IS that dict, so both queries collapse into one read.
        latest_docs = mongo_query.find_dicts(
            'fundamentals', {'ticker': ticker},
            sort=[('snapshot_date', -1)], limit=1)
        data = latest_docs[0] if latest_docs else {}

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

def _audit_news(ticker: str) -> dict:
    try:
        # CURRENT_TIMESTAMP - INTERVAL '7 days' is evaluated in Python: a
        # relative interval inside the pipeline would be recomputed per shard.
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        rows, min_d, max_d, sources = mongo_query.agg_row(
            'news_articles', {'ticker': ticker},
            [('count', None), ('min', 'published_at'), ('max', 'published_at'),
             ('count_distinct', 'source')])
        # COUNT(*) FILTER (...) is a differently-filtered count, so it is its
        # own query rather than a column of the aggregate above.
        recent = mongo_query.count('news_articles',
                                   {'ticker': ticker,
                                    'published_at': {'$gt': cutoff}})

        source_list = []
        if rows > 0:
            src = mongo_query.group_rows('news_articles', {'ticker': ticker}, ['source'], [('count', None)], [('key', 'source'), ('agg', 0)])
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
    for ticker in tickers:
        try:
            cats = [
                _audit_price_history(ticker),
                _audit_technicals(ticker),
                _audit_fundamentals(ticker),
                _audit_news(ticker),
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
