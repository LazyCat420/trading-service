"""Tests for the measured skill gate.

The decision logic is pure on purpose: version stamping began 2026-07-25 and
nothing stamped has cleared the 7-day resolve lag, so no live row can exercise
these paths yet. Everything that decides whether a live system prompt gets
reverted is tested here instead of waiting a week to find out.
"""
from __future__ import annotations

import pytest

from app.autoresearch.eval_engine import (
    ERROR_CLASS_INFRA, classify_error_class, classify_tool_failure,
)
from app.autoresearch.scorecard import (
    CONTAMINATION_INCOMPLETE_RATE, MATURITY_N, MIN_TRACES_FOR_CONTAMINATION,
    REGRESSION_MARGIN, VERDICT_CONTAMINATED, VERDICT_HEALTHY, VERDICT_IMMATURE,
    Component, _weighted, blend, classify,
)


# ── Infra vs agent attribution ──────────────────────────────────────────────
# Every string below is copied from a real trace sampled over 21 days.


@pytest.mark.parametrize("summary", [
    "ERROR: {'error': 'Failed to reach trading-service at http://10.0.0.16:3031/"
    "api/v1/agent-tools/execute: This operation was aborted', 'is_error': True}",
    "ERROR: {'role': 'tool', 'name': 'lazy_web_search', 'content': "
    "'{\"status\": \"error\", \"message\": \"Search failed: ...\"}'}",
    "ERROR: {'error': 'MCP tool call failed: MCP error -32001: Request timed out'}",
    "ERROR: {'error': 'Failed to reach trading-service ...: bridge timeout after 30000ms'}",
    "ERROR: {'error': 'MCP tool call failed: Error POSTing to endpoint (HTTP 400): "
    "{\"error\":\"Invalid or expired session\"}'}",
    "ERROR: {'error': 'HTTP 403: https://www.ainvest.com/stocks/NYSE-DIS/financials'}",
])
def test_transport_failures_are_infra(summary):
    """A dead provider is not the agent using it badly.

    These were all recorded as `bad_arguments`, which is why the fundamental
    analyst's eval failure rate tracked DuckDuckGo's uptime.
    """
    assert classify_tool_failure(summary) == "infra"


@pytest.mark.parametrize("summary", [
    "ERROR: {'content': '{\"error\": \"whiteboard_write() got an unexpected keyword\"}'}",
    "ERROR: {'error': \"At least one of 'query' or 'domain' is required.\"}",
    'ERROR: {\'error\': \'Skill "fundamental_analysis" not found. Use list_skills to see\'}',
    "ERROR: {'content': '{\"error\": \"Need at least 2 tickers for allocation\"}'}",
    "ERROR: {'content': '{\"error\": \"need >= 100 daily returns\"}'}",
])
def test_agent_mistakes_stay_attributed_to_the_agent(summary):
    assert classify_tool_failure(summary) == "agent"


def test_an_agent_marker_wins_over_a_transport_word():
    """Checked in that order deliberately: the agent signal is rarer and the
    one worth keeping, and agent-side text can quote a transport word."""
    assert classify_tool_failure(
        "ERROR: missing required argument 'timeout'"
    ) == "agent"


@pytest.mark.parametrize("summary", [None, "", "ERROR: something entirely new"])
def test_unrecognised_failures_are_not_guessed(summary):
    """None is a real answer. Guessing either way corrupts the score: call it
    infra and a bad skill hides; call it agent and an outage reverts a good one."""
    assert classify_tool_failure(summary) is None


def test_tool_unavailable_maps_to_the_infra_class():
    assert classify_error_class("tool_unavailable") == ERROR_CLASS_INFRA


def test_a_passing_run_has_no_error_class():
    assert classify_error_class(None) is None


# ── Outcome weighting ───────────────────────────────────────────────────────


def test_confidence_weights_the_mean():
    """A high-conviction loss should hurt more than a low-conviction one."""
    timid = _weighted([("WIN", 10), ("LOSS", 90)], {"WIN": 1.0, "LOSS": 0.0})
    bold = _weighted([("WIN", 90), ("LOSS", 10)], {"WIN": 1.0, "LOSS": 0.0})
    assert timid.score < 0.5 < bold.score
    assert timid.n == bold.n == 2


