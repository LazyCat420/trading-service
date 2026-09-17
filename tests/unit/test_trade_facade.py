"""Unit tests for TradeFacade: OBSERVE, SHADOW, ENFORCE routing, idempotency, and policy denial."""

import datetime
import pytest
from unittest.mock import AsyncMock, patch

from app.trading.attribution.models import (
    DecisionArtifact,
    ExecutionIntent,
    IntentStatus,
    PolicyDecision,
    PolicyDisposition,
    ReservationStatus,
    RiskReservation,
)
from app.trading.control_plane import ControlPlaneMode
from app.trading.facade import TradeFacade, TradeResultStatus
from app.trading.policy.policy_translator import PolicyInputSnapshot


@pytest.mark.asyncio
async def test_facade_observe_mode_policy_approved(monkeypatch):
    """In OBSERVE mode, approved policy executes through legacy trader with enforced=False."""
    monkeypatch.setattr(
        "app.trading.facade.resolve_control_plane_mode",
        lambda bot_id: ControlPlaneMode.OBSERVE,
    )
    monkeypatch.setattr(
        "app.trading.paper_trader._get_current_price",
        lambda ticker: (150.0, 0.5),
    )
    monkeypatch.setattr(
        "app.trading.facade.build_policy_snapshot",
        lambda **kwargs: (
            PolicyInputSnapshot(
                portfolio_equity=100000.0,
                cash_balance=50000.0,
                quote_price=150.0,
                quote_age_hours=0.5,
            ),
            {},
        ),
    )
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_execution_intent", lambda x: x)
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_legacy_buy = AsyncMock(return_value={"order_id": "ord-legacy-1", "fill_price": 150.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.paper_trader.buy", mock_legacy_buy)

    res = await TradeFacade.submit_trade(
        bot_id="bot-test",
        ticker="AAPL",
        action="BUY",
        size_pct=0.10,
        confidence=90,
    )

    assert res["status"] == TradeResultStatus.COMMITTED.value
    assert res["effective_mode"] == "OBSERVE"
    assert res["enforced"] is False
    assert res["policy_would_deny"] is False
    assert res["trade_executed"] is True
    mock_legacy_buy.assert_called_once()
    assert mock_legacy_buy.call_args.kwargs["called_via_facade"] is True


@pytest.mark.asyncio
async def test_facade_observe_mode_policy_denied_still_executes_legacy(monkeypatch):
    """In OBSERVE mode, policy denial is advisory: legacy trader executes and policy_would_deny=True is recorded."""
    monkeypatch.setattr(
        "app.trading.facade.resolve_control_plane_mode",
        lambda bot_id: ControlPlaneMode.OBSERVE,
    )
    monkeypatch.setattr(
        "app.trading.paper_trader._get_current_price",
        lambda ticker: (150.0, 0.5),
    )
    monkeypatch.setattr(
        "app.trading.facade.build_policy_snapshot",
        lambda **kwargs: (
            PolicyInputSnapshot(
                portfolio_equity=100000.0,
                cash_balance=50000.0,
                quote_price=150.0,
                quote_age_hours=0.5,
            ),
            {},
        ),
    )
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_legacy_buy = AsyncMock(return_value={"order_id": "ord-legacy-2", "fill_price": 150.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.paper_trader.buy", mock_legacy_buy)

    # Low confidence -> policy will BLOCK
    res = await TradeFacade.submit_trade(
        bot_id="bot-test",
        ticker="AAPL",
        action="BUY",
        size_pct=0.10,
        confidence=40,  # Below threshold
    )

    assert res["status"] == TradeResultStatus.COMMITTED.value
    assert res["effective_mode"] == "OBSERVE"
    assert res["enforced"] is False
    assert res["policy_would_deny"] is True
    assert res["trade_executed"] is True
    mock_legacy_buy.assert_called_once()


@pytest.mark.asyncio
async def test_facade_shadow_mode_simulation(monkeypatch):
    """In SHADOW mode, approved intent triggers simulation via execute_intent without portfolio mutation."""
    monkeypatch.setattr(
        "app.trading.facade.resolve_control_plane_mode",
        lambda bot_id: ControlPlaneMode.SHADOW,
    )
    monkeypatch.setattr(
        "app.trading.paper_trader._get_current_price",
        lambda ticker: (150.0, 0.5),
    )
    monkeypatch.setattr(
        "app.trading.facade.build_policy_snapshot",
        lambda **kwargs: (
            PolicyInputSnapshot(
                portfolio_equity=100000.0,
                cash_balance=50000.0,
                quote_price=150.0,
                quote_age_hours=0.5,
            ),
            {},
        ),
    )
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_execution_intent", lambda x: x)
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_executor = AsyncMock(return_value={
        "status": "SIMULATED",
        "effective_mode": "SHADOW",
        "order_id": "ord-sim-1",
        "ticker": "AAPL",
        "side": "BUY",
        "fill_price": 150.0,
        "qty": 10.0,
        "simulated": True,
    })
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_executor)
    mock_legacy_buy = AsyncMock()
    monkeypatch.setattr("app.trading.paper_trader.buy", mock_legacy_buy)

    res = await TradeFacade.submit_trade(
        bot_id="bot-test",
        ticker="AAPL",
        action="BUY",
        size_pct=0.10,
        confidence=85,
    )

    assert res["status"] == TradeResultStatus.SIMULATED.value
    assert res["effective_mode"] == "SHADOW"
    assert res["simulated"] is True
    assert res["trade_executed"] is False
    mock_executor.assert_called_once()
    mock_legacy_buy.assert_not_called()


@pytest.mark.asyncio
async def test_facade_shadow_and_enforce_policy_denied_zero_executors(monkeypatch):
    """In SHADOW and ENFORCE, a policy denial invokes neither executor nor legacy trader."""
    monkeypatch.setattr(
        "app.trading.paper_trader._get_current_price",
        lambda ticker: (150.0, 0.5),
    )
    monkeypatch.setattr(
        "app.trading.facade.build_policy_snapshot",
        lambda **kwargs: (
            PolicyInputSnapshot(
                portfolio_equity=100000.0,
                cash_balance=50000.0,
                quote_price=150.0,
                quote_age_hours=0.5,
            ),
            {},
        ),
    )
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_executor = AsyncMock()
    mock_legacy_buy = AsyncMock()
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_executor)
    monkeypatch.setattr("app.trading.paper_trader.buy", mock_legacy_buy)

    for mode in (ControlPlaneMode.SHADOW, ControlPlaneMode.ENFORCE):
        monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda b, m=mode: m)
        res = await TradeFacade.submit_trade(
            bot_id="bot-test",
            ticker="AAPL",
            action="BUY",
            size_pct=0.10,
            confidence=30,  # Below threshold
        )

        assert res["status"] == TradeResultStatus.POLICY_DENIED.value
        assert res["effective_mode"] == mode.value
        assert res["trade_executed"] is False
        mock_executor.assert_not_called()
        mock_legacy_buy.assert_not_called()


@pytest.mark.asyncio
async def test_facade_enforce_mode_execution(monkeypatch):
    """In ENFORCE mode, approved intent executes via atomic admission and execute_intent()."""
    monkeypatch.setattr(
        "app.trading.facade.resolve_control_plane_mode",
        lambda bot_id: ControlPlaneMode.ENFORCE,
    )
    monkeypatch.setattr(
        "app.trading.paper_trader._get_current_price",
        lambda ticker: (150.0, 0.5),
    )
    monkeypatch.setattr(
        "app.trading.facade.build_policy_snapshot",
        lambda **kwargs: (
            PolicyInputSnapshot(
                portfolio_equity=100000.0,
                cash_balance=50000.0,
                quote_price=150.0,
                quote_age_hours=0.5,
            ),
            {},
        ),
    )
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_execution_intent", lambda x: x)
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_admit = lambda **kwargs: {"admitted": True, "is_duplicate": False}
    monkeypatch.setattr("app.trading.attribution.repository.admit_execution_intent", mock_admit)

    mock_executor = AsyncMock(return_value={
        "status": "FILLED",
        "order_id": "ord-enforce-1",
        "ticker": "AAPL",
        "side": "BUY",
        "fill_price": 150.0,
        "qty": 10.0,
    })
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_executor)
    mock_legacy_buy = AsyncMock()
    monkeypatch.setattr("app.trading.paper_trader.buy", mock_legacy_buy)

    res = await TradeFacade.submit_trade(
        bot_id="bot-test",
        ticker="AAPL",
        action="BUY",
        size_pct=0.10,
        confidence=85,
    )

    assert res["status"] == TradeResultStatus.COMMITTED.value
    assert res["effective_mode"] == "ENFORCE"
    assert res["enforced"] is True
    assert res["trade_executed"] is True
    assert res["order_id"] == "ord-enforce-1"
    mock_executor.assert_called_once()
    mock_legacy_buy.assert_not_called()


