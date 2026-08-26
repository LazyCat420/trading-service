"""The debate must be framed to the desk, and fair between the sides.

Guards the 2026-08-05 rework. Two defects were measured before it: the Bear
won 72-94% of 288 debates because it read the Bull's thesis, added risks the
Bull never answered, and had the last word; and every ticker got the same
generic "is this a buy" debate regardless of what was actually in question.
"""

import ast
from pathlib import Path

import pytest

from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS, get_agent_tools
from app.v3.artifacts import ARTIFACT_SCHEMAS, validate_artifact
from app.v3.debate_frame import (
    MAX_FRAMES,
    build_debate_frame_block,
    derive_debate_frame,
)

_ORCHESTRATOR = Path(__file__).resolve().parents[2] / "app" / "v3" / "orchestrator.py"


class _Desk:
    """Minimal SharedDesk stand-in — the framer reads attributes only."""

    def __init__(self, **kwargs):
        self.desk_note = None
        self.fundamental_report = None
        self.quant_report = None
        self.valuation_report = None
        self.cycle_metadata = {}
        for key, value in kwargs.items():
            setattr(self, key, value)


def _score(**kwargs) -> dict:
    return {"decision_score": {"band": "NEUTRAL", "coverage_pct": 80, **kwargs}}


# ─────────────────────────── framing ───────────────────────────


def test_solvency_leads_when_a_structural_gate_fails():
    """VNRX's shape: the balance sheet is the question, not the growth story."""
    desk = _Desk(
        cycle_metadata=_score(
            gates=[{"name": "liquidity", "verdict": "FAIL", "detail": "current ratio 0.333"}]
        )
    )
    frame = derive_debate_frame(desk)
    assert frame["keys"][0] == "SOLVENCY"
    assert "0.333" in frame["frames"][0]["because"]


def test_entry_quality_fires_when_direction_is_good_but_reward_is_not():
    """UBS's shape: right company, wrong price is its own question."""
    desk = _Desk(
        cycle_metadata=_score(risk_reward={"ratio": 0.68}),
        fundamental_report={"thesis_direction": "BULLISH"},
    )
    assert "ENTRY_QUALITY" in derive_debate_frame(desk)["keys"]


def test_entry_quality_stays_silent_when_nothing_is_constructive():
    """A poor R:R on a name nobody likes is not an entry-timing debate."""
    desk = _Desk(
        cycle_metadata=_score(risk_reward={"ratio": 0.68}),
        fundamental_report={"thesis_direction": "BEARISH"},
        quant_report={"thesis_direction": "BEARISH"},
    )
    assert "ENTRY_QUALITY" not in derive_debate_frame(desk)["keys"]


def test_desk_disagreement_is_named_rather_than_averaged():
    desk = _Desk(
        fundamental_report={"thesis_direction": "BULLISH"},
        quant_report={"thesis_direction": "BEARISH"},
    )
    assert "DESK_DISAGREEMENT" in derive_debate_frame(desk)["keys"]


def test_not_scoreable_becomes_a_data_sufficiency_debate():
    desk = _Desk(cycle_metadata=_score(band="NOT_SCOREABLE"))
    assert "DATA_SUFFICIENCY" in derive_debate_frame(desk)["keys"]


def test_an_empty_desk_still_gets_exactly_one_question():
    """The fallback is the question the unconditional debate always asked."""
    frame = derive_debate_frame(_Desk())
    assert frame["keys"] == ["THESIS_DURABILITY"]


def test_frames_are_capped_and_priority_ordered():
    """Beyond a few propositions the frame is a checklist, not a focus."""
    desk = _Desk(
        cycle_metadata=_score(
            band="NOT_SCOREABLE",
            risk_reward={"ratio": 0.5},
            gates=[{"name": "leverage", "verdict": "FAIL", "detail": "d/e 9"}],
        ),
        fundamental_report={"thesis_direction": "BULLISH"},
        quant_report={"thesis_direction": "BEARISH", "risk_metrics": {"rsi": 12}},
        valuation_report={"verdict": "UNDERVALUED", "margin_of_safety_pct": 40},
        desk_note={"catalyst_call": {"catalyst": "FDA decision", "already_priced_in": False}},
    )
    frame = derive_debate_frame(desk)
    assert len(frame["frames"]) == MAX_FRAMES
    assert frame["keys"][0] == "SOLVENCY"  # highest priority wins the lead
    assert frame["considered"] > MAX_FRAMES  # and the rest were genuinely dropped


def test_framing_is_deterministic():
    """Same desk, same debate — the trigger must be auditable after the fact."""
    desk = _Desk(
        cycle_metadata=_score(risk_reward={"ratio": 1.1}),
        fundamental_report={"thesis_direction": "BULLISH"},
    )
    assert derive_debate_frame(desk) == derive_debate_frame(desk)


