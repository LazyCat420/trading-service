"""Tests for the 2026-07-24 bot_id resolution fix.

Three sources of truth disagreed and the wrong one was winning. `run_v3_pipeline`
takes `bot_id: str = ""` and its only caller never passed it, so every desk
stored bot_id='' and downstream callers fell back to `settings.BOT_ID`
('lazy-trader-v4' — **zero positions**) instead of the genuinely active bot
('test_bot' — 9 open positions).

Three things broke silently:
  * every ticker resolved to held=False, including ones the desk really owned;
  * agents were told "NO OPEN POSITION" about their own book;
  * the HRP branch needs >=2 tickers in the portfolio, saw an empty book, and
    skipped — so HRP sizing had NEVER been computed in production, which is why
    `hrp_weight_suggestion` was null in 516 of 568 reports.
"""

import pytest

from app.tools import portfolio_tools
from app.tools.portfolio_tools import resolve_bot_id


class TestResolveBotId:
    def test_explicit_bot_id_always_wins(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.bot_manager.get_active_bot_id", lambda: "active-bot"
        )
        assert resolve_bot_id("explicit-bot") == "explicit-bot"

    def test_empty_resolves_to_the_ACTIVE_bot_not_settings(self, monkeypatch):
        """The whole bug: settings.BOT_ID pointed at a bot with no positions."""
        monkeypatch.setattr(
            "app.services.bot_manager.get_active_bot_id", lambda: "test_bot"
        )
        monkeypatch.setattr(portfolio_tools.settings, "BOT_ID", "lazy-trader-v4")
        assert resolve_bot_id("") == "test_bot"

    def test_falls_back_to_settings_when_lookup_fails(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr("app.services.bot_manager.get_active_bot_id", boom)
        monkeypatch.setattr(portfolio_tools.settings, "BOT_ID", "fallback-bot")
        assert resolve_bot_id("") == "fallback-bot"

    def test_empty_active_bot_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.bot_manager.get_active_bot_id", lambda: ""
        )
        monkeypatch.setattr(portfolio_tools.settings, "BOT_ID", "fallback-bot")
        assert resolve_bot_id("") == "fallback-bot"


class TestHrpRequiresARealPortfolio:
    """HRP needs at least the candidate plus one holding. With an empty book it
    silently produces nothing — which is exactly how it stayed broken for so
    long: GARCH kept working, so the block looked healthy."""

    def test_empty_portfolio_yields_no_hrp_line(self, monkeypatch):
        import app.quant.context_block as cb

        monkeypatch.setattr(
            "app.tools.portfolio_tools._current_holdings",
            lambda bot_id: ({}, 100000.0, 100000.0),
        )
        block = cb.build_quant_math_block("NVDA", "some-bot")
        # No HRP *calculation* — the sizing bracket's static caveat mentions
        # the word, so assert on the computed line rather than the substring.
        assert "HRP covariance-aware target weight" not in block
        assert "HRP ceiling" not in block

    def test_block_never_raises_on_a_broken_portfolio(self, monkeypatch):
        """Precompute is fail-open: a portfolio error must degrade the block,
        never abort the cycle."""
        import app.quant.context_block as cb

        def boom(bot_id):
            raise RuntimeError("portfolio unavailable")

        monkeypatch.setattr("app.tools.portfolio_tools._current_holdings", boom)
        block = cb.build_quant_math_block("NVDA", "some-bot")
        assert isinstance(block, str)


class TestPipelinePassesBotId:
    def test_run_v3_pipeline_is_called_with_an_explicit_bot_id(self):
        """Regression guard for the root cause: the call site must pass bot_id.

        Asserted against the source because the real call is buried in a long
        async cycle method that cannot be invoked in a unit test.
        """
        import inspect
        from app.services import pipeline_service

        src = inspect.getsource(pipeline_service)
        call_idx = src.find("await run_v3_pipeline(")
        assert call_idx != -1, "run_v3_pipeline call site not found"
        call = src[call_idx:call_idx + 400]
        assert "bot_id=" in call, (
            "run_v3_pipeline must be called with an explicit bot_id — "
            "omitting it defaults to '' and silently disables HRP and held"
        )
