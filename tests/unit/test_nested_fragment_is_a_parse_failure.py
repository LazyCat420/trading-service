"""A nested block of an artifact must never be mistaken for the artifact.

Production, 2026-08-04, cycle-v3-1785818847 / TSM: the fundamental analyst's
outer JSON object did not parse, the extractor walked into it, and the run came
back holding the artifact's trailing `metrics` block alone — all 17 of its keys,
none of the 5 the schema requires. That is valid JSON, so the tool-less repair
pass never fired; the run was graded a collapse and re-ran with tools, spending
99.7s and 37.7k tokens re-deriving research it had already produced. Two
v3_quant_analyst runs the same day failed identically, returning one entry from
their trailing `overlays` array (['reasoning','type','x0','x1','y0','y1']).
"""

import json
import pytest

from app.v3.agent_runner import _is_wrong_shape, _parse_artifact
from app.utils.text_utils import _malformed_fallback, parse_json_response


FUNDAMENTAL = {
    "summary": "TSM trades at 18x forward earnings [finviz].",
    "pillars": {"revenue_growth": "+38% YoY", "valuation": "18x fwd vs sector 24x"},
    "thesis_direction": "BULLISH",
    "confidence": 72,
    "positioning_read": {"congress_disclosures_90d": 5, "stance": "SUPPORTS_BULL"},
    "metrics": {"pe_ratio": 21.4, "forward_pe": 18.0, "roe": 0.31, "roa": 0.19},
}
DECISION = {
    "action": "BUY",
    "confidence": 78,
    "reasoning": "Foundry lead intact and the weekly setup is constructive.",
    "entry_plan": {"limit": 142.0, "stop": 133.5, "target": 168.0},
}


def _truncate(obj) -> str:
    """The output ceiling ate the closing brace and nothing else."""
    text = json.dumps(obj, indent=2)
    return text[: text.rstrip().rfind("}")]


# ── the shape guard ──────────────────────────────────────────────────────
def test_the_metrics_block_is_not_a_fundamental_report():
    assert _is_wrong_shape("fundamental_report", FUNDAMENTAL["metrics"])


def test_an_overlays_entry_is_not_a_quant_report():
    overlay = {"type": "trendline", "x0": "2026-05-10", "y0": 138.0,
               "x1": "2026-07-18", "y1": 150.0, "reasoning": "Ascending support"}
    assert _is_wrong_shape("quant_report", overlay)


def test_a_real_artifact_is_never_wrong_shape():
    assert not _is_wrong_shape("fundamental_report", FUNDAMENTAL)
    assert not _is_wrong_shape("final_decision", DECISION)


def test_a_degraded_but_real_artifact_is_not_wrong_shape():
    # One surviving required field is enough — grading that is the collapse
    # branch's job, not this guard's.
    assert not _is_wrong_shape("fundamental_report", {"confidence": 40})


def test_unknown_artifact_types_are_never_narrowed():
    assert not _is_wrong_shape("not_a_real_type", {"anything": 1})


# ── end to end through the parser ────────────────────────────────────────
MANGLES = [
    (lambda o: _truncate(o), "truncated at the token ceiling"),
    (lambda o: _truncate(o) + ",\n}", "trailing comma"),
    (lambda o: json.dumps(o, indent=2).replace("18x forward", '"18x" forward', 1),
     "unescaped quote in the prose"),
]


@pytest.mark.parametrize("mangle,label", MANGLES)
def test_a_malformed_fundamental_report_never_reaches_the_desk_as_metrics(mangle, label):
    """Whatever the extractor hands back, the runner must recognise it as not
    being the artifact — that is what routes it to repair instead of to schema
    validation and a full tool-enabled re-run."""
    got = _parse_artifact(mangle(FUNDAMENTAL), "fundamental_report",
                          "v3_fundamental_analyst")
    assert got is None or _is_wrong_shape("fundamental_report", got), (
        f"{label}: {sorted(got)} was accepted as a fundamental_report"
    )


def test_a_valid_report_is_untouched():
    got = _parse_artifact(json.dumps(FUNDAMENTAL, indent=2),
                          "fundamental_report", "v3_fundamental_analyst")
    assert got == FUNDAMENTAL


