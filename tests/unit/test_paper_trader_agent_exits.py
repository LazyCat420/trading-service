"""Agent-owned exits — the agent's stop/target survive to the position
(band-clamped), ATR is only the fallback, and 'reanalyze_on_breach' positions
are left alone by the background hard-sell monitor (dual-stop fix)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.trading import paper_trader as pt


def _run(coro):
    return asyncio.run(coro)


# ── _resolve_entry_exits ────────────────────────────────────────────────────

def test_agent_stop_price_becomes_position_stop(monkeypatch):
    monkeypatch.setattr(pt, "_classify_asset", lambda t: "stock")
    stop_pct, source, tp = pt._resolve_entry_exits("AAPL", 100.0, stop_loss_price=92.0)
    assert stop_pct == 0.08
    assert source == "agent"


def test_agent_stop_clamped_into_asset_band(monkeypatch):
    monkeypatch.setattr(pt, "_classify_asset", lambda t: "stock")
    # Stop at $50 on a $100 entry = 50% — clamped to the stock max (12%).
    stop_pct, source, _ = pt._resolve_entry_exits("AAPL", 100.0, stop_loss_price=50.0)
    assert stop_pct == 0.12
    assert source == "agent"
    # Absurdly tight stop clamps to the stock min (4%).
    stop_pct, source, _ = pt._resolve_entry_exits("AAPL", 100.0, stop_loss_price=99.5)
    assert stop_pct == 0.04
    assert source == "agent"


def test_no_agent_stop_falls_back_to_atr(monkeypatch):
    monkeypatch.setattr(pt, "_compute_stop_loss_pct", lambda t, p: 0.0777)
    stop_pct, source, _ = pt._resolve_entry_exits("AAPL", 100.0, stop_loss_price=None)
    assert stop_pct == 0.0777
    assert source == "atr_fallback"


def test_invalid_agent_stop_falls_back_to_atr(monkeypatch):
    monkeypatch.setattr(pt, "_compute_stop_loss_pct", lambda t, p: 0.08)
    # Stop above entry is nonsense for a long — fallback, not honored.
    _, source, _ = pt._resolve_entry_exits("AAPL", 100.0, stop_loss_price=110.0)
    assert source == "atr_fallback"
    _, source, _ = pt._resolve_entry_exits("AAPL", 100.0, stop_loss_price=True)
    assert source == "atr_fallback"


def test_take_profit_only_above_entry(monkeypatch):
    monkeypatch.setattr(pt, "_compute_stop_loss_pct", lambda t, p: 0.08)
    _, _, tp = pt._resolve_entry_exits("AAPL", 100.0, take_profit_price=120.0)
    assert tp == 0.20
    _, _, tp = pt._resolve_entry_exits("AAPL", 100.0, take_profit_price=90.0)
    assert tp is None


def test_normalize_exit_style():
    assert pt.normalize_exit_style(None) == "hard_stop"
    assert pt.normalize_exit_style("hard_stop") == "hard_stop"
    assert pt.normalize_exit_style("REANALYZE_ON_BREACH") == "reanalyze_on_breach"
    assert pt.normalize_exit_style("yolo") == "hard_stop"


# ── background monitor honors exit_style ────────────────────────────────────

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return _FakeResult(self._rows)


def _fake_get_db(rows):
    class _Ctx:
        def __enter__(self):
            return _FakeDB(rows)

        def __exit__(self, *a):
            return False

    return lambda: _Ctx()


def test_check_stop_losses_skips_reanalyze_positions(monkeypatch):
    sells = []

    async def _fake_sell(bot_id, ticker, current_price=None, cycle_id=None, qty_pct=1.0):
        sells.append(ticker)
        return {"action": "SELL", "ticker": ticker}

    # Two breached positions: one hard_stop, one reanalyze_on_breach.
    rows = [
        ("id1", "HARD", 10.0, 100.0, 0.08, "hard_stop"),
        ("id2", "SOFT", 10.0, 100.0, 0.08, "reanalyze_on_breach"),
    ]
    monkeypatch.setattr(pt, "_ensure_bot", lambda b: None)
    monkeypatch.setattr(pt, "get_db", _fake_get_db(rows))
    monkeypatch.setattr(pt, "_get_current_price", lambda t: (80.0, 1.0))  # deep breach
    monkeypatch.setattr(pt, "sell", _fake_sell)
    monkeypatch.setattr(pt, "record_fund_alert", lambda **kw: None)

    triggered = _run(pt.check_stop_losses("bot-x"))
    assert sells == ["HARD"]
    assert len(triggered) == 1


def test_check_take_profits_uses_agent_target_and_skips_reanalyze(monkeypatch):
    sells = []

    async def _fake_sell(bot_id, ticker, current_price=None, cycle_id=None, qty_pct=1.0):
        sells.append(ticker)
        return {"action": "SELL", "ticker": ticker}

    # AGENT_TP: stored take_profit_pct 5% — price +6% must harvest even though
    # the R:R-derived target (8% stop * 2.0 = 16%) would NOT have fired.
    # RR_FALLBACK: no stored tp, +6% is below the 16% R:R target → no harvest.
    # SOFT: agent target hit but reanalyze style → monitor leaves it alone.
    rows = [
        ("id1", "AGENT_TP", 10.0, 100.0, 0.08, 0.05, "hard_stop"),
        ("id2", "RR_FALLBACK", 10.0, 100.0, 0.08, None, "hard_stop"),
        ("id3", "SOFT", 10.0, 100.0, 0.08, 0.05, "reanalyze_on_breach"),
    ]
    monkeypatch.setattr(pt, "_ensure_bot", lambda b: None)
    monkeypatch.setattr(pt, "get_db", _fake_get_db(rows))
    monkeypatch.setattr(pt, "_get_current_price", lambda t: (106.0, 1.0))
    monkeypatch.setattr(pt, "sell", _fake_sell)

    triggered = _run(pt.check_take_profits("bot-x", reward_risk_ratio=2.0))
    assert sells == ["AGENT_TP"]
    assert len(triggered) == 1
