"""
Position Sizer - Deterministic Lego for calculating exact trade allocations.
"""

def calculate_buy_size(confidence: int, available_cash: float, current_price: float, risk_factor: float = 1.0) -> dict:
    """
    Calculate the amount to allocate for a BUY signal based on confidence and available cash.
    Scales from 2% to 15% of total portfolio value/cash based on confidence (70-100).
    """
    MIN_SIZE_PCT = 0.02
    MAX_SIZE_PCT = 0.15
    MIN_CONF = 70.0
    MAX_CONF = 100.0

    if confidence < MIN_CONF:
        size_pct = 0.0
    elif confidence >= MAX_CONF:
        size_pct = MAX_SIZE_PCT
    else:
        # Linear interpolation
        size_pct = MIN_SIZE_PCT + ((confidence - MIN_CONF) / (MAX_CONF - MIN_CONF)) * (MAX_SIZE_PCT - MIN_SIZE_PCT)

    # Apply risk factor (e.g., if market is highly volatile, risk_factor < 1.0)
    size_pct *= risk_factor

    # Allocation amount
    amount = available_cash * size_pct
    
    # Cap amount to available cash (safety)
    amount = min(amount, available_cash)
    
    qty = amount / current_price if current_price > 0 else 0

    return {
        "size_pct": round(size_pct * 100, 2),
        "amount": round(amount, 2),
        "qty": round(qty, 2)
    }

def calculate_sell_size(confidence: int, current_qty: float) -> dict:
    """
    Calculate the amount to sell. Usually 100% of the position.
    """
    return {
        "size_pct": 100.0,
        "amount": 0.0, # Not used for sell qty
        "qty": current_qty
    }

def estimate_trade(confidence: int, cash: float, current_price: float) -> dict:
    """Estimate shares/$ for a BUY signal without executing.

    Returns: {"size_pct": 7.3, "amount": 7300, "qty": 52, "price": 140.38}
    """
    res = calculate_buy_size(confidence, cash, current_price)
    return {
        "size_pct": res["size_pct"],
        "amount": res["amount"],
        "qty": res["qty"],
        "price": round(current_price, 2),
    }