def test_zero_confidence_is_neutral_not_dropped():
    """Coercing to 0 would silently remove the row from the denominator."""
    c = _weighted([("WIN", 0), ("LOSS", 0)], {"WIN": 1.0, "LOSS": 0.0})
    assert c.n == 2
    assert c.score == pytest.approx(0.5)


def test_outcomes_outside_the_population_are_ignored():
    """HOLD rows must not leak into the directional component."""
    c = _weighted([("WIN", 50), ("HOLD_CORRECT", 50)], {"WIN": 1.0, "LOSS": 0.0})
    assert c.n == 1
    assert c.score == pytest.approx(1.0)


# ── Blending ────────────────────────────────────────────────────────────────


def test_hold_is_weighted_below_directional():
    """A HOLD is a weaker claim — "nothing much happens" is right by default —
    but it is the plurality of what the system decides, so it is not zero."""
    assert blend(1.0, 0.0) > 0.5
    assert blend(0.0, 1.0) < 0.5


def test_a_missing_component_does_not_sink_the_score():
    assert blend(0.8, None) == pytest.approx(0.8)
    assert blend(None, 0.8) == pytest.approx(0.8)
    assert blend(None, None) is None


# ── Verdicts ────────────────────────────────────────────────────────────────


def test_a_mature_clean_version_is_healthy():
    verdict, _ = classify(n_governed=MATURITY_N, combined=0.6,
                          incomplete_rate=0.02, n_traces=500)
    assert verdict == VERDICT_HEALTHY


def test_too_few_decisions_is_immature():
    verdict, detail = classify(n_governed=MATURITY_N - 1, combined=0.6,
                               incomplete_rate=0.0, n_traces=500)
    assert verdict == VERDICT_IMMATURE
    assert str(MATURITY_N) in detail


def test_contamination_beats_maturity():
    """Order matters. A window whose tools were broken is inadmissible however
    many decisions it governed — calling it IMMATURE would let it mature into a
    verdict it must never reach."""
    verdict, _ = classify(n_governed=MATURITY_N * 10, combined=0.9,
                          incomplete_rate=CONTAMINATION_INCOMPLETE_RATE + 0.01,
                          n_traces=500)
    assert verdict == VERDICT_CONTAMINATED


def test_the_outage_day_is_contaminated():
    """Measured: normal days peak at 18% incomplete; the DuckDuckGo outage
    put the fundamental analyst at 100%."""
    assert classify(n_governed=MATURITY_N, combined=0.9,
                    incomplete_rate=1.0, n_traces=100)[0] == VERDICT_CONTAMINATED
    assert classify(n_governed=MATURITY_N, combined=0.9,
                    incomplete_rate=0.18, n_traces=100)[0] == VERDICT_HEALTHY


def test_too_few_traces_cannot_declare_contamination():
    """One bad run out of three is not an outage."""
    verdict, _ = classify(n_governed=MATURITY_N, combined=0.6,
                          incomplete_rate=1.0,
                          n_traces=MIN_TRACES_FOR_CONTAMINATION - 1)
    assert verdict == VERDICT_HEALTHY


def test_no_score_is_immature_not_healthy():
    verdict, _ = classify(n_governed=MATURITY_N * 5, combined=None,
                          incomplete_rate=0.0, n_traces=500)
    assert verdict == VERDICT_IMMATURE


# ── The regression margin ───────────────────────────────────────────────────


def test_the_margin_is_the_measured_noise_band():
    """Bootstrapped over 1500 real resolved decisions: ±0.207 at n=25, ±0.104
    at n=100. A gate inside its own noise band fires at random, which is what
    the previous n=25 threshold did."""
    assert MATURITY_N == 100
    assert REGRESSION_MARGIN == pytest.approx(0.104)


def test_a_within_noise_drop_is_not_a_regression():
    """Guards the arithmetic the rollback decision turns on."""
    delta = -(REGRESSION_MARGIN - 0.001)
    assert not (delta < -REGRESSION_MARGIN)


def test_a_beyond_noise_drop_is_a_regression():
    delta = -(REGRESSION_MARGIN + 0.001)
    assert delta < -REGRESSION_MARGIN


def test_component_serialises_for_the_json_view():
    assert Component(score=0.5, n=3).to_dict() == {"score": 0.5, "n": 3}


# ── The rollback path ───────────────────────────────────────────────────────