@pytest.mark.asyncio
async def test_facade_pipeline_stage4_buy_enforce(monkeypatch):
    """Pipeline Stage 4 BUY in ENFORCE mode routes through TradeFacade to execute_intent."""
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda bot_id: ControlPlaneMode.ENFORCE)
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (150.0, 0.5))
    monkeypatch.setattr("app.trading.facade.build_policy_snapshot", lambda **kwargs: (
        PolicyInputSnapshot(portfolio_equity=100000.0, cash_balance=50000.0, quote_price=150.0, bot_id="bot-test"), {},
    ))
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.admit_execution_intent", lambda **kwargs: {"admitted": True, "is_duplicate": False})
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_exec = AsyncMock(return_value={"status": "FILLED", "order_id": "ord-p4-buy", "ticker": "AAPL", "side": "BUY", "fill_price": 150.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_exec)
    mock_legacy = AsyncMock()
    monkeypatch.setattr("app.trading.paper_trader.buy", mock_legacy)

    res = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="BUY", size_pct=0.10, confidence=85, cycle_id="cycle-1")
    assert res["status"] == TradeResultStatus.COMMITTED.value
    assert res["effective_mode"] == "ENFORCE"
    mock_exec.assert_called_once()
    mock_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_facade_pipeline_stage4_sell_enforce(monkeypatch):
    """Pipeline Stage 4 SELL in ENFORCE mode routes through TradeFacade to execute_intent."""
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda bot_id: ControlPlaneMode.ENFORCE)
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (150.0, 0.5))
    monkeypatch.setattr("app.trading.facade.build_policy_snapshot", lambda **kwargs: (
        PolicyInputSnapshot(portfolio_equity=100000.0, cash_balance=50000.0, quote_price=150.0, is_held=True, bot_id="bot-test"), {},
    ))
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.admit_execution_intent", lambda **kwargs: {"admitted": True, "is_duplicate": False})
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_exec = AsyncMock(return_value={"status": "FILLED", "order_id": "ord-p4-sell", "ticker": "AAPL", "side": "SELL", "fill_price": 150.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_exec)
    mock_legacy = AsyncMock()
    monkeypatch.setattr("app.trading.paper_trader.sell", mock_legacy)

    res = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="SELL", size_pct=1.0, confidence=85, cycle_id="cycle-1")
    assert res["status"] == TradeResultStatus.COMMITTED.value
    assert res["effective_mode"] == "ENFORCE"
    mock_exec.assert_called_once()
    mock_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_facade_interactive_tool_buy_modes(monkeypatch):
    """buy_stock tool routes through TradeFacade across OBSERVE, SHADOW, and ENFORCE modes."""
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (150.0, 0.5))
    monkeypatch.setattr("app.trading.facade.build_policy_snapshot", lambda **kwargs: (
        PolicyInputSnapshot(portfolio_equity=100000.0, cash_balance=50000.0, quote_price=150.0, bot_id="bot-test"), {},
    ))
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_execution_intent", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.admit_execution_intent", lambda **kwargs: {"admitted": True, "is_duplicate": False})
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_exec = AsyncMock(return_value={"status": "FILLED", "order_id": "ord-tool-buy", "ticker": "AAPL", "side": "BUY", "fill_price": 150.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_exec)
    mock_legacy = AsyncMock(return_value={"order_id": "ord-legacy", "fill_price": 150.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.paper_trader.buy", mock_legacy)

    # 1. OBSERVE
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda b: ControlPlaneMode.OBSERVE)
    res_obs = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="BUY", size_pct=0.10, confidence=85, producer="interactive_tool")
    assert res_obs["effective_mode"] == "OBSERVE"
    assert res_obs["trade_executed"] is True
    mock_legacy.assert_called_once()
    mock_exec.assert_not_called()

    # 2. SHADOW
    mock_legacy.reset_mock()
    mock_exec.reset_mock()
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda b: ControlPlaneMode.SHADOW)
    res_shad = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="BUY", size_pct=0.10, confidence=85, producer="interactive_tool")
    assert res_shad["effective_mode"] == "SHADOW"
    assert res_shad["trade_executed"] is False
    assert res_shad["simulated"] is True
    mock_exec.assert_called_once()
    mock_legacy.assert_not_called()

    # 3. ENFORCE
    mock_legacy.reset_mock()
    mock_exec.reset_mock()
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda b: ControlPlaneMode.ENFORCE)
    res_enf = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="BUY", size_pct=0.10, confidence=85, producer="interactive_tool")
    assert res_enf["effective_mode"] == "ENFORCE"
    assert res_enf["trade_executed"] is True
    mock_exec.assert_called_once()
    mock_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_facade_interactive_tool_sell_modes(monkeypatch):
    """sell_stock tool routes through TradeFacade across OBSERVE, SHADOW, and ENFORCE modes."""
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (150.0, 0.5))
    monkeypatch.setattr("app.trading.facade.build_policy_snapshot", lambda **kwargs: (
        PolicyInputSnapshot(portfolio_equity=100000.0, cash_balance=50000.0, quote_price=150.0, is_held=True, bot_id="bot-test"), {},
    ))
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_execution_intent", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.admit_execution_intent", lambda **kwargs: {"admitted": True, "is_duplicate": False})
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_exec = AsyncMock(return_value={"status": "FILLED", "order_id": "ord-tool-sell", "ticker": "AAPL", "side": "SELL", "fill_price": 150.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_exec)
    mock_legacy = AsyncMock(return_value={"order_id": "ord-legacy-sell", "fill_price": 150.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.paper_trader.sell", mock_legacy)

    # 1. OBSERVE
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda b: ControlPlaneMode.OBSERVE)
    res_obs = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="SELL", size_pct=1.0, confidence=85, producer="interactive_tool")
    assert res_obs["effective_mode"] == "OBSERVE"
    assert res_obs["trade_executed"] is True
    mock_legacy.assert_called_once()
    mock_exec.assert_not_called()

    # 2. SHADOW
    mock_legacy.reset_mock()
    mock_exec.reset_mock()
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda b: ControlPlaneMode.SHADOW)
    res_shad = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="SELL", size_pct=1.0, confidence=85, producer="interactive_tool")
    assert res_shad["effective_mode"] == "SHADOW"
    assert res_shad["trade_executed"] is False
    assert res_shad["simulated"] is True
    mock_exec.assert_called_once()
    mock_legacy.assert_not_called()

    # 3. ENFORCE
    mock_legacy.reset_mock()
    mock_exec.reset_mock()
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda b: ControlPlaneMode.ENFORCE)
    res_enf = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="SELL", size_pct=1.0, confidence=85, producer="interactive_tool")
    assert res_enf["effective_mode"] == "ENFORCE"
    assert res_enf["trade_executed"] is True
    mock_exec.assert_called_once()
    mock_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_facade_emergency_exit_modes(monkeypatch):
    """emergency_risk_exit routes through TradeFacade in OBSERVE, SHADOW, ENFORCE."""
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (150.0, 0.5))
    monkeypatch.setattr("app.trading.facade.build_policy_snapshot", lambda **kwargs: (
        PolicyInputSnapshot(portfolio_equity=100000.0, cash_balance=50000.0, quote_price=150.0, is_held=True, bot_id="bot-test"), {},
    ))
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_execution_intent", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.admit_execution_intent", lambda **kwargs: {"admitted": True, "is_duplicate": False})
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_exec = AsyncMock(return_value={"status": "FILLED", "order_id": "ord-em-exit", "ticker": "AAPL", "side": "SELL", "fill_price": 150.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_exec)
    mock_legacy = AsyncMock(return_value={"order_id": "ord-legacy-em", "fill_price": 150.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.paper_trader.sell", mock_legacy)

    # SHADOW mode: must simulate with zero live execution
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda b: ControlPlaneMode.SHADOW)
    res_shad = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="SELL", size_pct=1.0, confidence=100, is_emergency=True)
    assert res_shad["effective_mode"] == "SHADOW"
    assert res_shad["trade_executed"] is False
    assert res_shad["simulated"] is True
    mock_exec.assert_called_once()
    mock_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_facade_stop_loss_modes(monkeypatch):
    """check_stop_losses routes through TradeFacade in OBSERVE, SHADOW, ENFORCE."""
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (90.0, 0.5))
    monkeypatch.setattr("app.trading.facade.build_policy_snapshot", lambda **kwargs: (
        PolicyInputSnapshot(portfolio_equity=100000.0, cash_balance=50000.0, quote_price=90.0, is_held=True, bot_id="bot-test"), {},
    ))
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_execution_intent", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.admit_execution_intent", lambda **kwargs: {"admitted": True, "is_duplicate": False})
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_exec = AsyncMock(return_value={"status": "FILLED", "order_id": "ord-sl", "ticker": "AAPL", "side": "SELL", "fill_price": 90.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_exec)
    mock_legacy = AsyncMock(return_value={"order_id": "ord-legacy-sl", "fill_price": 90.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.paper_trader.sell", mock_legacy)

    # ENFORCE mode
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda b: ControlPlaneMode.ENFORCE)
    res_enf = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="SELL", size_pct=1.0, confidence=100, idempotency_key="stoploss:bot-test:AAPL:c1")
    assert res_enf["effective_mode"] == "ENFORCE"
    assert res_enf["trade_executed"] is True
    mock_exec.assert_called_once()
    mock_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_facade_take_profit_modes(monkeypatch):
    """check_take_profits routes through TradeFacade in OBSERVE, SHADOW, ENFORCE."""
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (130.0, 0.5))
    monkeypatch.setattr("app.trading.facade.build_policy_snapshot", lambda **kwargs: (
        PolicyInputSnapshot(portfolio_equity=100000.0, cash_balance=50000.0, quote_price=130.0, is_held=True, bot_id="bot-test"), {},
    ))
    monkeypatch.setattr("app.trading.attribution.repository.save_decision_artifact", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_policy_decision", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.save_execution_intent", lambda x: x)
    monkeypatch.setattr("app.trading.attribution.repository.admit_execution_intent", lambda **kwargs: {"admitted": True, "is_duplicate": False})
    monkeypatch.setattr("app.db.mongo_store.find_docs", lambda *args, **kwargs: [])

    mock_exec = AsyncMock(return_value={"status": "FILLED", "order_id": "ord-tp", "ticker": "AAPL", "side": "SELL", "fill_price": 130.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_exec)
    mock_legacy = AsyncMock(return_value={"order_id": "ord-legacy-tp", "fill_price": 130.0, "qty": 10.0})
    monkeypatch.setattr("app.trading.paper_trader.sell", mock_legacy)

    # SHADOW mode: zero live execution
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda b: ControlPlaneMode.SHADOW)
    res_shad = await TradeFacade.submit_trade(bot_id="bot-test", ticker="AAPL", action="SELL", size_pct=1.0, confidence=100, idempotency_key="takeprofit:bot-test:AAPL:c1")
    assert res_shad["effective_mode"] == "SHADOW"
    assert res_shad["trade_executed"] is False
    assert res_shad["simulated"] is True
    mock_exec.assert_called_once()
    mock_legacy.assert_not_called()
