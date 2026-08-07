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


class TestGatekeeperIsShadowable:
    """It does not go through agent_runner, so it was structurally unshadowable
    — MODEL_SHADOW_AGENTS=v3_portfolio_manager did nothing, silently."""

    def test_pipeline_service_dispatches_a_gatekeeper_shadow(self):
        from app.services import pipeline_service

        src = inspect.getsource(pipeline_service)
        assert "dispatch_shadow(" in src
        assert "shadow_endpoint_for(AGENT_NAME)" in src

    def test_the_shadow_cannot_break_the_cycle(self):
        from app.services import pipeline_service

        src = inspect.getsource(pipeline_service)
        idx = src.index("shadow_endpoint_for(AGENT_NAME)")
        window = src[idx - 1200:idx + 2000]
        assert "except Exception" in window, "an unguarded bench can kill a cycle"

    def test_it_only_shadows_a_usable_result(self):
        """Shadowing a run the pipeline could not handle compares the boxes on
        an input that was already broken."""
        from app.services import pipeline_service

        src = inspect.getsource(pipeline_service)
        assert 'if result and result.get("response"):' in src
