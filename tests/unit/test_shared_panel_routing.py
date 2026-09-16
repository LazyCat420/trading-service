"""Small automatic research panels must use both eligible serving endpoints."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.services import prism_agent_caller as routing


@pytest.fixture
def boxes(monkeypatch):
    endpoints = {
        key: SimpleNamespace(name=key, url=f'http://{key}:8000', enabled=True,
                             requests_running=0, requests_waiting=0, max_concurrent=6,
                             max_model_len=context, model=None, validation_required=True)
        for key, context in [('jetson', 128000), ('dgx_spark', 1000000)]
    }
    monkeypatch.setattr(routing, 'llm', SimpleNamespace(_endpoints=endpoints))

    async def discover(url, force_refresh=False):
        key = next(key for key, ep in endpoints.items() if ep.url == url)
        if not endpoints[key].enabled:
            raise routing.ModelUnavailableError('No serving model')
        return f'live-{key}'

    monkeypatch.setattr(routing, 'get_live_model_from_vllm', discover)
    monkeypatch.setattr('app.services.model_capabilities.validate_endpoint',
                        AsyncMock(return_value={'eligible': True, 'reason': 'verified'}))
    return endpoints


@pytest.mark.asyncio
async def test_single_ticker_panel_dispatches_to_both_boxes_without_saturation(boxes, monkeypatch):
    """Exercise actual base-agent dispatch, including model/provider receipts."""
    from app.agents.base_agent import run_agent
    from app.v3.agents import (regime_engine, junior_analyst, fundamental_analyst,
                              quant_analyst, bull_agent, bear_agent, bull_defense,
                              board_of_directors)
    from app.v3.agents import decision_agent as decision_synthesizer
    dispatched = []

    async def chat(**kwargs):
        dispatched.append(kwargs)
        return {'response': '{"result":"complete"}', 'tokens_used': 20,
                'loops_used': 1, 'model_used': kwargs['model'],
                'provider': kwargs['provider'], 'execution_ms': 1}

    monkeypatch.setattr(routing, 'chat_toolless', chat)
    monkeypatch.setattr('app.agents.tool_whitelists.get_agent_tools', lambda *a, **kw: [])
    for module in (regime_engine, junior_analyst, fundamental_analyst, quant_analyst,
                   bull_agent, bear_agent, bull_defense, board_of_directors, decision_synthesizer):
        result = await run_agent(agent_name=module.AGENT_NAME, ticker='AMD',
                                 cycle_id='routing-panel-test', bot_id='test',
                                 system_prompt='Return a JSON result.', user_prompt='Check supplied evidence.',
                                 enable_tools=False)
        expected_box = routing.box_for_agent(module.AGENT_NAME)
        assert dispatched[-1]['model'] == f'live-{expected_box}'
        assert result['provider'] == routing.ENDPOINT_PROVIDERS[expected_box]
    assert {row['provider'] for row in dispatched} == {'vllm', 'vllm-2'}


@pytest.mark.asyncio
@pytest.mark.parametrize('offline', ['jetson', 'dgx_spark'])
async def test_either_box_can_cover_panel_and_rejoin_without_restart(boxes, offline):
    boxes[offline].enabled = False
    remaining = 'dgx_spark' if offline == 'jetson' else 'jetson'
    roles = ['v3_junior_analyst', 'v3_board_of_directors']
    for role in roles:
        assert await routing.resolve_default_model_for_agent(role, minimum_context_tokens=20000) == (
            f'live-{remaining}', routing.ENDPOINT_PROVIDERS[remaining])
    boxes[offline].enabled = True
    providers = { (await routing.resolve_default_model_for_agent(role, minimum_context_tokens=20000))[1]
                  for role in roles }
    assert providers == {'vllm', 'vllm-2'}


@pytest.mark.asyncio
async def test_jetson_panel_role_overflows_to_idle_dgx(boxes):
    boxes['jetson'].requests_running = 6
    assert (await routing.resolve_default_model_for_agent('v3_bull_agent'))[1] == 'vllm-2'


@pytest.mark.asyncio
async def test_loaded_but_incapable_box_is_not_used(boxes, monkeypatch):
    async def capability(key, model):
        return {'eligible': key == 'dgx_spark', 'reason': 'tool parsing unavailable'}
    monkeypatch.setattr('app.services.model_capabilities.validate_endpoint', capability)
    assert (await routing.resolve_default_model_for_agent('v3_junior_analyst'))[1] == 'vllm-2'


@pytest.mark.asyncio
async def test_large_prompt_uses_endpoint_that_fits_not_role_preference(boxes):
    assert (await routing.resolve_default_model_for_agent(
        'v3_bull_agent', minimum_context_tokens=200000))[1] == 'vllm-2'
    boxes['dgx_spark'].requests_running = 6
    assert (await routing.resolve_default_model_for_agent(
        'v3_quant_analyst', minimum_context_tokens=200000))[1] == 'vllm-2'


@pytest.mark.asyncio
@pytest.mark.parametrize('capacity', [None, 10000])
async def test_unknown_or_insufficient_context_fails_before_dispatch(boxes, capacity):
    for ep in boxes.values():
        ep.max_model_len = capacity
    with pytest.raises(routing.ModelContractError, match='context capacity'):
        await routing.resolve_default_model_for_agent('v3_junior_analyst', minimum_context_tokens=20000)


@pytest.mark.asyncio
async def test_explicit_box_override_does_not_silently_change_boxes(boxes):
    with pytest.raises(routing.ModelContractError, match='context capacity'):
        await routing.resolve_default_model_for_agent('v3_junior_analyst',
            endpoint_override='jetson', minimum_context_tokens=200000)


@pytest.mark.asyncio
async def test_discovery_preserves_actionable_failure_reason(boxes, monkeypatch):
    from app.services.model_capabilities import discover_cycle_models

    async def discover(url, force_refresh=False):
        if 'dgx_spark' in url:
            raise routing.ModelUnavailableError('HTTP 502 during model discovery')
        return 'live-jetson'

    monkeypatch.setattr(routing, 'get_live_model_from_vllm', discover)
    receipt = await discover_cycle_models()
    assert receipt['eligible']
    failed = next(row for row in receipt['endpoints'] if row.get('box') == 'dgx_spark')
    assert not failed['eligible'] and 'HTTP 502' in failed['reason']


@pytest.mark.asyncio
async def test_base_agent_supplies_prompt_size_before_choosing_box(boxes, monkeypatch):
    from app.agents.base_agent import run_agent
    monkeypatch.setattr('app.services.context_gate.estimate_tokens', lambda text: 200000)
    monkeypatch.setattr('app.agents.tool_whitelists.get_agent_tools', lambda *a, **kw: [])
    chat = AsyncMock(return_value={'response': '{"result":"complete"}', 'tokens_used': 20,
                                  'loops_used': 1, 'model_used': 'live-dgx_spark',
                                  'provider': 'vllm-2', 'execution_ms': 1})
    monkeypatch.setattr(routing, 'chat_toolless', chat)
    result = await run_agent(agent_name='v3_junior_analyst', ticker='AMD',
        cycle_id='routing-large-prompt-test', bot_id='test', system_prompt='Check evidence.',
        user_prompt='Return JSON.', enable_tools=False)
    assert result['provider'] == 'vllm-2'
    assert chat.await_args.kwargs['provider'] == 'vllm-2'
