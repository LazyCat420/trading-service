"""A repair that comes back cut off is re-asked; one that comes back whole is not.

Measured 2026-09-11 on the case03 repair payload: the SAME prompt truncated in
a degenerate repetition loop on 3 of 8 identical attempts, while nemotron35's
context (128k) and the harness budget (8192) were both far from binding. The
detection already existed -- `classify_output` returned TRUNCATED_JSON for every
one of them, agreeing 6 of 6 with `usage.outputTokens == 0` -- and nothing acted
on it: the single repair attempt failed and the desk fell through to the Board.

The line these tests hold is that retrying is allowed ONLY for a generation that
stopped mid-artifact. Re-asking because the artifact's CONTENT is unwelcome
would be re-rolling until the model says something nicer, and no assertion here
may be satisfiable that way.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.v3 import data_trace
from app.v3.agent_runner import REPAIR_ATTEMPTS, REPAIR_RETRY_MIN_BUDGET_S
from app.v3.output_rules import ZERO_USAGE_MIN_CHARS, classify_output, was_cut_off
from app.v3.shared_desk import SharedDesk
from app.v3.financial_evidence import calculated_facts

CASES = {c['id']: c for c in json.loads(
    (Path(__file__).resolve().parents[1] / 'benchmarks/fixtures/financial_reasoning_v1.json').read_text())['cases']}


def fixture():
    from app.v3.agents import board_of_directors as board
    record = deepcopy(CASES['headroom'])
    record['questions'] = []
    fact = calculated_facts(record)['calc_headroom_pct']
    good = {'action': 'HOLD', 'confidence': 72,
            'reasoning': 'The proposal exceeds available headroom [calc_headroom_pct].',
            'position_size_pct': 0, 'entry_mode': 'watch_only', 'trigger_purpose': 'none',
            'dynamic_trigger': None,
            'financial_claims': [{'fact_id': fact['id'],
                                  **{k: fact[k] for k in ('metric', 'value', 'unit', 'as_of', 'source')}}],
            'research_answers': []}
    desk = SharedDesk(ticker='EVLT', cycle_id='bench-repair-retry')
    desk.cycle_metadata = {'decision_contract_version': 1, 'financial_evidence_version': 1,
                           'held': True, 'financial_evidence_record': record}
    module = SimpleNamespace(AGENT_NAME=board.AGENT_NAME, ARTIFACT_TYPE=board.ARTIFACT_TYPE,
                             TOOL_WHITELIST=board.TOOL_WHITELIST,
                             SYSTEM_PROMPT=board.PERSONA_JANE_STREET)
    return desk, module, good


def reply(text, tokens=80):
    return {'response': text, 'tokens_used': tokens, 'loops_used': 1, 'stop_reason': 'completed'}


async def drive(desk, module, replies):
    from app.v3.agent_runner import run_v3_agent
    with patch('app.agents.base_agent.run_agent', new_callable=AsyncMock, side_effect=replies) as model, \
         patch.object(data_trace.mongo_store, 'insert_docs'), \
         patch.object(data_trace.mongo_store, 'update_docs'):
        outcome = await run_v3_agent(desk, module, cycle_id=desk.cycle_id, bot_id='test')
    return outcome, model


class TestTheCutOffSignalItself:
    """Both signals are derived from the reply, never from a pinned string."""

    def test_unclosed_json_is_cut_off_however_many_tokens_were_reported(self):
        _, _, good = fixture()
        stump = json.dumps(good)[:-40]
        assert classify_output(stump).name == 'TRUNCATED_JSON'
        assert was_cut_off({'tokens_used': 4096}, stump) is True

    def test_a_substantial_reply_with_zero_reported_output_is_cut_off(self):
        _, _, good = fixture()
        whole = json.dumps(good)
        assert len(whole) >= ZERO_USAGE_MIN_CHARS
        assert was_cut_off({'tokens_used': 0}, whole) is True
        assert was_cut_off({'tokens_used': 80}, whole) is False

    def test_a_short_reply_with_zero_usage_is_not_evidence_of_anything(self):
        assert was_cut_off({'tokens_used': 0}, 'no') is False

    def test_missing_usage_is_not_read_as_zero(self):
        _, _, good = fixture()
        assert was_cut_off({}, json.dumps(good)) is False
        assert was_cut_off({'tokens_used': None}, json.dumps(good)) is False

    def test_a_complete_artifact_is_never_cut_off_whatever_it_says(self):
        _, _, good = fixture()
        unwelcome = {**good, 'action': 'BUY', 'confidence': 99}
        assert was_cut_off({'tokens_used': 80}, json.dumps(unwelcome)) is False


class TestTheRunnerActsOnIt:

    @pytest.mark.asyncio
    async def test_a_cut_off_repair_is_re_asked_and_the_artifact_recovered(self):
        desk, module, good = fixture()
        stump = json.dumps(good)[:-40]
        # call 1 fails to parse -> repair; call 2 (the repair) is cut off ->
        # retry; call 3 lands.
        outcome, model = await drive(desk, module, [reply(stump), reply(stump), reply(json.dumps(good))])
        assert model.await_count == 3
        assert desk.final_decision is not None
        assert desk.final_decision['action'] == 'HOLD'
        retries = desk.cycle_metadata.get('repair_retries')
        assert retries and retries[0]['attempts'][0]['cut_off'] is True
        assert retries[0]['attempts'][-1]['cut_off'] is False

    @pytest.mark.asyncio
    async def test_a_whole_repair_is_not_re_asked(self):
        desk, module, good = fixture()
        stump = json.dumps(good)[:-40]
        outcome, model = await drive(desk, module, [reply(stump), reply(json.dumps(good))])
        assert model.await_count == 2, 'a complete repair must not be re-rolled'
        assert 'repair_retries' not in desk.cycle_metadata

    @pytest.mark.asyncio
    async def test_the_retries_are_bounded(self):
        desk, module, good = fixture()
        stump = json.dumps(good)[:-40]
        # Every reply is cut off; the runner must stop, not spin.
        outcome, model = await drive(desk, module, [reply(stump)] * 8)
        assert model.await_count == 1 + REPAIR_ATTEMPTS

    @pytest.mark.asyncio
    async def test_no_retry_is_started_without_the_budget_to_finish_it(self):
        desk, module, good = fixture()
        stump = json.dumps(good)[:-40]
        # A clock that has already burned the whole run budget by the time the
        # first repair returns: the retry must be refused, not attempted.
        import app.v3.agent_runner as runner
        base = runner.time.monotonic()
        ticks = iter([base, base, base, base + 10_000, base + 10_000, base + 10_000])
        real = runner.time.monotonic
        with patch.object(runner.time, 'monotonic', lambda: next(ticks, base + 10_000)):
            outcome, model = await drive(desk, module, [reply(stump)] * 8)
        assert model.await_count == 2, 'exactly the first repair, no retry'
        assert REPAIR_RETRY_MIN_BUDGET_S > 0
