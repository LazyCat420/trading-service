"""Read pending paper orders and reject unpriceable reservations."""
from app.v3.financial_evidence import number


def pending_capacity(bot_id, ticker):
    from app.db import mongo_store
    # Filled paper orders are recorded with filled_at and are not reservations.
    rows = mongo_store.find_docs('orders', {'bot_id': bot_id, 'side': 'BUY', 'filled_at': None,
        'status': {'$nin': ['cancelled', 'canceled', 'rejected', 'expired', 'filled']}})
    total = same_name = 0.0
    for row in rows:
        amount = number(row.get('remaining_notional'))
        if amount is None:
            qty, filled, price = (number(row.get(k)) for k in ('qty', 'filled_qty', 'price'))
            if filled is None and row.get('status') not in ('partially_filled', 'partial'):
                filled = number(0)
            if qty is None or filled is None or price is None or qty < filled or filled < 0 or price <= 0:
                raise ValueError('Pending order has unknown remaining capacity.')
            amount = (qty - filled) * price
        if amount < 0 or not row.get('ticker'):
            raise ValueError('Invalid pending-order reservation.')
        total += float(amount)
        if row['ticker'].upper() == ticker.upper():
            same_name += float(amount)
    return {'cash_reserved': total, 'ticker_reserved': same_name}


def strict_capacity_error(*, equity, cash, held_value, requested_fraction, concentration_fraction,
                          order_fraction, reservations):
    values = [number(v) for v in (equity, cash, held_value, requested_fraction,
                                  concentration_fraction, order_fraction)]
    if any(v is None for v in values):
        return 'Current purchase capacity is unavailable.'
    equity, cash, held, size, concentration, order_limit = values
    if equity <= 0 or cash < 0 or held < 0 or size <= 0 or concentration < 0 or order_limit < 0:
        return 'Current purchase capacity or requested size is invalid.'
    reserved_cash = number(reservations.get('cash_reserved'))
    reserved_name = number(reservations.get('ticker_reserved'))
    if reserved_cash is None or reserved_name is None or min(reserved_cash, reserved_name) < 0:
        return 'Pending-order capacity is unavailable.'
    amount = equity * size
    if size > order_limit or amount > cash - reserved_cash or amount + held + reserved_name > equity * concentration:
        return 'Requested purchase exceeds current capacity including pending orders; re-analysis required.'
    return None
