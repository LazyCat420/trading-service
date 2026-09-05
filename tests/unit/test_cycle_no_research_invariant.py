"""A cycle that called no tool at all decided on its briefing alone.

THE OUTAGE THIS EXISTS FOR — cycle-v3-1788565070, 2026-09-04/05. The Gold Spark
was serving DeepSeek V4 through SGLang launched without
`--tool-call-parser deepseekv4`, so every agent's tool call was returned as
TEXT in the message content. Nothing executed. 116 LLM agent runs across 12
tickers wrote **zero** rows to `agent_tool_telemetry`, the tool-less repair
pass rebuilt each artifact from the pre-collected briefing, `quality_scorer`
graded those 80-88, and 12 HOLD decisions were recorded. Every instrument in
the service read healthy, including the invariant that exists to catch
exactly "spent tokens, did no research":

    _check_agent_cost  needs >150_000 tokens at <=1 loop.
                       These runs were ~40_000 at exactly 1 loop.

An absence cannot be graded by a check that only grades what is present. The
fixtures below are the SHAPES of the real telemetry, and the thresholds they
exercise were calibrated against the real ratios (see the constant's docstring
in app/v3/invariants.py):

    broken   sglang     117 runs      0 tool rows   ratio 0.00
    healthy  GLM         20 runs     69 tool rows   ratio 3.45
    healthy  ds0731      56 runs    196 tool rows   ratio 3.50
    healthy  nemotron    10 runs     24 tool rows   ratio 2.40   <- smallest

Driven against the live database on 2026-09-05 with `record_violation` patched
out, the check fired on cycle-v3-1788565070 (llm_runs 116, tool_calls 0,
tickers 12) and was SILENT on all five healthy cycles above.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.v3 import invariants


def _runs(n: int, agent: str = "v3_junior_analyst", ticker: str = "SWBI") -> list[dict]:
    return [
        {"agent_name": agent, "ticker": ticker, "loops_used": 1, "outcome": "SUCCESS"}
        for _ in range(n)
    ]


@pytest.fixture
def recorded():
    """Capture violations instead of writing them."""
    seen: list[tuple[str, dict]] = []

    def _rec(kind, **detail):
        seen.append((kind, detail))
        return kind

    with patch.object(invariants, "record_violation", side_effect=_rec):
        yield seen


def _drive(runs: list[dict], tool_calls: int) -> list[str]:
    """Run the real check over fixture rows."""
    with patch("app.db.mongo_store.find_docs", return_value=runs), patch(
        "app.db.mongo_store.count_docs", return_value=tool_calls
    ):
        return invariants._check_cycle_did_research("cycle-v3-fixture")


class TestItFiresOnTheOutage:
    def test_a_cycle_with_no_tool_calls_is_a_violation(self, recorded):
        out = _drive(_runs(116), 0)
        assert out == [invariants.KIND_CYCLE_NO_RESEARCH]
        kind, detail = recorded[0]
        assert detail["llm_runs"] == 116
        assert detail["tool_calls"] == 0

    def test_the_roster_names_who_went_without(self, recorded):
        runs = _runs(6, "v3_junior_analyst") + _runs(6, "v3_bear_agent")
        _drive(runs, 0)
        assert recorded[0][1]["agents"] == ["v3_bear_agent", "v3_junior_analyst"]

    def test_the_roster_is_capped(self, recorded):
        runs = [
            {"agent_name": f"agent_{i:03d}", "ticker": "X", "loops_used": 1,
             "outcome": "SUCCESS"}
            for i in range(invariants.STALL_ROSTER_CAP + 20)
        ]
        _drive(runs, 0)
        assert len(recorded[0][1]["agents"]) == invariants.STALL_ROSTER_CAP


class TestItIsSilentOnHealthyCycles:
    @pytest.mark.parametrize(
        "label,n_runs,n_tools",
        [
            ("nemotron 1-ticker, the smallest healthy cycle", 10, 24),
            ("nemotron 1-ticker", 12, 35),
            ("GLM 3-ticker", 20, 69),
            ("nemotron 4-ticker", 38, 131),
            ("deepseek 9-ticker", 56, 196),
        ],
    )
    def test_a_researching_cycle_is_silent(self, recorded, label, n_runs, n_tools):
        assert _drive(_runs(n_runs), n_tools) == [], label
        assert recorded == []

    def test_one_single_tool_call_is_enough_to_clear_it(self, recorded):
        """The check is `== 0`, deliberately.

        There is no ratio to tune: the healthy floor is 2.40 tool calls per run
        and the outage is 0.00. A threshold anywhere between them would be a
        number nobody could defend, and the one that fires on a healthy cycle
        gets muted.
        """
        assert _drive(_runs(116), 1) == []


class TestItCannotFireOnNoise:
    def test_an_aborted_three_run_cycle_is_below_the_floor(self, recorded):
        """The watch-desk cycles that die at the regime engine carry 3 rows and
        no tool calls. They are a different failure and have their own row."""
        assert _drive(_runs(3), 0) == []
        assert recorded == []

    def test_the_floor_sits_below_the_smallest_healthy_cycle(self):
        assert invariants.NO_RESEARCH_MIN_RUNS < 10

    def test_cached_and_non_llm_rows_do_not_count_toward_the_floor(self, recorded):
        """`contradiction_shadow` is deterministic and `v3_regime_engine`'s
        result is cached; a cycle made only of those has not failed to
        research, it simply had nothing to research with."""
        runs = _runs(7, "contradiction_shadow") + _runs(7, "v3_regime_engine")
        assert _drive(runs, 0) == []
        assert recorded == []


class TestTheOldCheckCouldNotSeeIt:
    def test_the_cost_floor_is_far_above_the_observed_runs(self):
        """The mechanism of the miss, asserted.

        `_check_agent_cost` fires above `COST_NO_RESEARCH_TOKENS` at <=1 loop.
        The sglang runs averaged 37_260-48_538 tokens at exactly 1.0 loops, so
        the AND could never be true.
        """
        assert invariants.COST_NO_RESEARCH_TOKENS == 150_000
        observed_max_avg_tokens = 48_538
        assert observed_max_avg_tokens < invariants.COST_NO_RESEARCH_TOKENS

    def test_the_new_check_is_registered_in_the_cycle_sweep(self):
        """A check nothing calls is not a check."""
        import inspect

        src = inspect.getsource(invariants.check_cycle_complete)
        assert "_check_cycle_did_research" in src
