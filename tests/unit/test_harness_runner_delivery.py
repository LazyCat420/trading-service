"""Use the real prompt assembler/parser; substitute only the external model."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from app.v3.shared_desk import SharedDesk, PhaseOutcome
from app.v3.agent_runner import run_v3_agent
from app.v3.decision_contract import board_reference


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_synth_receives_defense_and_enforces_board_timing(invalid):
    desk = SharedDesk(ticker="DKS", cycle_id="fixture-delivery")
    desk.cycle_metadata = {"decision_contract_version": 1, "held": False,
                           "data_report": "Verified price and filing. " * 2000}
    desk.desk_note = {"summary": "Earlier research. " * 2000}
    desk.cycle_metadata['research_questions'] = [{'id': 'question-1',
        'payload': {'question': 'Have the price and filing been verified?'}}]
    answer = {'item_id': 'question-1', 'status': 'answered',
        'answer': 'The price and filing were verified for this research question.',
        'evidence': [{'source': 'data_report', 'quote': 'Verified price and filing.'}]}
    desk.fundamental_report = {'summary': 'Verified evidence.', 'research_answers': [answer]}
    from app.services.research_work import record_tool_receipts
    record_tool_receipts(desk.fundamental_report, [],
        delivered_text=desk.cycle_metadata['data_report'], metadata=desk.cycle_metadata)
    desk.bull_defense = {"summary": "Narrowed thesis", "thesis_survives": True,
        "final_confidence": 72, "independent_risks_answered": [
            {"risk": "Dilution", "answer": "Dilution remains unresolved until the next reported share count."}]}
    board = {"action": "BUY", "confidence": 72, "reasoning": "Wait for the pullback to support before entering.",
        "position_size_pct": 1.0, "stop_loss": 118, "take_profit": 155,
        "entry_mode": "enter_on_condition", "trigger_purpose": "entry",
        "dynamic_trigger": {"type": "sma_50_drop", "value": 125.2}}
    desk.final_decision = deepcopy(board)
    decision = {**board, "source_board_ref": board_reference(board),
        "source_board_action": "BUY", "decision_relation": "preserve"}
    if invalid:
        decision.update(entry_mode="enter_now", trigger_purpose="monitor")
    captured = []
    async def model(**kwargs):
        captured.append(kwargs)
        return {"response": json.dumps(decision), "tokens_used": 400, "loops_used": 1, "stop_reason": "completed"}
    from app.v3.agents import decision_agent
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=model)):
        outcome = await run_v3_agent(desk=desk, agent_module=decision_agent,
                                    cycle_id=desk.cycle_id, bot_id="test", include_debate_context=True)
    prompt = captured[0]["system_prompt"] + captured[0]["user_prompt"]
    assert board_reference(board) in prompt
    assert answer['answer'] in prompt
    assert desk.bull_defense["independent_risks_answered"][0]["answer"] in prompt
    assert desk.cycle_metadata["context_delivery"][-1]["defense_delivered"]
    assert desk.cycle_metadata["context_delivery"][-1]["research_answers_delivered"]
    if invalid:
        assert outcome == PhaseOutcome.AGENT_ERROR
        assert not desk.trade_decision
    else:
        assert outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP)
        assert desk.trade_decision["entry_mode"] == "enter_on_condition"
        assert desk.trade_decision["source_board_ref"] == board_reference(board)


def test_unreceived_evidence_and_forged_receipts_do_not_complete_questions():
    from app.services.research_work import record_tool_receipts, _evidenced
    desk = SharedDesk(ticker="TEST", cycle_id="receipt-check")
    desk.cycle_metadata = {"data_report": "Operating margin is twelve point four percent."}
    answer = {"evidence": [{"source": "data_report", "quote": "Operating margin is twelve point four percent."}]}
    artifact = {"research_answers": [answer], "_research_tool_receipts": answer["evidence"]}
    record_tool_receipts(artifact, [], delivered_text="This field was shed.", metadata=desk.cycle_metadata)
    assert not _evidenced(answer, artifact, desk)
    record_tool_receipts(artifact, [], delivered_text=desk.cycle_metadata["data_report"], metadata=desk.cycle_metadata)
    assert _evidenced(answer, artifact, desk)


def test_queued_question_carries_its_original_time_scope():
    from app.services.research_work import question_block
    block = question_block([{'id': 'old-question', 'created_at': '2026-08-17T14:31:30Z',
        'source_agent': 'quant', 'payload': {'question': 'Was volume decreasing in that five-day window?'}}])
    assert '2026-08-17T14:31:30Z' in block
    assert 'current snapshot does not answer a historical' in block
