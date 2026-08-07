"""The agent's tool declaration picks its transport.

WHY. The transport was hardcoded per call site, so the declaration and the
route could disagree with nothing to reconcile them. The gatekeeper is the
proof: its system prompt (rule 6) tells it to call `get_parameters`, its
TOOL_WHITELIST carries 14 tools, and its call site passed
`enable_tools=False`. It could never have called anything, and no test could
see the contradiction because no single place read both.

MEASURED 2026-08-06, n=10 interleaved on replayed v3_regime_engine prompts:

    /chat          10/10 non-empty  10/10 valid  16.2s  ttft 2.9s
    /agent         10/10             8/10        68.1s  ttft 8.4s
    /agent+tools    9/10             8/10        45.4s  ttft 8.8s

so a tool-less role pays ~5.5s of TTFT and two artifact failures in ten for a
catalog it never touches.
"""

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from app.agents import base_agent
from app.agents.base_agent import transport_for


class TestDeclarationPicksTheRoute:
    def test_declared_tools_go_to_agent(self):
        assert transport_for(True, [{"name": "get_market_data"}]) == "agent"

    def test_no_tools_goes_to_chat(self):
        assert transport_for(False, []) == "chat"

    def test_tools_disabled_goes_to_chat_even_with_a_whitelist(self):
        """`enable_tools=False` is the caller saying 'not this run'."""
        assert transport_for(False, [{"name": "get_market_data"}]) == "chat"

    def test_enabled_but_empty_whitelist_goes_to_chat(self):
        """An empty whitelist is a role with nothing to call, NOT 'all tools'.

        Prism treats empty availableTools as UNSCOPED — full-catalog discovery
        headroom — so reading it as 'enable everything' is how agents reached
        execute_command. Here it simply means there is nothing to route for.
        """
        assert transport_for(True, []) == "chat"
        assert transport_for(True, None) == "chat"


class TestFailSafeDirection:
    """When the two inputs disagree, prefer the route that cannot lose tools.

    A tool-using agent sent to /chat silently loses its tools. A tool-less
    agent sent to /agent is merely slower. Only one of those is a correctness
    bug, so `agent` is the only value returned when tools are actually present
    AND enabled.
    """

    @pytest.mark.parametrize("enable,tools,expected", [
        (True, [{"name": "x"}], "agent"),
        (True, [], "chat"),
        (False, [{"name": "x"}], "chat"),
        (False, [], "chat"),
    ])
    def test_truth_table_is_pinned(self, enable, tools, expected):
        assert transport_for(enable, tools) == expected

    def test_agent_is_only_reachable_with_real_tools(self):
        """No input combination reaches /agent without a non-empty tool list."""
        for enable in (True, False):
            for tools in ([], None):
                assert transport_for(enable, tools) == "chat"


class TestItIsWiredIntoTheRealCallPath:
    """A correct helper nobody calls routes nothing."""

    def test_run_agent_consults_the_helper(self):
        src = inspect.getsource(base_agent.run_agent)
        assert 'transport_for(enable_tools, agent_tools) == "chat"' in src

    def test_the_chat_branch_uses_the_shared_transport(self):
        """Not a second copy of the SSE loop — one implementation."""
        src = inspect.getsource(base_agent.run_agent)
        assert "chat_toolless" in src

    def test_the_chat_branch_is_fail_closed_on_harness_errors(self):
        """Prism returns harness errors as ordinary assistant text; without
        this the error string is booked as the agent's artifact."""
        src = inspect.getsource(base_agent.run_agent)
        chat_branch = src[src.index('transport_for(enable_tools, agent_tools) == "chat"'):]
        assert "_FAILURE_MARKERS" in chat_branch

    def test_the_chat_branch_floors_the_token_budget(self):
        """Prism's ContextExhaustionGuard rejects budgets under 4096."""
        src = inspect.getsource(base_agent.run_agent)
        assert "max(4096, int(max_tokens or 8192))" in src

    def test_the_chat_branch_reports_one_loop(self):
        """/chat is single-shot. Inflating the count would corrupt the loop
        statistics the box comparison is built on."""
        src = inspect.getsource(base_agent.run_agent)
        chat_branch = src[src.index('transport_for(enable_tools, agent_tools) == "chat"'):]
        assert "single-shot" in chat_branch


