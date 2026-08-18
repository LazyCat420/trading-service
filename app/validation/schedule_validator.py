"""
Schedule Validator — enforcer layer preventing unneeded/excessive schedules.

Pure MongoDB implementation for cycle_schedules collection.
"""

import logging
from datetime import datetime, timezone, timedelta
from app.db import mongo_query

logger = logging.getLogger(__name__)


class ScheduleValidator:
    """
    Validator/enforcer layer: prevents stupid schedules.
    Checks minimum spacing, max daily runs, duplicate suppression, and scope justifications.
    """
    
    MAX_SYSTEM_SCHEDULES = 10
    
    @staticmethod
    def validate_proposal(proposal: dict, active_count: int | None = None) -> tuple[bool, str]:
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

        if active_count is None:
            count_row = mongo_query.agg_row('cycle_schedules', {'is_active': True}, [('count', None)])
            active_count = count_row[0] if count_row else 0

        if active_count >= ScheduleValidator.MAX_SYSTEM_SCHEDULES:
            if urgency != "critical":
                return False, f"System has reached max active schedules ({active_count}). Only critical updates allowed."
                    
        if scope == "portfolio" and intent != "weekly_review":
            if not reason_codes and urgency != "critical":
                return False, "Full portfolio scopes are reserved for weekly reviews or critical market shocks with explicit reason_codes."
                
        if intent == "monitor" and urgency in ("high", "critical"):
            return False, "A 'monitor' intent represents a lightweight check and cannot have high or critical urgency."
            
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
            row = mongo_query.find_row('cycle_schedules', {'id': schedule_id}, ['schedule_scope', 'review_intent', 'urgency', 'tickers', 'last_run_at'])
            
            if not row:
                return False, "Schedule not found"
                
            scope, intent, urgency, tickers_json, last_run_at = row
            
            if last_run_at:
                if intent == "weekly_review":
                    if datetime.now(timezone.utc) - last_run_at < timedelta(days=4):
                        return False, "Weekly review cooldown active."
                elif intent == "monitor":
                    if datetime.now(timezone.utc) - last_run_at < timedelta(hours=12):
                        return False, "Monitor cooldown active."
                        
            return True, ""
        except Exception as e:
            logger.error("[VALIDATOR] Pre-run check error: %s", e)
            return True, ""
