import pytest
from unittest.mock import patch, MagicMock

@pytest.mark.asyncio
async def test_planner_agent_bypasses_prism_routing():
    from app.agents.base_agent import run_agent
    from app.services.vllm_client import Priority
    
    with patch("app.agents.base_agent.settings") as mock_settings, \
         patch("app.agents.base_agent.llm") as mock_llm, \
         patch("app.tools.prism_agent_harness.run_prism_agent") as mock_run_prism_agent, \
         patch("app.agents.agent_loop.run_agent_loop") as mock_run_agent_loop, \
         patch("app.agents.agent_loop.run_split_agent_loop") as mock_run_split_loop:
         
        mock_settings.PRISM_ENABLED = True
        mock_settings.PRISM_AGENT_ROUTING = True
        
        # Mock prism client and health check
        mock_prism_client = MagicMock()
        mock_prism_client.check_health = MagicMock(return_value=True)
        mock_llm.prism_client = mock_prism_client
        mock_llm.get_model_context_window = MagicMock(return_value=128000)
        
        # Mock returns for both loops
        mock_run_agent_loop.return_value = {"final_text": "{}", "token_usage": 10, "execution_ms": 100}
        mock_run_split_loop.return_value = {"final_text": "{}", "token_usage": 10, "execution_ms": 100}
        
        await run_agent(
            agent_name="planner",
            ticker="TSLA",
            cycle_id="c123",
            bot_id="b123",
            system_prompt="sys",
            user_prompt="user",
            enable_tools=True,
        )
        
        # Verify run_prism_agent was bypassed and we fell back to local run loops
        mock_run_prism_agent.assert_not_called()
        assert mock_run_agent_loop.called or mock_run_split_loop.called