class TestTheRouteIsTakenNotJustChosen:
    """The class above asserts on source text; these drive the real function.

    A source assertion cannot tell a route that is computed from one that is
    taken — and the transport reroute's first casualty was a test suite that
    passed while making live network calls, because it patched only the seam
    the code no longer used.
    """

    @staticmethod
    async def _run(enable_tools: bool, tools: list | None):
        from app.agents.base_agent import run_agent

        with patch(
            "lazycat.agent.AgentHarness"
        ) as harness_cls, patch(
            "app.services.prism_agent_caller.chat_toolless",
            new_callable=AsyncMock,
            return_value={
                "response": '{"ok": true}', "tokens_used": 1, "loops_used": 1,
                "model_used": "m", "provider": "vllm", "execution_ms": 5,
            },
        ) as chat, patch(
            "app.agents.tool_whitelists.get_agent_tools", return_value=tools or [],
        ), patch(
            "app.services.prism_agent_caller.resolve_default_model_for_agent",
            new_callable=AsyncMock,
            return_value=("cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit", "vllm"),
        ):
            harness_run = AsyncMock(return_value='{"ok": true}')
            harness_cls.return_value.run = harness_run
            await run_agent(
                agent_name="v3_junior_analyst", ticker="_AUDIT_TEST",
                cycle_id="cycle-test", bot_id="bot-test",
                system_prompt="s", user_prompt="u", enable_tools=enable_tools,
            )
        return chat.await_count, harness_run.await_count

    @pytest.mark.asyncio
    async def test_a_declared_tool_actually_reaches_the_harness(self):
        chat_calls, harness_calls = await self._run(True, [{"name": "get_market_data"}])

        assert harness_calls == 1, "a tool-using agent must run the agentic loop"
        assert chat_calls == 0, "and must not be silently stripped of its tools"

    @pytest.mark.asyncio
    async def test_a_tool_less_call_actually_reaches_chat(self):
        chat_calls, harness_calls = await self._run(False, [])

        assert chat_calls == 1
        assert harness_calls == 0

    @pytest.mark.asyncio
    async def test_an_empty_whitelist_does_not_reach_the_harness(self):
        """`enable_tools=True` with nothing to call is the case the truth table
        settles in the abstract; this is it happening."""
        chat_calls, harness_calls = await self._run(True, [])

        assert chat_calls == 1
        assert harness_calls == 0


class TestTheGatekeeperIsStillOutsideThisRule:
    """The role that motivated the change is the one it does not govern.

    `transport_for` derives the route inside `run_agent`. The gatekeeper does
    not call `run_agent` — `pipeline_service` calls `chat_toolless` directly
    (1755c3d, a parallel session) — so its route is still hardcoded, and its
    own declaration still disagrees with it: 13 tools whitelisted, a system
    prompt whose rule 6 tells it to call `get_parameters`, and a transport
    that cannot carry a tool call.

    That is not a bug being hidden, it is the state of play: /chat is what
    made the gatekeeper work again, and nothing has measured it WITH tools.
    These tests exist so the contradiction is visible and asserted rather than
    resting on a commit message, and so that whichever way it is resolved —
    routing it through run_agent, or emptying the whitelist — a test has to be
    updated deliberately.
    """

    def test_the_gatekeeper_declares_tools(self):
        from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

        assert AGENT_TOOL_WHITELISTS.get("v3_portfolio_manager"), (
            "if this is now empty, the declaration finally matches the /chat "
            "route — delete this class and say so in 02-current-state.md"
        )

    def test_by_its_declaration_it_would_route_to_agent(self):
        from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

        tools = list(AGENT_TOOL_WHITELISTS["v3_portfolio_manager"])
        assert transport_for(True, tools) == "agent"

    def test_but_the_call_site_bypasses_the_rule_entirely(self):
        """It calls the transport helper directly, so `transport_for` never
        runs for this agent. Pinned because the fix and the bypass shipped
        within hours of each other and only one of them is documented."""
        from app.services import pipeline_service

        src = inspect.getsource(pipeline_service.PipelineService)
        assert "chat_toolless(" in src
        assert "transport_for(" not in src

    def test_so_the_shadow_compares_boxes_and_not_transports(self):
        """Both sides tool-less is the ONE thing that keeps the comparison
        honest: `model_shadow._prism_chat` is /chat, and so is the primary.

        The cost is that n=10 gatekeeper rows will still describe a tool-less
        job — the same limitation every prior box comparison had — so they
        answer 'which box' and not 'what does the catalog cost'.
        """
        from app.v3 import model_shadow

        src = inspect.getsource(model_shadow._prism_chat)
        assert "chat_toolless" in src
