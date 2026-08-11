"""
Flow-graph contract (2026-08-10 cycle audit).

The replay flow graph is built server-side and shipped to the client as a
pre-rendered Mermaid string, so everything asserted here is asserted against
that string as well as the JSON — the bug this file exists for lived in the
gap between the two.
"""

from app.routers.cycle_replay_router import (
    _AGENT_META,
    _PIPELINE_EDGES,
    _assign_short_ids,
    _build_mermaid,
)


def _node(agent, *, ticker="MA", elapsed_ms=1000, outcome="SUCCESS", quality=80):
    meta = _AGENT_META.get(agent, {"label": agent.title(), "icon": "🔧", "layer": 99})
    return {
        "id": agent, "label": meta["label"], "icon": meta["icon"], "layer": meta["layer"],
        "outcome": outcome, "elapsed_ms": elapsed_ms, "loops_used": 1,
        "token_usage": 100, "ticker": ticker, "quality_score": quality,
    }


def _edges_for(agents):
    present = set(agents)
    return [
        {"from": s, "to": d, "artifact": a}
        for s, d, a in _PIPELINE_EDGES
        if s in present and d in present
    ]


# ── The three agents that rendered as unconnected islands ──────────────

def test_debate_agents_are_connected_in_json_and_in_the_diagram():
    """bull_defense / valuation_analyst / contradiction_shadow had no edges.

    Asserting on `edges` alone would have passed while the diagram stayed
    broken: the mermaid builder dropped edges whose agent was missing from its
    id table, so the JSON and the picture disagreed.
    """
    agents = [
        "regime_engine", "junior_analyst", "fundamental_analyst", "quant_analyst",
        "valuation_analyst", "bull_agent", "bear_agent", "bull_defense",
        "debate_judge", "board_of_directors", "decision_synthesizer",
        "contradiction_shadow",
    ]
    nodes = [_node(a) for a in agents]
    edges = _edges_for(agents)
    mermaid = _build_mermaid(nodes, edges)

    for src, dst in [
        ("junior_analyst", "valuation_analyst"),
        ("valuation_analyst", "bull_agent"),
        ("bull_agent", "bull_defense"),
        ("bear_agent", "bull_defense"),
        ("bull_defense", "debate_judge"),
        ("decision_synthesizer", "contradiction_shadow"),
    ]:
        assert any(e["from"] == src and e["to"] == dst for e in edges), f"no JSON edge {src}->{dst}"

    ids = _assign_short_ids(agents)
    for src, dst in [
        ("junior_analyst", "valuation_analyst"),
        ("bull_agent", "bull_defense"),
        ("bull_defense", "debate_judge"),
        ("decision_synthesizer", "contradiction_shadow"),
    ]:
        assert f"    {ids[src]} --> {ids[dst]}" in mermaid, f"edge {src}->{dst} missing from diagram"

    # No agent may float: every node id must appear on some edge line.
    drawn = {ln.strip() for ln in mermaid.splitlines() if "-->" in ln}
    for agent in agents:
        sid = ids[agent]
        assert any(sid in ln.split("-->")[0].strip() or sid == ln.split("-->")[1].strip()
                   for ln in drawn), f"{agent} rendered with no edges"


def test_edges_survive_for_an_agent_missing_from_the_id_table():
    """Regression: the guard used `short_ids.get(n)` with no default.

    An agent absent from the table contributed None to the guard list, so its
    edge could never match its own fallback id and vanished from the diagram
    while still appearing in the JSON. That silently un-drew every
    tournament_debate edge.
    """
    nodes = [_node("junior_analyst"), _node("brand_new_agent")]
    edges = [{"from": "junior_analyst", "to": "brand_new_agent", "artifact": "x"}]
    mermaid = _build_mermaid(nodes, edges)

    ids = _assign_short_ids(["junior_analyst", "brand_new_agent"])
    assert f"    {ids['junior_analyst']} --> {ids['brand_new_agent']}" in mermaid


def test_tournament_debate_has_a_node_id():
    """It sits in _AGENT_META and _PIPELINE_EDGES; without an id its edges drop."""
    assert _assign_short_ids(["tournament_debate"])["tournament_debate"] == "TOURN"


def test_short_ids_never_collide():
    ids = _assign_short_ids(["bull_agent", "bull_defense", "bulldozer", "bulldozer_two"])
    assert len(set(ids.values())) == 4


# ── Cross-ticker folding ───────────────────────────────────────────────

def test_a_failure_on_one_ticker_is_not_hidden_by_a_success_on_another():
    """The bug that made the graph lie on cycle-v3-1786401874.

    The old dedup kept the first row per agent, so MA's healthy
    fundamental_analyst (199.3s, Q:87) was rendered as the cycle's and F's
    AGENT_ERROR on the same agent was invisible.
    """
    nodes = [
        _node("fundamental_analyst", ticker="MA", elapsed_ms=199345, quality=87),
        _node("fundamental_analyst", ticker="F", elapsed_ms=61644,
              outcome="AGENT_ERROR", quality=-1),
    ]
    mermaid = _build_mermaid(nodes, [])

    assert "1/2 failed" in mermaid
    assert "fill:#dc2626" in mermaid, "a wave containing a failure must not style green"


def test_single_row_keeps_the_plain_duration_caption():
    mermaid = _build_mermaid([_node("junior_analyst", elapsed_ms=106080, quality=87)], [])
    assert "106.1s ✅ Q:87" in mermaid
    assert "×1" not in mermaid


def test_multi_ticker_caption_reports_the_run_count():
    nodes = [_node("junior_analyst", ticker=t, elapsed_ms=ms)
             for t, ms in [("MA", 100000), ("F", 200000), ("XOM", 300000)]]
    mermaid = _build_mermaid(nodes, [])
    assert "×3" in mermaid
    assert "med 200.0s" in mermaid


def test_contradiction_shadow_keeps_its_zero_and_shows_no_quality_badge():
    """A non-LLM step: 0ms is accurate and quality_score -1 means unscored."""
    nodes = [_node("contradiction_shadow", elapsed_ms=0, quality=-1)]
    mermaid = _build_mermaid(nodes, [])
    assert "0.0s ✅" in mermaid
    assert "Q:" not in mermaid
