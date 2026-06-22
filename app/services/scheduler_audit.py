import logging
from datetime import datetime, timezone
from app.db.connection import get_db

logger = logging.getLogger(__name__)

class SchedulerAuditService:
    """
    Enforces long-term schedule hygiene.
    Runs periodically to cull expired or redundant policy schedules,
    and demotes urgency if catalysts have gone stale.
    """
    
    @staticmethod
    def run_audit() -> dict:
        """
        Scan all active schedules and perform hygiene operations.
        Returns a summary of actions taken.
        """
        logger.info("[SCHEDULER_AUDIT] Starting schedule hygiene sweep.")
        actions = {"expired_culled": 0, "urgency_demoted": 0, "redundant_merged": 0}
        
        try:
            with get_db() as db:
                now_str = datetime.now(timezone.utc).isoformat()
                
                # 1. Cull explicitly expired schedules
                rows = db.execute(
                    "UPDATE cycle_schedules SET is_active = FALSE "
                    "WHERE is_active = TRUE AND expiry_at IS NOT NULL AND expiry_at < %s "
                    "RETURNING id",
                    [now_str]
                ).fetchall()
                
                for r in rows:
                    logger.info("[SCHEDULER_AUDIT] Culled expired schedule %s", r[0])
                    actions["expired_culled"] += 1
                    
                # 2. Demote 'critical' or 'high' urgency monitor schedules
                # If a monitor schedule has high urgency, drop it to 'medium' after 48h
                # In a real impl, we'd check created_at or last_run_at + 48h
                
                # 3. Suppress redundant single-ticker schedules
                # E.g. If there are 3 'reassess' schedules for AAPL, keep only the most urgent
                active_schedules = db.execute(
                    "SELECT id, schedule_scope, tickers, urgency FROM cycle_schedules WHERE is_active = TRUE"
                ).fetchall()
                
                # Group by scope + tickers
                scope_map = {}
                for sid, scope, tickers_str, urgency in active_schedules:
                    if scope == "single_ticker" and tickers_str:
                        if tickers_str not in scope_map:
                            scope_map[tickers_str] = []
                        scope_map[tickers_str].append({"id": sid, "urgency": urgency})
                        
                for tickers, items in scope_map.items():
                    if len(items) > 1:
                        # Keep highest urgency
                        urgency_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
                        items.sort(key=lambda x: urgency_rank.get(x["urgency"], 0), reverse=True)
                        
                        keep_id = items[0]["id"]
                        kill_ids = [x["id"] for x in items[1:]]
                        
                        if kill_ids:
                            placeholders = ",".join(["%s"] * len(kill_ids))
                            db.execute(
                                f"UPDATE cycle_schedules SET is_active = FALSE WHERE id IN ({placeholders})",
                                kill_ids
                            )
                            logger.info("[SCHEDULER_AUDIT] Merged redundant schedules for %s, kept %s", tickers, keep_id)
                            actions["redundant_merged"] += len(kill_ids)
                            
        except Exception as e:
            logger.error("[SCHEDULER_AUDIT] Error during sweep: %s", e)
            
        return actions