# ── the routing, end to end through the runner ───────────────────────────
@pytest.mark.parametrize("mangle,label", MANGLES)
@pytest.mark.asyncio
async def test_the_repair_pass_recovers_the_run_without_a_tooled_rerun(mangle, label):
    """The payoff. On TSM this path cost 99.7s and 37.7k tokens of tool-enabled
    re-run; it should cost one tool-less repair call."""
    from unittest.mock import AsyncMock, patch
    from app.v3.agent_runner import run_v3_agent
    from app.v3.shared_desk import SharedDesk, PhaseOutcome

    class _FundamentalAgent:
        AGENT_NAME = "v3_fundamental_analyst"
        ARTIFACT_TYPE = "fundamental_report"
        TOOL_WHITELIST = ["get_sec_filings"]
        SYSTEM_PROMPT = "You are the fundamental analyst. Output JSON."

    calls = []

    async def _run(**kwargs):
        calls.append(kwargs)
        # First call: the mangled artifact. The repair call gets it right.
        body = json.dumps(FUNDAMENTAL) if len(calls) > 1 else mangle(FUNDAMENTAL)
        return {"response": body, "tokens_used": 1000, "loops_used": 6,
                "stop_reason": "completed"}

    desk = SharedDesk(ticker="TSM", cycle_id="cycle-test")
    desk.cycle_metadata = {"ticker": "TSM", "agent_locale": "default"}
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(desk=desk, agent_module=_FundamentalAgent,
                                     cycle_id="cycle-test", bot_id="b1")

    assert outcome == PhaseOutcome.SUCCESS, f"{label}: {outcome}"
    assert len(calls) == 2, f"{label}: expected one repair call, got {calls}"
    assert calls[1]["enable_tools"] is False, "the repair pass must be tool-less"
    assert desk.fundamental_report["summary"] == FUNDAMENTAL["summary"]
    assert not desk.fundamental_report.get("_degraded")


@pytest.mark.asyncio
async def test_a_fragment_is_graded_not_discarded_when_repair_fails():
    """If repair does not land, the fragment must come back and be graded by
    the collapse branches — AGENT_ERROR, which earns the breaker's retry. A
    hard 'no parseable artifact' here would return AGENT_ERROR on the retry
    too, and two of those abort the whole ticker."""
    from unittest.mock import AsyncMock, patch
    from app.v3.agent_runner import run_v3_agent
    from app.v3.shared_desk import SharedDesk, PhaseOutcome

    class _FundamentalAgent:
        AGENT_NAME = "v3_fundamental_analyst"
        ARTIFACT_TYPE = "fundamental_report"
        TOOL_WHITELIST = ["get_sec_filings"]
        SYSTEM_PROMPT = "You are the fundamental analyst. Output JSON."

    async def _run(**kwargs):
        return {"response": _truncate(FUNDAMENTAL), "tokens_used": 1000,
                "loops_used": 6, "stop_reason": "completed"}

    desk = SharedDesk(ticker="TSM", cycle_id="cycle-test")
    desk.cycle_metadata = {"ticker": "TSM", "agent_locale": "default"}
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(desk=desk, agent_module=_FundamentalAgent,
                                     cycle_id="cycle-test", bot_id="b1")

    assert outcome == PhaseOutcome.AGENT_ERROR
    outcome_retry = None
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome_retry = await run_v3_agent(desk=desk, agent_module=_FundamentalAgent,
                                           cycle_id="cycle-test", bot_id="b1",
                                           is_retry=True)
    assert outcome_retry == PhaseOutcome.DATA_GAP, (
        "the retry must degrade, never return a second AGENT_ERROR"
    )


# ── the decision path ────────────────────────────────────────────────────
def test_a_broken_decision_is_never_rebuilt_by_regex():
    """The prose fallback can synthesise action + confidence + reasoning —
    the complete required set for final_decision. Pointed at a decision whose
    JSON merely failed to parse, it would return a schema-clean BUY nobody
    voted for."""
    text = _truncate(DECISION)
    assert _malformed_fallback(text) is None
    got = _parse_artifact(text, "final_decision", "v3_decision_synthesizer")
    assert got is None or "action" not in got, f"manufactured a decision: {got}"


def test_the_prose_fallback_still_handles_a_markdown_report():
    """Its real job — a persona that ignored the JSON instruction — must
    still work, or every such run becomes a hard failure."""
    report = (
        "## Recommendation: **HOLD**\n\n"
        "| Metric | Value |\n| Confidence | 62 |\n\n"
        "## Rationale\nThe setup is unresolved and the tape is choppy, so we "
        "stand aside until the earnings print lands.\n"
    )
    got = parse_json_response(report)
    assert got.get("action") == "HOLD"
    assert got.get("confidence") == 62
