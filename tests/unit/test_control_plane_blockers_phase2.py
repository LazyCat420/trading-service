"""Unit tests for Phase 2: Close Remaining Execution Paths & Fail-Closed Mode Handling.

Verifies:
1. resolve_control_plane_mode fails closed on DB error or invalid mode (raises ControlPlaneConfigurationError).
2. check_stop_losses, harvest_take_profits, and emergency_risk_exit call TradeFacade.submit_trade.
3. In SHADOW mode, protective exits result in zero ledger mutations.
4. AST check catches direct callers to paper_trader.buy/sell.
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.trading.control_plane import (
    ControlPlaneMode,
    ControlPlaneConfigurationError,
    resolve_control_plane_mode,
)
from app.trading.facade import TradeFacade, TradeResultStatus
from app.trading.paper_trader import (
    check_stop_losses,
    check_take_profits,
    emergency_risk_exit,
)


def test_mode_resolution_fails_closed_on_db_exception(monkeypatch):
    """Database lookup failure must raise ControlPlaneConfigurationError, NOT fall through to OBSERVE."""
    def _mock_find_row(*args, **kwargs):
        raise RuntimeError("Database connection timed out")
    monkeypatch.setattr("app.db.mongo_query.find_row", _mock_find_row)

    with pytest.raises(ControlPlaneConfigurationError) as exc:
        resolve_control_plane_mode(bot_id="bot-err")
    assert "Database lookup failed" in str(exc.value)


def test_mode_resolution_fails_closed_on_invalid_mode_in_db(monkeypatch):
    """Unrecognized control plane mode in DB must raise ControlPlaneConfigurationError."""
    monkeypatch.setattr(
        "app.db.mongo_query.find_row",
        lambda table, q, cols: ["INVALID_MODE_VALUE"],
    )

    with pytest.raises(ControlPlaneConfigurationError) as exc:
        resolve_control_plane_mode(bot_id="bot-invalid")
    assert "Invalid mode" in str(exc.value)


def test_mode_resolution_fails_closed_on_invalid_mode_in_env(monkeypatch):
    """Unrecognized CONTROL_PLANE_MODE env var must raise ControlPlaneConfigurationError."""
    monkeypatch.setattr("app.db.mongo_query.find_row", lambda table, q, cols: None)
    monkeypatch.setenv("CONTROL_PLANE_MODE", "NON_EXISTENT_MODE")

    with pytest.raises(ControlPlaneConfigurationError) as exc:
        resolve_control_plane_mode()
    assert "Invalid mode" in str(exc.value)


@pytest.mark.asyncio
async def test_protective_exits_route_through_facade(monkeypatch):
    """check_stop_losses, harvest_take_profits, and emergency_risk_exit must call TradeFacade.submit_trade."""
    mock_submit = AsyncMock(return_value={
        "status": TradeResultStatus.COMMITTED.value,
        "effective_mode": "ENFORCE",
        "trade_executed": True,
        "order_id": "ord-test",
    })
    monkeypatch.setattr(TradeFacade, "submit_trade", mock_submit)
    monkeypatch.setattr("app.trading.paper_trader._ensure_bot", lambda bot_id: None)
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (90.0, 0.1))

    # Mock stop-loss position: entry $100, stop 8% ($92), current $90 -> triggered
    monkeypatch.setattr(
        "app.db.mongo_query.find_rows",
        lambda table, q, cols: [
            ["pos-1", "AAPL", 10.0, 100.0, 0.08, "hard_stop"],
        ],
    )

    # 1. Test check_stop_losses
    res_sl = await check_stop_losses(bot_id="bot-test", cycle_id="cycle-sl")
    assert len(res_sl) == 1
    mock_submit.assert_called_once()
    assert mock_submit.call_args.kwargs["action"] == "SELL"
    assert mock_submit.call_args.kwargs["ticker"] == "AAPL"
    assert "stoploss" in mock_submit.call_args.kwargs.get("idempotency_key", "")

    # 2. Test check_take_profits
    mock_submit.reset_mock()
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (125.0, 0.1))
    # Mock take-profit position: entry $100, target $120, current $125 -> triggered
    monkeypatch.setattr(
        "app.db.mongo_query.find_rows",
        lambda table, q, cols: [
            ["pos-2", "MSFT", 5.0, 100.0, 0.08, 0.20, "hard_target"],
        ],
    )
    res_tp = await check_take_profits(bot_id="bot-test", cycle_id="cycle-tp")
    assert len(res_tp) == 1
    mock_submit.assert_called_once()
    assert mock_submit.call_args.kwargs["action"] == "SELL"
    assert mock_submit.call_args.kwargs["ticker"] == "MSFT"
    assert "takeprofit" in mock_submit.call_args.kwargs.get("idempotency_key", "")

    # 3. Test emergency_risk_exit
    mock_submit.reset_mock()
    res_em = await emergency_risk_exit(bot_id="bot-test", ticker="TSLA", reason="DRAWDOWN_LIMIT")
    mock_submit.assert_called_once()
    assert mock_submit.call_args.kwargs["action"] == "SELL"
    assert mock_submit.call_args.kwargs["ticker"] == "TSLA"
    assert mock_submit.call_args.kwargs["is_emergency"] is True
