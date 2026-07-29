"""Tools removed because a precomputed block already answers them.

A block that is injected into an agent's prompt and a tool that fetches the
same quantity are not complementary — the tool is a slower second route to a
number the agent already holds, and it costs a TURN. That matters here because
six of the ten V3 agents average loops_used = 1.00 against a 7-turn budget:
they emit their JSON on the first pass, so a turn spent re-fetching a value
already in the prompt is most of the budget they ever use.

Measured 2026-07-28: `get_finviz_fundamentals` kept firing on the fundamental
analyst for weeks AFTER fundamental_context made it redundant — step 2 of that
prompt had explicitly said "do NOT spend a turn on get_finviz_fundamentals"
since the block shipped, and the calls stopped only when the NAME left the
whitelist and the prompt text. A tool named in a prompt outlives the block that
replaced it.

Three things must hold together, and each is pinned below:
  1. The tool is gone from that agent's whitelist.
  2. The superseding block STILL REACHES that agent — the guards in
     app/v3/agent_runner.py are per-agent, and removing a tool from an agent a
     block does not reach makes it blind instead of efficient. This is why
     v3_valuation_analyst, v3_delta_analyst, v3_regime_engine and the two
     worker agents KEPT their tools.
  3. No whitelist is empty. prism_registration reads an empty TOOL_WHITELIST as
     UNSCOPED FULL CATALOG (documented at app/v3/agents/valuation_analyst.py's
     TOOL_WHITELIST) — emptying one would grant every tool in the system, the
     exact opposite of the intent.
"""

import re
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[2] / "app" / "v3" / "agent_runner.py"


# (agent, tool it lost, the cycle_metadata key that replaced it)
SUPERSEDED = [
    ("v3_board_of_directors", "get_technical_indicators", "technical_baseline_context"),
    ("v3_board_of_directors", "get_finviz_fundamentals", "fundamental_context"),
    ("v3_board_of_directors", "calculate_hrp_allocation", "quant_math_context"),
    ("v3_quant_analyst", "get_technical_indicators", "technical_baseline_context"),
    ("v3_quant_analyst", "calculate_hrp_allocation", "quant_math_context"),
    ("v3_quant_analyst", "forecast_volatility_garch", "quant_math_context"),
    ("v3_fundamental_analyst", "get_finviz_fundamentals", "fundamental_context"),
]


@pytest.mark.parametrize("agent,tool,_block", SUPERSEDED)
def test_superseded_tool_is_not_whitelisted(agent, tool, _block):
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    assert tool not in AGENT_TOOL_WHITELISTS.get(agent, []), (
        f"{agent} still whitelists {tool}, which returns what the precomputed "
        f"block already puts in its prompt — it will spend a turn on it."
    )


@pytest.mark.parametrize("agent,tool,_block", SUPERSEDED)
def test_the_prompt_no_longer_names_the_removed_tool(agent, tool, _block):
    """A prompt naming a tool the agent cannot call is WORSE than the tool.

    The agent burns its one turn on a call that comes back "not available"
    instead of on analysis. Every removal above had to be paired with a prompt
    edit; this pins that pairing so a future removal cannot skip it.
    """
    import importlib
    import pkgutil

    import app.v3.agents as pkg

    for mod_info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"app.v3.agents.{mod_info.name}")
        if getattr(module, "AGENT_NAME", None) != agent:
            continue
        prompts = [
            v for k, v in vars(module).items()
            if isinstance(v, str) and ("PROMPT" in k or k.startswith("_BOARD"))
        ]
        for text in prompts:
            assert tool not in text, (
                f"{agent}'s {mod_info.name} prompt still names `{tool}`, which "
                f"is no longer in its whitelist."
            )
        return
    pytest.fail(f"No app/v3/agents module declares AGENT_NAME = {agent!r}")