def test_a_gate_that_passes_does_not_open_a_solvency_debate():
    """A check that fires for both states is not a check."""
    desk = _Desk(
        cycle_metadata=_score(
            gates=[
                {"name": "liquidity", "verdict": "PASS", "detail": "current ratio 2.1"},
                {"name": "leverage", "verdict": "UNKNOWN", "detail": "not on file"},
            ]
        )
    )
    assert "SOLVENCY" not in derive_debate_frame(desk)["keys"]


def test_block_is_empty_when_there_are_no_frames():
    """An empty block must not inject a heading with nothing under it."""
    assert build_debate_frame_block("AAA", {"frames": []}) == ""


def test_block_names_the_ticker_and_every_proposition():
    desk = _Desk(fundamental_report={"thesis_direction": "BULLISH"},
                 quant_report={"thesis_direction": "BEARISH"})
    frame = derive_debate_frame(desk)
    block = build_debate_frame_block("XYZ", frame)
    assert "XYZ" in block
    for key in frame["keys"]:
        assert key in block


# ─────────────────────────── fairness plumbing ───────────────────────────


def test_bull_defense_is_registered_and_validates():
    assert "bull_defense" in ARTIFACT_SCHEMAS
    artifact = {
        "summary": "s",
        "defense_points": [{"bear_claim_addressed": "a", "defense": "b"}],
        "concessions": [{"conceded_point": "c", "cost_to_thesis": "d"}],
        "final_confidence": 61,
    }
    assert validate_artifact("bull_defense", artifact) == []


def test_june_era_string_arrays_still_validate():
    """112 historical desks carry bare strings and are still replayed."""
    artifact = {
        "summary": "s",
        "defense_points": ["still holds"],
        "concessions": ["margin pressure is real"],
        "final_confidence": 55,
    }
    assert validate_artifact("bull_defense", artifact) == []


def test_bull_defense_gets_no_research_tools():
    """A defense that fetches NEW evidence makes an argument the Bear cannot
    answer — the asymmetry this turn exists to remove. Whiteboard-only.

    Note the two conventions that nearly collided here: `[]` in this map means
    "no tools", but `[]` in an agent module means UNSCOPED to prism and grants
    the whole catalog (see test_no_v3_whitelist_is_empty).
    """
    from app.v3.agents import bull_defense

    # get_agent_tools returns resolved tool SPECS, not bare names.
    granted = {
        (t.get("function", {}) or {}).get("name") or t.get("name")
        for t in get_agent_tools("v3_bull_defense")
    }
    assert granted, "empty grant would route through the unknown-agent error path"
    assert granted == {"whiteboard_read"}
    assert AGENT_TOOL_WHITELISTS["v3_bull_defense"] == ["whiteboard_read"]
    # The two copies have different consumers (runtime grant vs prism
    # registration) and must not drift apart.
    assert set(bull_defense.TOOL_WHITELIST) == granted
    assert not (granted & {"lazy_web_search", "get_market_data", "get_sec_filings"})


def _orchestrator_tree() -> ast.Module:
    return ast.parse(_ORCHESTRATOR.read_text())


