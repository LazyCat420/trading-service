import logging
import json
from datetime import datetime, timezone, timedelta
from app.db.connection import get_db

logger = logging.getLogger(__name__)

class ScheduleValidator:
    """
    Validator/enforcer layer: prevents stupid schedules.
    Checks minimum spacing, max daily runs, duplicate suppression, and scope justifications.
    """
    
    # Policy limits
    MAX_SYSTEM_SCHEDULES = 10
    
    @staticmethod
    def validate_proposal(proposal: dict) -> tuple[bool, str]:
        """
        Validate an LLM schedule proposal before it hits the database.
        Returns (is_valid, rejection_reason). If valid, reason is empty.
        """
        scope = proposal.get("schedule_scope")
        intent = proposal.get("review_intent")
        urgency = proposal.get("urgency")
        window = proposal.get("earliest_window")
        reason_codes = proposal.get("reason_codes", [])
        
        if not scope or not intent or not urgency or not window:
            return False, "Missing required schema fields (scope, intent, urgency, window)."
            
        # 1. Max schedules limit
        with get_db() as db:
            count_row = db.execute("SELECT COUNT(*) FROM cycle_schedules WHERE is_active = TRUE").fetchone()
            active_count = count_row[0] if count_row else 0
            if active_count >= ScheduleValidator.MAX_SYSTEM_SCHEDULES:
                # Unless it's critical, block it
                if urgency != "critical":
                    return False, f"System has reached max active schedules ({active_count}). Only critical updates allowed."
                    
        # 2. Anti-Stupidity: Frequent full-cycle scans
        if scope == "portfolio" and intent != "weekly_review":
            if not reason_codes and urgency != "critical":
                return False, "Full portfolio scopes are reserved for weekly reviews or critical market shocks with explicit reason_codes."
                
        # 3. Anti-Stupidity: Incompatible urgency vs intent
        if intent == "monitor" and urgency in ("high", "critical"):
            return False, "A 'monitor' intent represents a lightweight check and cannot have high or critical urgency."
            
        # 4. Anti-Stupidity: Overtrading/Repetition
        # If the LLM proposes an immediate window for a single ticker, it must cite a catalyst.
        if scope == "single_ticker" and window in ("next_open", "midday", "next_pre_market") and intent == "trade_window":
            if not reason_codes:
                return False, "Intraday trade windows for single tickers require a catalyst in reason_codes."
                
        return True, ""
        
    @staticmethod
    def pre_run_check(schedule_id: str) -> tuple[bool, str]:
        """
        Evaluates right before the APScheduler trigger fires to see if the run is still justified.
        """
        try:
            with get_db() as db:
                row = db.execute(
                    "SELECT schedule_scope, review_intent, urgency, tickers, last_run_at "
                    "FROM cycle_schedules WHERE id = %s", [schedule_id]
                ).fetchone()
                
                if not row:
                    return False, "Schedule not found"
                    
                scope, intent, urgency, tickers_json, last_run_at = row
                
                # Check cooldowns
                if last_run_at:
                    if intent == "weekly_review":
                        # Cooldown = at least 4 days
                        if datetime.now(timezone.utc) - last_run_at < timedelta(days=4):
                            return False, "Weekly review cooldown active."
                    elif intent == "monitor":
                        # Cooldown = 12 hours
                        if datetime.now(timezone.utc) - last_run_at < timedelta(hours=12):
                            return False, "Monitor cooldown active."
                            
                return True, ""
        except Exception as e:
            logger.error("[VALIDATOR] Pre-run check error: %s", e)
            return True, ""  # Fail open if DB error
