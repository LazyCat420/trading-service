"""
Tests for agent-driven topology controls (plan Sections 2.2/2.3/2.4, 6.1).

The Junior Analyst can now recommend pipeline depth (triage_recommendation),
the Quant Analyst can surface unresolved questions (sub_analyses_requested),
research agents can reach the whiteboard + peer-request tools, and the
scheduler records an iteration log.
"""
from app.v3.artifacts import validate_artifact
from app.v3.shared_desk import SharedDesk


def test_desk_note_schema_accepts_triage_recommendation():
    artifact = {
        "summary": "s", "key_findings": [], "data_gaps": [], "confidence": 50,
        "triage_recommendation": "QUANT_ONLY",
    }
    assert validate_artifact("desk_note", artifact) == []


def test_desk_note_schema_rejects_bad_triage_value():
    artifact = {
        "summary": "s", "key_findings": [], "data_gaps": [], "confidence": 50,
        "triage_recommendation": "MAYBE",
    }
    errors = validate_artifact("desk_note", artifact)
    assert any("triage_recommendation" in e for e in errors)


def test_quant_open_questions_render_in_desk_context():
    desk = SharedDesk(ticker="TEST", cycle_id="c1")
    desk.append_artifact("quant_report", {
        "summary": "numbers",
        "risk_metrics": {"rsi": 55},
        "thesis_direction": "NEUTRAL",
        "confidence": 60,
        "sub_analyses_requested": [
            "What happened during the last 3 earnings surprises?",
        ],
    })
    ctx = desk.get_compressed_context()
    assert "Open questions the Quant could not resolve" in ctx
    assert "earnings surprises" in ctx


def test_judge_nuance_fields_render_in_debate_context():
    desk = SharedDesk(ticker="TEST", cycle_id="c1")
    desk.append_artifact("debate_judge", {
        "summary": "bull wins",
        "winner": "bull",
        "final_confidence": 70,
        "weaknesses_of_winner": ["sector-wide margin compression ignored"],
        "strongest_point_of_loser": "insider selling accelerated last month",
    })
    ctx = desk.get_compressed_context(include_debate=True)
    assert "margin compression" in ctx
    assert "Loser's best point" in ctx


def test_research_agents_have_collaboration_tools():
    from app.v3.agents import junior_analyst, fundamental_analyst, quant_analyst

    assert "whiteboard_read" in junior_analyst.TOOL_WHITELIST
    assert "whiteboard_read" in fundamental_analyst.TOOL_WHITELIST
    assert "whiteboard_read" in quant_analyst.TOOL_WHITELIST
    assert "request_peer_analysis" in fundamental_analyst.TOOL_WHITELIST
    assert "request_peer_analysis" in quant_analyst.TOOL_WHITELIST


def test_debate_agents_have_small_verification_toolset():
    from app.v3.agents import bull_agent, bear_agent

    for mod in (bull_agent, bear_agent):
        assert set(mod.TOOL_WHITELIST) == {"lazy_web_search", "get_market_data"}