@pytest.mark.parametrize("agent,_tool,block", SUPERSEDED)
def test_the_superseding_block_still_reaches_that_agent(agent, _tool, block):
    """The removal is only safe while the block is actually injected.

    Parsed out of agent_runner.py source rather than executed: the injection
    site is a chain of `if agent_name in (...)` guards around
    `desk.cycle_metadata.get("<block>")`, and building a real SharedDesk here
    would need a database. What can silently break is someone narrowing a guard
    — that is a source-level edit, and this catches it.
    """
    src = _RUNNER.read_text()
    idx = src.find(f'cycle_metadata.get("{block}"')
    assert idx != -1, f"{block} is no longer read in agent_runner.py at all"

    # Walk back to the guard that encloses this read.
    head = src[:idx]
    guard_start = max(head.rfind("if agent_name in ("), head.rfind("if agent_name =="))
    assert guard_start != -1, f"{block} read has no agent_name guard before it"
    guard = src[guard_start:idx]
    names = set(re.findall(r'"(v3_[a-z_]+)"', guard))
    assert agent in names, (
        f"{block} no longer reaches {agent} (guard covers {sorted(names)}) — "
        f"its tool was removed on the assumption that it does."
    )


def test_no_v3_whitelist_is_empty():
    """An empty TOOL_WHITELIST grants the FULL catalog, not zero tools.

    prism_registration treats [] as unscoped. A trimming pass that emptied one
    of these would hand that agent every tool in the system.
    """
    import importlib
    import pkgutil

    import app.v3.agents as pkg

    for mod_info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"app.v3.agents.{mod_info.name}")
        wl = getattr(module, "TOOL_WHITELIST", None)
        if wl is None:
            continue
        assert len(wl) > 0, (
            f"app/v3/agents/{mod_info.name}.py has an EMPTY TOOL_WHITELIST — "
            f"prism will grant it the entire catalog."
        )


def test_agents_a_block_does_not_reach_keep_their_tools():
    """The refusals, pinned so a later pass does not 'finish the job'.

    Each of these was proposed for removal on 2026-07-28 and REFUSED because
    the superseding block does not reach that agent:
      - v3_valuation_analyst gets valuation_context (multiples, reverse DCF),
        NOT fundamental_context — the runner guard for the fundamental block
        lists only the fundamental analyst, the board and the synthesizer. Its
        prompt uses get_finviz_fundamentals as a gap-filler for fields the
        valuation block does not carry.
      - v3_delta_analyst runs in the pre-panel delta tier, before any of these
        blocks are built, and no guard names it.
      - v3_regime_engine classifies MARKET state from macro_briefing; the
        technical baseline is per-analysed-ticker and never reaches it.
    """
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    for agent, tool in [
        ("v3_valuation_analyst", "get_finviz_fundamentals"),
        ("v3_delta_analyst", "get_technical_indicators"),
        ("v3_regime_engine", "get_technical_indicators"),
        ("v3_worker_fundamental", "get_finviz_fundamentals"),
        ("v3_worker_quant", "get_technical_indicators"),
    ]:
        assert tool in AGENT_TOOL_WHITELISTS.get(agent, []), (
            f"{agent} lost {tool}, but no precomputed block reaches it — it is "
            f"now blind to that data rather than efficient."
        )


def test_get_portfolio_state_survives_the_portfolio_context_block():
    """portfolio_context does NOT supersede get_portfolio_state.

    It looks like it should — it reaches every agent with no filter. But it is
    a SINGLE-TICKER position line built by get_position_context(): held or not
    held, entry price, P&L, holding days. get_portfolio_state returns the whole
    book: cash, every open position with market value, and total equity. An
    agent sizing a trade against available cash cannot get that from the block,
    and the board's prompt asks for it by name ("only when existing exposure
    would change sizing").
    """
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    for agent in ("v3_board_of_directors", "v3_quant_analyst", "v3_delta_analyst"):
        assert "get_portfolio_state" in AGENT_TOOL_WHITELISTS.get(agent, []), (
            f"{agent} lost get_portfolio_state; portfolio_context carries only "
            f"this ticker's position, never cash or the rest of the book."
        )