def _run_counts_keys(tree: ast.Module) -> set[str]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "run_counts" in targets and isinstance(node.value, ast.Dict):
            return {
                k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    raise AssertionError("run_counts dict literal not found in orchestrator")


def _queued_agent_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_queue_agent"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def test_every_queued_agent_has_a_run_counter():
    """The scheduler does `run_counts[name] += 1` — a missing key is a KeyError
    that kills the whole desk, not a missing statistic. Restoring bull_defense
    without its counter would have done exactly that."""
    tree = _orchestrator_tree()
    missing = _queued_agent_names(tree) - _run_counts_keys(tree)
    assert not missing, f"queued with no run_counts entry: {sorted(missing)}"


def test_bull_defense_is_actually_queued_and_counted():
    tree = _orchestrator_tree()
    assert "bull_defense" in _queued_agent_names(tree)
    assert "bull_defense" in _run_counts_keys(tree)


@pytest.mark.parametrize("phase", ["bull_defense", "debate_judge"])
def test_debate_phases_are_reachable_from_the_source(phase):
    """Both turns must exist as task-loop branches, or the queue silently
    no-ops and the desk stalls without a verdict."""
    source = _ORCHESTRATOR.read_text()
    assert f'name == "{phase}"' in source


# ─────────────────────────── held positions are exit decisions ───────────────


def test_a_held_position_reframes_the_whole_debate():
    """Measured 2026-08-05: every re-look of a held name reasoned about ENTRY
    ("wait for trend confirmation before re-engaging" — HOOD, on a position the
    book owned) and so returned HOLD, which for a held name means KEEP. Zero
    SELLs in 14 days. The exit question must lead."""
    desk = _Desk(
        cycle_metadata={
            "held": True,
            "position": {"held": True, "unrealized_pnl_pct": -8.4, "holding_days": 12},
            **_score(gates=[{"name": "liquidity", "verdict": "FAIL", "detail": "cr 0.3"}]),
        }
    )
    frame = derive_debate_frame(desk)
    assert frame["keys"][0] == "POSITION_REVIEW", (
        "the exit question must outrank every entry-shaped question"
    )
    lead = frame["frames"][0]
    assert "KEEP" in lead["proposition"] and "EXIT" in lead["proposition"]
    assert "-8.4" in lead["because"]


def test_entry_quality_is_never_asked_about_a_position_we_own():
    """'Is the entry acceptable?' is not a question about committed capital."""
    metadata = {
        "held": True,
        "position": {"held": True},
        **_score(risk_reward={"ratio": 0.68}),
    }
    desk = _Desk(cycle_metadata=metadata, fundamental_report={"thesis_direction": "BULLISH"})
    assert "ENTRY_QUALITY" not in derive_debate_frame(desk)["keys"]

    # …but it is exactly the right question when the book is flat.
    flat = _Desk(
        cycle_metadata=_score(risk_reward={"ratio": 0.68}),
        fundamental_report={"thesis_direction": "BULLISH"},
    )
    assert "ENTRY_QUALITY" in derive_debate_frame(flat)["keys"]


def test_position_review_survives_a_metadata_only_held_flag():
    """`position` is new; older desks carry only the `held` boolean."""
    desk = _Desk(cycle_metadata={"held": True})
    assert derive_debate_frame(desk)["keys"][0] == "POSITION_REVIEW"


def test_a_flat_desk_never_opens_a_position_review():
    """A check that fires for both states is not a check."""
    assert "POSITION_REVIEW" not in derive_debate_frame(_Desk())["keys"]
    assert "POSITION_REVIEW" not in derive_debate_frame(
        _Desk(cycle_metadata={"held": False, "position": {"held": False}})
    )["keys"]


def test_the_deciding_prompts_frame_the_exit_symmetrically():
    """The not-held branch always stated a hard constraint; the held branch
    stated a bare fact. That asymmetry is why nothing ever sold."""
    from app.v3.agents import board_of_directors, decision_agent, delta_analyst

    for module in (board_of_directors, decision_agent):
        prompt = getattr(module, "SYSTEM_PROMPT", "") or getattr(
            module, "_BOARD_COMMON", ""
        )
        source = (prompt + open(module.__file__).read()).lower()
        assert "already holds this ticker" in source
        # The exit must be named as an available action, not merely mentioned.
        assert "sell" in source and "exit" in source
        assert "thesis, not on its p&l" in source
        # …and must not overcorrect into a sell bias.
        assert "overcorrect" in source

    delta_src = open(delta_analyst.__file__).read()
    assert "ALREADY HOLDS THIS TICKER" in delta_src
    assert "REAFFIRM then means KEEP" in delta_src


# ─────────────────────────── turn budgets ───────────────────────────


def test_a_tool_carrying_agent_never_has_a_single_turn_budget():
    """agent_runner sets `enable_tools=bool(tool_whitelist)`, so ANY non-empty
    whitelist turns tools on — and a budget of 1 then gives the agent one
    iteration total. Spend it on a tool call and there is no turn left to emit
    the artifact.

    This is not hypothetical: v3_bull_defense shipped with whiteboard_read and
    a budget of 1 and returned 49- and 94-char non-artifacts for RNGR within
    the hour.
    """
    import importlib
    import pkgutil

    import app.v3.agents as pkg
    from app.agents.tool_whitelists import AGENT_BUDGET_OVERRIDES

    offenders = []
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"app.v3.agents.{mod_info.name}")
        name = getattr(module, "AGENT_NAME", None)
        whitelist = getattr(module, "TOOL_WHITELIST", None)
        if not name or not whitelist:
            continue  # tool-less agents legitimately run on a single turn
        budget = AGENT_BUDGET_OVERRIDES.get(name)
        if budget is not None and budget < 2:
            offenders.append(f"{name}: {len(whitelist)} tool(s), budget {budget}")
    assert not offenders, (
        "a tool-carrying agent needs one turn to call and one to answer: "
        + "; ".join(offenders)
    )


def test_every_agents_numbered_rules_number_once():
    """The judge's CRITICAL RULES ran 1-8 and then a second '5.' — a leftover
    from inserting rules 5-8 without renumbering the tail (found by the
    2026-08-26 behavioral audit). A duplicated number makes 'rule 5' ambiguous
    to the model and to every prompt edit that cites it. Sweep all agents:
    within any one numbered list, no leading integer may repeat."""
    import importlib
    import pkgutil
    import re

    import app.v3.agents as pkg

    offenders = []
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"app.v3.agents.{mod_info.name}")
        prompt = getattr(module, "SYSTEM_PROMPT", "") or ""
        seen: set[int] = set()
        for line in prompt.splitlines():
            m = re.match(r"^(\d+)\. ", line)
            if not m:
                continue
            n = int(m.group(1))
            if n in (0, 1):
                seen = set()  # a fresh list restarts the numbering
            if n in seen:
                offenders.append(f"{mod_info.name}: rule number {n} repeats")
            seen.add(n)
    assert not offenders, "; ".join(offenders)
