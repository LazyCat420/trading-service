import logging
from typing import Dict, Any
from datetime import datetime, date, timedelta, timezone
from app.db import mongo_store

logger = logging.getLogger(__name__)


def _is_recent(val: Any, min_dt: datetime) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val)
        except Exception:
            return False
    if isinstance(val, date) and not isinstance(val, datetime):
        val = datetime.combine(val, datetime.min.time(), tzinfo=timezone.utc)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        if min_dt.tzinfo is None:
            min_dt = min_dt.replace(tzinfo=timezone.utc)
        return val >= min_dt
    return False


class DataCompletenessOracle:
    """
    Deterministically cross-references MongoDB to verify the 'Ground Truth'
    of what data was actually collected during a cycle for a specific ticker.
    """

    @staticmethod
    def verify_ground_truth(ticker: str) -> Dict[str, Any]:
        """
        Query MongoDB to produce a deterministic scorecard of whether FRESH
        evidence was actually gathered for this ticker.
        """
        results = {
            "ticker": ticker,
            "checklist": {},
            "completeness_score": 0.0,
            "missing_critical": [],
        }

        try:
            ticker_upper = ticker.upper().strip()
            now_utc = datetime.now(timezone.utc)

            # 1. Price history
            prices = mongo_store.find_docs("price_history", {"ticker": ticker_upper}, sort=[("date", -1)], limit=1)
            has_price = bool(prices and _is_recent(prices[0].get("date"), now_utc - timedelta(days=5)))

            # 2. Technicals
            techs = mongo_store.find_docs("technicals", {"ticker": ticker_upper}, sort=[("date", -1)], limit=1)
            has_tech = bool(techs and techs[0].get("rsi_14") is not None and _is_recent(techs[0].get("date"), now_utc - timedelta(days=5)))

            # 3. Fundamentals
            funds = mongo_store.find_docs("fundamentals", {"ticker": ticker_upper}, sort=[("snapshot_date", -1)], limit=1)
            has_fund = bool(funds and (funds[0].get("pe_ratio") is not None or funds[0].get("market_cap") is not None) and _is_recent(funds[0].get("snapshot_date"), now_utc - timedelta(days=30)))

            # 4. News
            news = mongo_store.find_docs("news_articles", {"ticker": ticker_upper}, sort=[("collected_at", -1)], limit=1)
            has_news = bool(news and _is_recent(news[0].get("collected_at") or news[0].get("published_at"), now_utc - timedelta(days=7)))

            checks = {
                "price_history": has_price,
                "technicals": has_tech,
                "fundamentals": has_fund,
                "news": has_news,
            }

            passed_checks = 0
            for key, passed in checks.items():
                results["checklist"][key] = passed
                if passed:
                    passed_checks += 1
                else:
                    results["missing_critical"].append(key)

            results["completeness_score"] = round((passed_checks / len(checks)) * 5.0, 2)
            return results

        except Exception as e:
            logger.error(f"Oracle failed to verify ground truth for {ticker}: {e}")
            return results
