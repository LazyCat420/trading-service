import logging
from typing import Dict, Any
from app.db.connection import get_db

logger = logging.getLogger(__name__)


class DataCompletenessOracle:
    """
    Deterministically cross-references PostgreSQL to verify the 'Ground Truth'
    of what data was actually collected during a cycle for a specific ticker.
    """

    # Define what "Complete Evidence" means functionally.
    # We can expand this per-asset_class later.
    #
    # Each check is FRESHNESS-AWARE: the old existence-only probes ("a row has
    # ever existed for this ticker") scored stale tickers as complete, which
    # poisoned 50% of every judge score. Windows are sized per data cadence:
    # daily bars/technicals allow weekends+holidays, fundamentals snapshots
    # are infrequent, news must be recent to count as this cycle's evidence.
    EXPECTED_TABLES = {
        "price_history": (
            "SELECT 1 FROM price_history WHERE ticker = %s "
            "AND date >= CURRENT_DATE - INTERVAL '5 days' LIMIT 1"
        ),
        "technicals": (
            "SELECT 1 FROM technicals WHERE ticker = %s AND rsi_14 IS NOT NULL "
            "AND date >= CURRENT_DATE - INTERVAL '5 days' LIMIT 1"
        ),
        "fundamentals": (
            "SELECT 1 FROM fundamentals WHERE ticker = %s "
            "AND (pe_ratio IS NOT NULL OR market_cap IS NOT NULL) "
            "AND snapshot_date >= CURRENT_DATE - INTERVAL '30 days' LIMIT 1"
        ),
        "news": (
            "SELECT 1 FROM news_articles WHERE ticker = %s "
            "AND collected_at >= NOW() - INTERVAL '7 days' LIMIT 1"
        ),
    }

    @staticmethod
    def verify_ground_truth(ticker: str) -> Dict[str, Any]:
        """
        Query PostgreSQL to produce a deterministic scorecard of whether FRESH
        evidence was actually gathered for this ticker (see window comments on
        EXPECTED_TABLES — existence-only probes previously counted stale data).
        """
        with get_db() as db:
            results = {
                "ticker": ticker,
                "checklist": {},
                "completeness_score": 0.0,
                "missing_critical": [],
            }

            try:
                total_checks = len(DataCompletenessOracle.EXPECTED_TABLES)
                passed_checks = 0

                for key, query in DataCompletenessOracle.EXPECTED_TABLES.items():
                    row = db.execute(query, [ticker]).fetchone()
                    passed = row is not None
                    results["checklist"][key] = passed

                    if passed:
                        passed_checks += 1
                    else:
                        results["missing_critical"].append(key)

                if total_checks > 0:
                    results["completeness_score"] = round(
                        (passed_checks / total_checks) * 5.0, 2
                    )

                return results

            except Exception as e:
                logger.error(f"Oracle failed to verify ground truth for {ticker}: {e}")
                return results
