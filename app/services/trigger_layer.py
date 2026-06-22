import logging
from datetime import datetime, timedelta, timezone
from app.db.connection import get_db

logger = logging.getLogger(__name__)

class TriggerLayer:
    """
    Consumes catalysts such as breaking news, earnings windows, 
    portfolio drawdown, stop-loss breaches, and thesis contradiction.
    """
    
    @staticmethod
    def evaluate_catalysts(tickers: list[str]) -> dict:
        """
        Evaluate if a list of tickers has any fresh catalysts.
        Returns a dict mapping ticker -> list of active reason codes.
        """
        if not tickers:
            return {}
            
        results = {t: [] for t in tickers}
        
        try:
            with get_db() as db:
                placeholders = ",".join(["%s"] * len(tickers))
                
                # Threshold for "fresh" catalyst (e.g. 24 hours)
                time_threshold = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
                
                # 1. Recent high-quality news
                rows = db.execute(f"""
                    SELECT DISTINCT ticker FROM news_articles 
                    WHERE ticker IN ({placeholders}) 
                    AND published_at >= %s
                    AND quality_score >= 70
                """, tickers + [time_threshold]).fetchall()
                
                for r in rows:
                    results[r[0]].append("news_catalyst")
                    
                # 2. Active fund alerts
                rows = db.execute(f"""
                    SELECT DISTINCT ticker FROM fund_alerts 
                    WHERE ticker IN ({placeholders}) 
                    AND created_at >= %s
                """, tickers + [time_threshold]).fetchall()
                
                for r in rows:
                    if "fund_alert" not in results[r[0]]:
                        results[r[0]].append("fund_alert")
                        
                # 3. Portfolio Risk (Drawdowns)
                # If the ticker is an open position nearing a stop-loss or in drawdown
                rows = db.execute(f"""
                    SELECT p.ticker, p.avg_entry_price, s.price 
                    FROM positions p
                    JOIN market_snapshots s ON p.ticker = s.ticker
                    WHERE p.ticker IN ({placeholders})
                """, tickers).fetchall()
                
                for r in rows:
                    ticker = r[0]
                    avg_entry = r[1]
                    current_price = r[2]
                    if avg_entry and current_price:
                        drawdown = (avg_entry - current_price) / avg_entry
                        if drawdown > 0.05:  # 5% drawdown threshold
                            if "portfolio_risk" not in results[ticker]:
                                results[ticker].append("portfolio_risk")
                                
        except Exception as e:
            logger.error("[TRIGGER-LAYER] Error evaluating catalysts: %s", e)
            
        # Return only tickers that actually have triggers
        return {k: v for k, v in results.items() if v}

    @staticmethod
    def has_material_change(tickers: list[str]) -> bool:
        """
        Returns True if any of the tickers have a material change justifying a priority run.
        """
        catalysts = TriggerLayer.evaluate_catalysts(tickers)
        return len(catalysts) > 0
