from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.services import prism_agent_caller as pac


@pytest.mark.asyncio
async def test_model_not_found_retry_moves_model_and_provider_together():
    response = MagicMock()
    response.json.return_value={'text':'{"status":"ok"}','usage':{'inputTokens':1,'outputTokens':1}}
    with patch.object(pac,'prism_client') as client, \
         patch.object(pac,'resolve_default_model_for_agent',new_callable=AsyncMock,
                      side_effect=[('GLM','vllm-2'),('Nemotron','vllm')]) as resolve, \
         patch.object(pac,'publish_event'), \
         patch('app.v3.guardrails.get_budget_for_role',return_value=type('Budget',(),{'max_turns':1})()):
        client.call_agent=AsyncMock(side_effect=[RuntimeError('404 model not found'),response])
        await pac.call_prism_agent(agent_id='CUSTOM_V3_BOARD_OF_DIRECTORS',
            user_message='Synthetic fixture',fallback_system_prompt='Return JSON',
            fallback_agent_name='v3_board_of_directors',max_tokens=1024)
    assert client.call_agent.await_count == 2
    second=client.call_agent.call_args_list[1].kwargs
    assert (second['model'],second['provider']) == ('Nemotron','vllm')
    assert resolve.call_args.kwargs['force_refresh'] is True


@pytest.mark.asyncio
async def test_tool_grading_runs_even_when_model_dependent_audit_fails():
    from app.autoresearch import eval_worker, core
    events=[]
    def grade(**kwargs):
        events.append(('grade',kwargs))
        return 2
    async def unavailable(*args):
        events.append(('model_audit',None))
        raise RuntimeError('only remaining model unavailable')
    with patch.object(eval_worker,'process_pending_traces',side_effect=grade), \
         patch.object(core,'run_autoresearch',side_effect=unavailable):
        with pytest.raises(Exception,match='Core run_autoresearch failed'):
            await eval_worker.run_autoresearch('job',{'cycle_id':'current','cycle_summary':{'status':'done'}})
    assert events[0] == ('grade',{'limit':1000,'cycle_id':'current'})
    assert events[1] == ('grade',{'limit':500})
    assert events[2][0] == 'model_audit'
