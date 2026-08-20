"""The background risk sweep must survive money arriving as Decimal.

`positions.avg_entry_price` is classified as money, so `mongo_query.find_rows`
returns it as `Decimal`. `positions.stop_loss_pct` is deliberately NOT money
(it is a ratio), so it stays `float`. `Decimal * float` raises TypeError.

`check_stop_losses` multiplied the two directly and therefore raised on its
FIRST position, every pass, before reaching a single stop. Measured in
production in the 40 minutes after the 2026-08-19 Mongo cutover deploy: 97 ×

    [SCHEDULER] Background risk sweep failed for bot 'cycle-backend':
    unsupported operand type(s) for *: 'decimal.Decimal' and 'float'

`check_take_profits` already promoted the float side and kept working — so the
harvest ran while the protective downside stop did not. That asymmetry is the
reason this file exists: a passing take-profit test said nothing about the stop.
"""
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.trading import paper_trader as pt


def _run(coro):
    return asyncio.run(coro)


def _wire(monkeypatch, rows, price):
    """Positions come back money-shaped, exactly as Mongo returns them."""
    monkeypatch.setattr(pt, "_ensure_bot", lambda b: None)
    monkeypatch.setattr(pt.mongo_query, "find_rows", lambda *a, **k: rows)
    monkeypatch.setattr(pt, "_get_current_price", lambda t: (price, 0.5))
    monkeypatch.setattr(pt, "normalize_exit_style", lambda s: s or "hard_stop")
    sold = []

    async def _sell(bot_id, ticker, current_price=None, cycle_id=None):
        sold.append((ticker, current_price))
        return {"realized_pnl": -100.0}

    monkeypatch.setattr(pt, "sell", _sell)
    monkeypatch.setattr(pt, "record_fund_alert", lambda **k: None)
    return sold


# id, ticker, qty, avg_entry_price (MONEY -> Decimal), stop_loss_pct (float), exit_style
def _pos(entry="100.00", stop=0.08):
    return [(1, "AAPL", 10, Decimal(entry), stop, "hard_stop")]


def test_stop_loss_survives_decimal_entry_and_float_ratio(monkeypatch):
    """THE REGRESSION: this raised TypeError before the fix."""
    sold = _wire(monkeypatch, _pos(), price=80.0)   # 80 < 100*(1-0.08)=92 -> fire
    out = _run(pt.check_stop_losses("cycle-backend"))
    assert sold == [("AAPL", 80.0)], "the stop did not fire"
    assert len(out) == 1


def test_stop_loss_does_not_fire_above_the_stop(monkeypatch):
    """Not-firing must be a decision, not an exception swallowed upstream."""
    sold = _wire(monkeypatch, _pos(), price=95.0)   # 95 > 92 -> hold
    out = _run(pt.check_stop_losses("cycle-backend"))
    assert sold == []
    assert out == []


def test_the_stop_boundary_is_exact_at_the_cent(monkeypatch):
    """Promoting the float (not demoting the Decimal) is what keeps this exact:
    100.00 * (1 - 0.08) is 92.00, and a position at exactly 92.00 stops."""
    sold = _wire(monkeypatch, _pos(), price=92.0)
    _run(pt.check_stop_losses("cycle-backend"))
    assert sold == [("AAPL", 92.0)], "a price exactly at the stop must trigger"


def test_default_stop_applies_when_the_column_is_null(monkeypatch):
    """stop_loss_pct NULL -> default_stop_pct, still a float meeting Decimal."""
    rows = [(1, "AAPL", 10, Decimal("100.00"), None, "hard_stop")]
    sold = _wire(monkeypatch, rows, price=80.0)     # default 0.08 -> stop 92
    _run(pt.check_stop_losses("cycle-backend"))
    assert sold == [("AAPL", 80.0)]


def test_reanalyze_positions_are_still_left_alone(monkeypatch):
    """The dual-stop rule must survive the fix."""
    rows = [(1, "AAPL", 10, Decimal("100.00"), 0.08, "reanalyze_on_breach")]
    monkeypatch.setattr(pt, "normalize_exit_style", lambda s: s)
    sold = _wire(monkeypatch, rows, price=10.0)
    monkeypatch.setattr(pt, "normalize_exit_style", lambda s: s)
    out = _run(pt.check_stop_losses("cycle-backend"))
    assert sold == [] and out == []


def test_take_profit_shares_the_boundary(monkeypatch):
    """The path that already worked must keep working — it is the control that
    proves the two are now handled the same way."""
    rows = [(1, "AAPL", 10, Decimal("100.00"), 0.08, 0.20, "hard_stop")]
    sold = _wire(monkeypatch, rows, price=130.0)    # 130 > 100*1.20 = 120
    monkeypatch.setattr(pt, "get_param", lambda k: 2.0)
    out = _run(pt.check_take_profits("cycle-backend"))
    assert sold == [("AAPL", 130.0)]
    assert len(out) == 1
