"""
Risk Manager - Deterministic Lego for enforcing portfolio constraints.
"""
import logging

logger = logging.getLogger(__name__)

def check_portfolio_constraints(portfolio: dict, requested_action: str) -> tuple[bool, str]:
    """
    Check if a requested action ('BUY' or 'SELL') is allowed given the current portfolio state.
    
    Constraints:
    - Max 8 open positions total.
    - Cannot buy if cash is exhausted (or near zero).
    """
    MAX_POSITIONS = 8
    MIN_CASH_THRESHOLD = 100.0 # Don't buy if less than $100 cash remaining
    
    cash = portfolio.get("cash", 0.0)
    # Count positions that have actual quantity > 0
    positions = portfolio.get("positions", [])
    active_positions = len([p for p in positions if p.get("qty", 0) > 0])
    
    if requested_action == "SELL":
        return True, "SELL allowed"
        
    if requested_action == "BUY":
        if active_positions >= MAX_POSITIONS:
            return False, f"VETO: Portfolio already at max capacity ({MAX_POSITIONS} positions)."
        if cash < MIN_CASH_THRESHOLD:
            return False, f"VETO: Insufficient cash to open new position (${cash:,.2f} remaining)."
        return True, "BUY allowed"
        
    return False, f"Unknown action: {requested_action}"
