"""
Finnhub Collector — Fetches news, analyst targets, earnings calendar.

Pure data collector. No LLM calls. No processing.
Writes to: news_articles (via news_collector proxy), fundamentals
(target_price, recom_score, earnings_date — merged into the newest EXISTING
row; this collector never creates a fundamentals row, see
_merge_into_fundamentals).
Requires: FINNHUB_API_KEY in .env (free tier = 60 calls/min)
"""

import logging

logger = logging.getLogger(__name__)


import hashlib
import datetime
import finnhub
from app.config import settings
from app.db import mongo_store


def _get_client() -> finnhub.Client:
    """Get Finnhub client. Raises ValueError if no API key."""
    key = settings.FINNHUB_API_KEY
    if not key:
        raise ValueError("FINNHUB_API_KEY not set in .env — get free key at finnhub.io")
    return finnhub.Client(api_key=key)


async def collect_news(ticker: str, days_back: int = 7) -> int:
    """
    DEPRECATED: Proxy to the robust Trafilatura-powered news collector.
    The raw Finnhub API only returns cut-off summaries. 
    This proxy ensures all news scraping goes through the unified engine 
    which visits URLs to extract full article bodies.
    """
    try:
        from app.collectors.news_collector import collect_finnhub_news
        logger.info(f"[finnhub_collector] Proxying collect_news({ticker}) to robust news_collector...")
        return await collect_finnhub_news(ticker, days=days_back)
    except Exception as e:
        logger.error(f"[finnhub_collector] Proxy call error: {e}")
        return 0


def _merge_into_fundamentals(ticker: str, fields: dict) -> None:
    """Merge a partial field set into the newest EXISTING fundamentals row.

    THIS MUST NOT CREATE A ROW. It used to key on today's date, so on any day
    the full snapshot collector had not run — every fast-path cycle skips
    `collect_fundamentals` — an earnings-date lookup wrote a four-field
    document `{ticker, snapshot_date, source, earnings_date}` for TODAY. Since
    every reader takes the newest row, that stub then hid the previous day's
    full snapshot and reported all 23 verified ratios as NOT ON FILE.

    Measured 2026-09-03: the fundamental analyst's own `get_upcoming_events`
    call created DELL's stub mid-cycle at 19:07:27, and the debate that
    followed argued about "16 data gaps" against a 41-field snapshot sitting
    one document away. NVDA and DKS had the same stub from the same path.

    Keying on the newest existing row is one rule with no branch: it IS today's
    row whenever today's row exists, and yesterday's when it does not — which
    is where a target or an earnings date belongs anyway, since it describes
    the same company on the same snapshot.

    `source` is deliberately NOT written: this collector supplies a few fields,
    it does not author the row, and stamping it relabelled yfinance-shaped rows
    as finnhub ones (DELL's 09-02 row still carries the wrong label).
    """
    doc = {c: v for c, v in fields.items() if v is not None}
    if not doc:
        return

    rows = mongo_store.find_docs(
        'fundamentals', {'ticker': ticker.upper()},
        sort=[('snapshot_date', -1)], limit=1,
    ) or []
    if not rows:
        logger.info(
            "[finnhub] %s: no fundamentals snapshot on file — %s not merged "
            "(a supplement must not be the only row for a ticker)",
            ticker, ", ".join(sorted(doc)),
        )
        return

    key = {'ticker': ticker.upper(), 'snapshot_date': rows[0].get('snapshot_date')}
    mongo_store.upsert_doc('fundamentals', key, doc)


async def collect_analyst_targets(ticker: str) -> bool:
    """
    Fetch analyst price targets from Finnhub and persist the mean target
    into today's fundamentals row (fills the gap when yfinance/finviz
    didn't supply one).
    """
    try:
        client = _get_client()
        import asyncio

        target = await asyncio.to_thread(client.price_target, ticker)

        if not target or "targetHigh" not in target:
            logger.info(f"[finnhub] No analyst targets for {ticker}")
            return False

        _merge_into_fundamentals(ticker, {"target_price": target.get("targetMean")})
        logger.info(
            f"[finnhub] {ticker}: analyst targets written — "
            f"high={target.get('targetHigh')}, "
            f"low={target.get('targetLow')}, "
            f"mean={target.get('targetMean')}"
        )
        return True
    except Exception as e:
        logger.info(f"[finnhub] Error fetching analyst targets for {ticker}: {e}")
        return False


async def collect_earnings_calendar(ticker: str) -> list[dict]:
    """
    Fetch upcoming earnings dates for a ticker.
    Returns list of earnings events (not written to DB — used by alert engine).
    """
    try:
        client = _get_client()
        today = datetime.date.today()
        future = today + datetime.timedelta(days=90)

        import asyncio

        earnings = await asyncio.to_thread(
            client.earnings_calendar, _from=str(today), to=str(future), symbol=ticker
        )

        events = earnings.get("earningsCalendar", [])
        if events:
            # Persist the next earnings date into today's fundamentals row
            # (events come newest-last; take the soonest upcoming date).
            dates = sorted(e["date"] for e in events if e.get("date"))
            if dates:
                try:
                    _merge_into_fundamentals(
                        ticker,
                        {"earnings_date": datetime.date.fromisoformat(dates[0])},
                    )
                except Exception as we:
                    logger.info(f"[finnhub] earnings date write failed for {ticker}: {we}")
            logger.info(f"[finnhub] {ticker}: {len(events)} upcoming earnings events")
        else:
            logger.info(f"[finnhub] {ticker}: no upcoming earnings")
        return events
    except Exception as e:
        logger.info(f"[finnhub] Error fetching earnings calendar for {ticker}: {e}")
        return []


async def collect_recommendation_trends(ticker: str) -> list[dict]:
    """
    Fetch analyst recommendation trends (buy/sell/hold/strongBuy/strongSell).
    Returns list of monthly recommendation snapshots.
    """
    try:
        client = _get_client()
        import asyncio

        trends = await asyncio.to_thread(client.recommendation_trends, ticker)

        if trends:
            latest = trends[0]
            # Persist as a finviz-style 1-5 score (1 = strong buy): weighted
            # mean over the analyst counts of the latest monthly snapshot.
            counts = [
                (1, latest.get("strongBuy") or 0),
                (2, latest.get("buy") or 0),
                (3, latest.get("hold") or 0),
                (4, latest.get("sell") or 0),
                (5, latest.get("strongSell") or 0),
            ]
            total = sum(n for _, n in counts)
            if total:
                score = sum(w * n for w, n in counts) / total
                try:
                    _merge_into_fundamentals(ticker, {"recom_score": round(score, 2)})
                except Exception as we:
                    logger.info(f"[finnhub] recom write failed for {ticker}: {we}")
            logger.info(
                f"[finnhub] {ticker}: recommendations — "
                f"buy={latest.get('buy')}, hold={latest.get('hold')}, "
                f"sell={latest.get('sell')}"
            )
        else:
            logger.info(f"[finnhub] {ticker}: no recommendation trends")
        return trends
    except Exception as e:
        logger.info(f"[finnhub] Error fetching recommendation trends for {ticker}: {e}")
        return []


async def collect_all(ticker: str) -> dict:
    """Run all Finnhub collectors for a single ticker."""
    news_count = await collect_news(ticker)
    targets = await collect_analyst_targets(ticker)
    earnings = await collect_earnings_calendar(ticker)
    recommendations = await collect_recommendation_trends(ticker)

    return {
        "ticker": ticker,
        "news_articles": news_count,
        "analyst_targets": targets,
        "earnings_events": len(earnings),
        "recommendation_snapshots": len(recommendations),
    }
