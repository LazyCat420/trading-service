"""Cost per decision is half-measured unless the OUTPUT side is recorded.

MEASURED 2026-09-12: `completion_tokens` was 0 on 200/200 recent rows of
`llm_audit_logs`, and `v3_agent_telemetry` carried `prompt_tokens` /
`token_usage` and no completion count at all. The number was available at the
seam and thrown away: the SDK stream carries
`usage.inputTokens / outputTokens / reasoningOutputTokens` per request, and
`base_agent` read only `totalInputTokens` / `inputTokens` off `last_usage`.

THE TRAP THIS MUST NOT RE-CREATE
--------------------------------
A completion with an ALL-ZERO reported usage block is a TRUNCATED generation,
not a cheap one (`zero-reported-token-usage-marks-a-truncated-generation`:
200 chars + all-zero usage = cut off, 8/8, and no stop_reason says so). A dict
of zeros is truthy, so it folds to 0; an ABSENT usage block leaves `last_usage`
at `{}` and `or 0` collapses to the same 0. So a RECORDED zero must stay
distinguishable from NOT RECORDED — that is what `usage_requests` is for:
`usage_requests == 0` is the only "we have no measurement" sentinel, and
`completion_tokens` is then None rather than a confident zero.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agents.base_agent import harness_usage_totals


class _FakeHarness:
    def __init__(self, completion_tokens=0, usage_requests=0):
        self.completion_tokens = completion_tokens
        self.usage_requests = usage_requests


class TestARecordedZeroIsNotAMissingMeasurement:
    def test_a_reported_zero_is_recorded_as_a_zero(self):
        """All-zero usage from 2 requests: a TRUNCATION, and we know it."""
        totals = harness_usage_totals(_FakeHarness(completion_tokens=0, usage_requests=2))
        assert totals["usage_requests"] == 2
        assert totals["completion_tokens"] == 0

    def test_no_usage_block_at_all_is_not_a_zero(self):
        """Nothing reported ⇒ None, never a confident 0."""
        totals = harness_usage_totals(_FakeHarness(completion_tokens=0, usage_requests=0))
        assert totals["usage_requests"] == 0
        assert totals["completion_tokens"] is None

    def test_the_two_cases_are_distinguishable(self):
        truncated = harness_usage_totals(_FakeHarness(0, 3))
        unmeasured = harness_usage_totals(_FakeHarness(0, 0))
        assert truncated != unmeasured, (
            "a truncated generation and an unmeasured one folded to the same row"
        )

    def test_a_real_count_survives(self):
        totals = harness_usage_totals(_FakeHarness(completion_tokens=8421, usage_requests=4))
        assert totals == {"completion_tokens": 8421, "usage_requests": 4}

    def test_an_sdk_without_the_attributes_reports_not_recorded(self):
        """An older SDK has neither attribute — that is NOT RECORDED, not 0."""
        class _Old:
            pass

        assert harness_usage_totals(_Old()) == {
            "completion_tokens": None,
            "usage_requests": 0,
        }


class TestRunAgentReturnsIt:
    """The harness accumulator has to reach run_agent's result dict."""

    @pytest.mark.asyncio
    async def test_the_result_dict_carries_the_completion_count(self):
        from lazycat.agent import AgentHarness

        async def _fake_run(self, prompt=None):
            self.completion_tokens = 1234
            self.usage_requests = 3
            self.total_tokens = 5678
            self.last_usage = {"inputTokens": 4444, "outputTokens": 900}
            return '{"result": "ok"}'

        with patch.object(AgentHarness, "run", new=_fake_run), patch(
            "app.services.prism_agent_caller.resolve_default_model_for_agent",
            new_callable=AsyncMock,
            return_value=(None, None),
        ):
            from app.agents.base_agent import run_agent

            result = await run_agent(
                agent_name="v3_junior_analyst",
                ticker="_AUDIT_TEST",
                cycle_id="cycle-test",
                bot_id="bot-test",
                system_prompt="You are a test agent.",
                user_prompt="Analyze.",
                enable_tools=True,
            )

        assert result["completion_tokens"] == 1234, result
        assert result["usage_requests"] == 3, result


class TestTheTelemetryEntryCarriesIt:
    def test_record_telemetry_puts_it_on_the_desk_entry(self):
        from app.v3 import agent_runner
        from app.v3.shared_desk import SharedDesk

        desk = SharedDesk(cycle_id="cycle-test", ticker="_AUDIT_TEST")
        with patch("app.v3.telemetry.flush_agent_telemetry", lambda *_a, **_k: 0):
            agent_runner._record_telemetry(
                desk, "v3_junior_analyst", 1000, 2, 5678, "SUCCESS",
                prompt_tokens=4444, completion_tokens=1234, usage_requests=3,
            )

        entry = desk.agent_telemetry[-1]
        assert entry["completion_tokens"] == 1234
        assert entry["usage_requests"] == 3

    def test_an_unmeasured_run_records_none_not_zero(self):
        from app.v3 import agent_runner
        from app.v3.shared_desk import SharedDesk

        desk = SharedDesk(cycle_id="cycle-test", ticker="_AUDIT_TEST")
        with patch("app.v3.telemetry.flush_agent_telemetry", lambda *_a, **_k: 0):
            agent_runner._record_telemetry(
                desk, "v3_junior_analyst", 1000, 2, 0, "AGENT_ERROR",
            )

        entry = desk.agent_telemetry[-1]
        assert entry["usage_requests"] == 0
        assert entry["completion_tokens"] is None


class TestItSurvivesTheAllowlist:
    """`_persist_entries` builds its record from an EXPLICIT allowlist, so a
    field added upstream is silently dropped unless it is named there."""

    def _persisted(self, entry: dict) -> dict:
        from app.v3 import telemetry
        from app.v3.shared_desk import SharedDesk

        desk = SharedDesk(cycle_id="cycle-test", ticker="_AUDIT_TEST")
        captured: list[list[dict]] = []

        def _capture(collection, recs):
            assert collection == "v3_agent_telemetry"
            captured.append(recs)

        with patch.object(telemetry.mongo_store, "insert_docs", _capture):
            telemetry._persist_entries(desk, [entry])
        assert captured, "nothing was written"
        return captured[0][0]

    def test_the_written_record_carries_the_completion_count(self):
        rec = self._persisted({
            "agent_name": "v3_junior_analyst",
            "token_usage": 5678,
            "prompt_tokens": 4444,
            "completion_tokens": 1234,
            "usage_requests": 3,
        })
        assert rec["completion_tokens"] == 1234, (
            f"dropped by the allowlist; record was {rec}"
        )
        assert rec["usage_requests"] == 3

    def test_a_truncated_run_is_written_as_a_recorded_zero(self):
        rec = self._persisted({
            "agent_name": "v3_junior_analyst",
            "completion_tokens": 0,
            "usage_requests": 2,
        })
        assert rec["completion_tokens"] == 0
        assert rec["usage_requests"] == 2

    def test_an_unmeasured_run_is_written_as_null(self):
        rec = self._persisted({"agent_name": "v3_junior_analyst"})
        assert rec["completion_tokens"] is None, (
            "a missing measurement must not be written as a confident 0"
        )
        assert rec["usage_requests"] == 0


class TestTheIndexesAlreadyExist:
    """No new index is needed for a per-cycle / per-agent cost query."""

    def test_both_compound_indexes_are_declared(self):
        import inspect
        from app.db import mongo_store

        src = inspect.getsource(mongo_store)
        assert '_try("v3_agent_telemetry", [("cycle_id"' in src
        assert '_try("v3_agent_telemetry", [("agent_name"' in src
