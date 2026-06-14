"""
Unit tests for the V3 Family Office Architecture.

Tests the data models, CIO directive routing, worker dispatch,
backward compatibility, and guardrail behavior.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.cognition.contracts.family_office import (
    CIODirective,
    CIODirectiveStatus,
    DataRequest,
    DebateRound,
    FamilyOfficeResult,
    FamilyOfficeVerdict,
    ManagerArgument,
    ManagerRole,
    WorkerResult,
    WorkerType,
)


# ── Data Model Tests ────────────────────────────────────────────────────

class TestManagerRole:
    """Test ManagerRole enum values and membership."""

    def test_all_8_roles_defined(self):
        assert len(ManagerRole) == 8

    def test_role_values(self):
        assert ManagerRole.CIO.value == "cio"
        assert ManagerRole.FUNDAMENTAL_PM.value == "fundamental_pm"
        assert ManagerRole.GROWTH_PM.value == "growth_pm"
        assert ManagerRole.MACRO_PM.value == "macro_pm"
        assert ManagerRole.RISK_MANAGER.value == "risk_manager"
        assert ManagerRole.CROSS_EXAMINER.value == "cross_examiner"
        assert ManagerRole.MEMORY_PM.value == "memory_pm"
        assert ManagerRole.WORKER_ORCHESTRATOR.value == "worker_orchestrator"


class TestWorkerType:
    """Test WorkerType enum."""

    def test_all_4_workers_defined(self):
        assert len(WorkerType) == 4

    def test_worker_values(self):
        assert WorkerType.QUANT.value == "worker_quant"
        assert WorkerType.FUNDAMENTAL.value == "worker_fundamental"
        assert WorkerType.NEWS.value == "worker_news"
        assert WorkerType.INSIDER.value == "worker_insider"


class TestDataRequest:
    """Test DataRequest model."""

    def test_create_basic_request(self):
        req = DataRequest(
            requesting_manager=ManagerRole.FUNDAMENTAL_PM,
            worker_type=WorkerType.FUNDAMENTAL,
            description="I need last 4 quarters of revenue and margins",
            ticker="AAPL",
        )
        assert req.requesting_manager == ManagerRole.FUNDAMENTAL_PM
        assert req.worker_type == WorkerType.FUNDAMENTAL
        assert req.priority == "normal"
        assert req.specific_metrics == []

    def test_create_critical_request_with_metrics(self):
        req = DataRequest(
            requesting_manager=ManagerRole.CIO,
            worker_type=WorkerType.QUANT,
            description="RSI and MACD data urgently needed",
            priority="critical",
            ticker="TSLA",
            specific_metrics=["rsi", "macd", "bollinger"],
        )
        assert req.priority == "critical"
        assert len(req.specific_metrics) == 3

    def test_frozen_model(self):
        req = DataRequest(
            requesting_manager=ManagerRole.CIO,
            worker_type=WorkerType.QUANT,
            description="test",
        )
        with pytest.raises(Exception):
            req.description = "changed"


class TestWorkerResult:
    """Test WorkerResult model."""

    def test_successful_result(self):
        result = WorkerResult(
            worker_type=WorkerType.QUANT,
            request_description="Get RSI data",
            data="RSI: 37.8, MACD: -1.2",
            source="v3_worker_quant",
            success=True,
        )
        assert result.success is True
        assert "RSI" in result.data

    def test_failed_result(self):
        result = WorkerResult(
            worker_type=WorkerType.NEWS,
            request_description="Get news",
            data="",
            success=False,
            error="API timeout",
        )
        assert result.success is False
        assert result.error == "API timeout"


class TestManagerArgument:
    """Test ManagerArgument model."""

    def test_create_argument_with_claims(self):
        arg = ManagerArgument(
            role=ManagerRole.FUNDAMENTAL_PM,
            claims=[
                "Revenue grew 15% YoY [financials:revenue_growth=15%]",
                "FCF margin is 28% [financials:fcf_margin=28%]",
            ],
            confidence=75,
            conviction="HIGH",
            key_argument="Strong revenue growth with improving margins",
            devils_advocate="High valuation multiples relative to peers",
        )
        assert len(arg.claims) == 2
        assert arg.confidence == 75
        assert arg.conviction == "HIGH"

    def test_argument_with_data_requests(self):
        arg = ManagerArgument(
            role=ManagerRole.GROWTH_PM,
            claims=["RSI at 37 suggests oversold [technical:RSI=37]"],
            confidence=60,
            data_requests=[
                DataRequest(
                    requesting_manager=ManagerRole.GROWTH_PM,
                    worker_type=WorkerType.QUANT,
                    description="Need MACD data",
                    ticker="AAPL",
                ),
            ],
        )
        assert len(arg.data_requests) == 1
        assert arg.data_requests[0].worker_type == WorkerType.QUANT


class TestCIODirective:
    """Test CIODirective routing logic."""

    def test_needs_more_data(self):
        directive = CIODirective(
            status=CIODirectiveStatus.NEEDS_MORE_DATA,
            rationale="Fundamental PM has no revenue data",
            data_requests=[
                DataRequest(
                    requesting_manager=ManagerRole.CIO,
                    worker_type=WorkerType.FUNDAMENTAL,
                    description="Get last 4Q revenue",
                    ticker="AAPL",
                ),
            ],
            directed_managers=[ManagerRole.FUNDAMENTAL_PM],
            round_number=1,
        )
        assert directive.status == CIODirectiveStatus.NEEDS_MORE_DATA
        assert len(directive.data_requests) == 1
        assert ManagerRole.FUNDAMENTAL_PM in directive.directed_managers

    def test_ready_for_verdict(self):
        directive = CIODirective(
            status=CIODirectiveStatus.READY_FOR_VERDICT,
            rationale="All PMs have submitted sufficient evidence",
            round_number=2,
        )
        assert directive.status == CIODirectiveStatus.READY_FOR_VERDICT
        assert len(directive.data_requests) == 0

    def test_abstain(self):
        directive = CIODirective(
            status=CIODirectiveStatus.ABSTAIN,
            rationale="Data quality too poor to make a decision",
            round_number=3,
        )
        assert directive.status == CIODirectiveStatus.ABSTAIN


class TestFamilyOfficeVerdict:
    """Test FamilyOfficeVerdict model."""

    def test_buy_verdict(self):
        verdict = FamilyOfficeVerdict(
            action="BUY",
            confidence=80,
            winning_side="bull",
            key_deciding_factor="Strong revenue growth",
            rationale="Fundamental PM convinced with 15% YoY growth",
            conviction="HIGH",
        )
        assert verdict.action == "BUY"
        assert verdict.confidence == 80

    def test_abstain_verdict(self):
        verdict = FamilyOfficeVerdict(
            action="HOLD",
            confidence=0,
            winning_side="split",
            rationale="[ABSTAIN] Insufficient evidence after 3 rounds",
            conviction="WATCH",
        )
        assert verdict.confidence == 0
        assert "ABSTAIN" in verdict.rationale


class TestDebateRound:
    """Test DebateRound model."""

    def test_create_round(self):
        rnd = DebateRound(
            round_number=1,
            pm_arguments=[
                ManagerArgument(role=ManagerRole.FUNDAMENTAL_PM, claims=["claim1"]),
                ManagerArgument(role=ManagerRole.RISK_MANAGER, claims=["risk1"]),
            ],
            cross_exam_findings="All claims verified.",
            tokens_used=5000,
            elapsed_ms=15000,
        )
        assert rnd.round_number == 1
        assert len(rnd.pm_arguments) == 2
        assert rnd.tokens_used == 5000


# ── Backward Compatibility Tests ────────────────────────────────────────

class TestFamilyOfficeResultBackwardCompat:
    """Test that FamilyOfficeResult converts to DebateResult correctly."""

    def test_to_debate_result_basic(self):
        fo = FamilyOfficeResult(
            ticker="AAPL",
            debate_rounds=[
                DebateRound(
                    round_number=1,
                    pm_arguments=[
                        ManagerArgument(
                            role=ManagerRole.FUNDAMENTAL_PM,
                            claims=["Revenue is growing [financials:revenue_growth=15%]"],
                            confidence=75,
                        ),
                        ManagerArgument(
                            role=ManagerRole.RISK_MANAGER,
                            claims=["Valuation is stretched [metrics:pe_ratio=35]"],
                            confidence=60,
                        ),
                    ],
                    cross_exam_findings="All claims verified.",
                ),
            ],
            verdict=FamilyOfficeVerdict(
                action="BUY",
                confidence=70,
                winning_side="bull",
                rationale="Fundamental PM convinced the CIO",
            ),
            total_tokens=10000,
        )

        dr = fo.to_debate_result()

        # DebateResult shape assertions
        assert dr.judge_action == "BUY"
        assert dr.judge_confidence == 70
        assert dr.winning_side == "bull"
        assert dr.total_tokens == 10000
        assert len(dr.bull_claims) == 1  # Fundamental PM → bull
        assert len(dr.bear_claims) == 1  # Risk Manager → bear
        assert dr.judge_rationale == "Fundamental PM convinced the CIO"

    def test_to_debate_result_multi_round(self):
        fo = FamilyOfficeResult(
            ticker="TSLA",
            debate_rounds=[
                DebateRound(
                    round_number=1,
                    pm_arguments=[
                        ManagerArgument(role=ManagerRole.FUNDAMENTAL_PM, claims=["claim1"]),
                    ],
                    cio_directive=CIODirective(
                        status=CIODirectiveStatus.NEEDS_MORE_DATA,
                        round_number=1,
                    ),
                ),
                DebateRound(
                    round_number=2,
                    pm_arguments=[
                        ManagerArgument(role=ManagerRole.FUNDAMENTAL_PM, claims=["claim1_refined"]),
                    ],
                ),
            ],
            verdict=FamilyOfficeVerdict(action="SELL", confidence=65),
            total_tokens=15000,
            total_rounds=2,
        )

        dr = fo.to_debate_result()
        assert dr.judge_action == "SELL"
        assert dr.judge_confidence == 65
        # Round 2 claims should have survived_rebuttal=True
        round2_claims = [c for c in dr.bull_claims if c["survived_rebuttal"]]
        assert len(round2_claims) >= 1

    def test_to_debate_result_empty(self):
        fo = FamilyOfficeResult(ticker="EMPTY")
        dr = fo.to_debate_result()
        assert dr.judge_action == "HOLD"
        assert dr.judge_confidence == 0
        assert len(dr.bull_claims) == 0


# ── Action Gate Integration Tests ───────────────────────────────────────

class TestActionGateIntegration:
    """Test that V3 verdicts are properly gated by position state."""

    def test_not_held_buy_allowed(self):
        from app.cognition.debate.action_gate import gate_action
        assert gate_action("BUY", held=False) == "BUY"

    def test_not_held_hold_remapped(self):
        from app.cognition.debate.action_gate import gate_action
        assert gate_action("HOLD", held=False) == "SELL"

    def test_held_hold_allowed(self):
        from app.cognition.debate.action_gate import gate_action
        assert gate_action("HOLD", held=True) == "HOLD"

    def test_held_sell_allowed(self):
        from app.cognition.debate.action_gate import gate_action
        assert gate_action("SELL", held=True) == "SELL"


# ── Manager Evidence Filter Tests ───────────────────────────────────────

class TestManagerEvidenceFilter:
    """Test evidence filtering for different manager roles."""

    def test_risk_manager_sees_all(self):
        from app.agents.debate_agents.family_office_managers import MANAGER_EVIDENCE_FILTER
        # Risk Manager has empty filter → sees full packet
        assert MANAGER_EVIDENCE_FILTER.get(ManagerRole.RISK_MANAGER) == []

    def test_memory_pm_sees_all(self):
        from app.agents.debate_agents.family_office_managers import MANAGER_EVIDENCE_FILTER
        assert MANAGER_EVIDENCE_FILTER.get(ManagerRole.MEMORY_PM) == []

    def test_fundamental_pm_has_filter(self):
        from app.agents.debate_agents.family_office_managers import MANAGER_EVIDENCE_FILTER
        fundamental_filter = MANAGER_EVIDENCE_FILTER.get(ManagerRole.FUNDAMENTAL_PM)
        assert fundamental_filter is not None
        assert "revenue" in fundamental_filter
        assert "rsi" not in fundamental_filter

    def test_growth_pm_has_filter(self):
        from app.agents.debate_agents.family_office_managers import MANAGER_EVIDENCE_FILTER
        growth_filter = MANAGER_EVIDENCE_FILTER.get(ManagerRole.GROWTH_PM)
        assert growth_filter is not None
        assert "rsi" in growth_filter
        assert "revenue" not in growth_filter


# ── Configuration Tests ─────────────────────────────────────────────────

class TestV3Configuration:
    """Test V3 configuration defaults."""

    def test_v3_disabled_by_default(self):
        from app.config.config_cognition import CognitionSettings
        settings = CognitionSettings()
        assert settings.V3_FAMILY_OFFICE_ENABLED is False

    def test_v3_max_loops_default(self):
        from app.config.config_cognition import CognitionSettings
        settings = CognitionSettings()
        assert settings.V3_MAX_CIO_LOOPS == 3

    def test_v3_abstain_default(self):
        from app.config.config_cognition import CognitionSettings
        settings = CognitionSettings()
        assert settings.V3_ABSTAIN_ON_MAX_LOOPS is True


# ── Worker Whitelist Tests ──────────────────────────────────────────────

class TestWorkerWhitelists:
    """Test that V3 worker whitelists are properly defined."""

    def test_quant_worker_has_tools(self):
        from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
        assert "v3_worker_quant" in AGENT_TOOL_WHITELISTS
        tools = AGENT_TOOL_WHITELISTS["v3_worker_quant"]
        assert "get_market_data" in tools
        assert "get_technical_indicators" in tools

    def test_fundamental_worker_has_tools(self):
        from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
        assert "v3_worker_fundamental" in AGENT_TOOL_WHITELISTS
        tools = AGENT_TOOL_WHITELISTS["v3_worker_fundamental"]
        assert "get_finviz_fundamentals" in tools
        assert "query_financial_metrics" in tools

    def test_news_worker_has_tools(self):
        from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
        assert "v3_worker_news" in AGENT_TOOL_WHITELISTS
        tools = AGENT_TOOL_WHITELISTS["v3_worker_news"]
        assert "get_finnhub_news" in tools
        assert "search_web" in tools

    def test_insider_worker_has_tools(self):
        from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
        assert "v3_worker_insider" in AGENT_TOOL_WHITELISTS
        tools = AGENT_TOOL_WHITELISTS["v3_worker_insider"]
        assert "get_insider_trades" in tools
        assert "get_congress_trades" in tools

    def test_workers_dont_have_trading_tools(self):
        """Workers should never have access to trading execution tools."""
        from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
        for worker_key in ["v3_worker_quant", "v3_worker_fundamental", "v3_worker_news", "v3_worker_insider"]:
            tools = AGENT_TOOL_WHITELISTS[worker_key]
            assert "buy_stock" not in tools
            assert "sell_stock" not in tools