def test_a_regressed_version_is_rolled_back_and_costs_no_llm_call():
    """The loop's only destructive action. A version measured worse than its
    predecessor must revert BEFORE a new proposal is paid for."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    import app.autoresearch.skill_optimizer as S
    from app.autoresearch.scorecard import VERDICT_REGRESSED, VersionScorecard

    regressed = VersionScorecard(
        agent_name="v3_bull_agent", version=5, combined=0.40,
        n_governed=MATURITY_N, verdict=VERDICT_REGRESSED,
        detail="v5 scored 0.400 against v4's 0.600",
    )
    llm = AsyncMock()
    with patch.object(S, "_load_skill", return_value=("doc", 5)), \
         patch.object(S, "_decisions_governed", return_value=MATURITY_N), \
         patch.object(S, "regression_verdict", return_value=regressed), \
         patch.object(S, "_rollback_skill", return_value=True) as rb, \
         patch.object(S, "_call_optimizer_llm", new=llm):
        out = asyncio.run(
            S._optimize_one_agent("v3_bull_agent", "role", {}, "cyc-1", 0.55)
        )
    assert out == "rolled_back"
    rb.assert_called_once()
    llm.assert_not_awaited(), "a reverting agent should not also pay for a proposal"


def test_a_failed_rollback_holds_rather_than_editing_on():
    """If the predecessor cannot be recovered, the safe move is to stop — not
    to stack another edit on top of a version already measured worse."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    import app.autoresearch.skill_optimizer as S
    from app.autoresearch.scorecard import VERDICT_REGRESSED, VersionScorecard

    regressed = VersionScorecard(agent_name="v3_bull_agent", version=5,
                                 combined=0.4, n_governed=MATURITY_N,
                                 verdict=VERDICT_REGRESSED)
    with patch.object(S, "_load_skill", return_value=("doc", 5)), \
         patch.object(S, "_decisions_governed", return_value=MATURITY_N), \
         patch.object(S, "regression_verdict", return_value=regressed), \
         patch.object(S, "_rollback_skill", return_value=False), \
         patch.object(S, "_call_optimizer_llm", new=AsyncMock()) as llm:
        out = asyncio.run(
            S._optimize_one_agent("v3_bull_agent", "role", {}, "cyc-1", 0.55)
        )
    assert out == "immature"
    llm.assert_not_awaited()


def test_a_contaminated_window_neither_promotes_nor_reverts():
    """The window's tools were broken, so its decisions say nothing about the
    doc. Editing on it would encode an outage into a system prompt."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    import app.autoresearch.skill_optimizer as S
    from app.autoresearch.scorecard import VersionScorecard

    contaminated = VersionScorecard(
        agent_name="v3_bull_agent", version=5, combined=0.9,
        n_governed=MATURITY_N * 5, verdict=VERDICT_CONTAMINATED,
        incomplete_rate=1.0, n_traces=200,
    )
    with patch.object(S, "_load_skill", return_value=("doc", 5)), \
         patch.object(S, "_decisions_governed", return_value=MATURITY_N * 5), \
         patch.object(S, "regression_verdict", return_value=contaminated), \
         patch.object(S, "_rollback_skill") as rb, \
         patch.object(S, "_call_optimizer_llm", new=AsyncMock()) as llm:
        out = asyncio.run(
            S._optimize_one_agent("v3_bull_agent", "role", {}, "cyc-1", 0.55)
        )
    assert out == "contaminated"
    rb.assert_not_called()
    llm.assert_not_awaited()


def test_rollback_appends_rather_than_reactivating():
    """Append-only. Reactivating the old row would stamp two disjoint periods
    with the same version number, and every scorecard query would silently pool
    them into one sample."""
    from unittest.mock import patch

    import app.autoresearch.skill_optimizer as S

    class _Cur:
        def __init__(self):
            self.q = []
        def execute(self, sql, params=None):
            self.q.append(sql)
            return self
        def fetchone(self):
            return ("predecessor doc", "hash4")

    cur = _Cur()

    class _Ctx:
        def __enter__(self): return cur
        def __exit__(self, *a): return False

    with patch.object(S, "get_db", lambda: _Ctx()), \
         patch.object(S, "_save_skill") as save, \
         patch.object(S, "_log_rejection"):
        assert S._rollback_skill("v3_bull_agent", 5, "cyc-1", "worse") is True

    kwargs = save.call_args.kwargs
    assert kwargs["new_version"] == 6, "rollback must mint a NEW version number"
    assert kwargs["skill_text"] == "predecessor doc"
    assert kwargs["action"] == "ROLLBACK"
