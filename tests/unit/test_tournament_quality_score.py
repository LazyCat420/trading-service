"""
Quality scoring for the tournament debate artifact.

The tournament is the most expensive stage in the pipeline — measured at
~264s/ticker and ~1.2M tokens across a 5-ticker cycle, about a third of all
agent time — and it was the only substantial agent recording quality_score=-1,
because it bypasses run_v3_agent (and therefore score_artifact).

Its failure mode is NOT weak prose. It is stages silently collapsing: every
pitch eliminated before the head-to-head, an empty jury, or the fallback path
returning a structurally valid HOLD@0 that did no work. These tests pin that
distinction — a hollow tournament must score materially below a real one.
"""
from app.v3.quality_scorer import score_artifact


def _full_tournament() -> dict:
    """Shape mirrors run_tournament_debate()'s success return."""
    return {
        "action": "BUY",
        "confidence": 72,
        "winning_side": "bull",
        "vetoed": False,
        "risk_flags": ["valuation stretched vs 5y median"],
        "rationale": (
            "Value and momentum theses converged on the Q3 revenue beat of $62.0B "
            "(+18% YoY), with margin expansion to 21% supporting a re-rating. The "
            "bear case on decelerating cloud growth was rebutted by the RSI 58 "
            "trend structure and institutional accumulation."
        ),
        "pitches": [
            {"persona": "Value_Quant",
             "claim": "Trading at 18x forward earnings versus a 24x 5-year median, a 25% discount unjustified by the 18% revenue growth rate.",
             "equation": "pe_discount", "direction": "bullish", "risk": "multiple compression"},
            {"persona": "Momentum_Quant",
             "claim": "Price reclaimed the 200-day SMA on 2.1x relative volume with RSI 58, a constructive trend reset after the June drawdown.",
             "equation": "sma_reclaim", "direction": "bullish", "risk": "false breakout"},
        ],
        "survivors": [
            {"persona": "Value_Quant",
             "claim": "Trading at 18x forward earnings versus a 24x 5-year median, a 25% discount unjustified by the 18% revenue growth rate.",
             "backtest_pnl": 4.2},
        ],
        "h2h": {
            "thesis_a": {
                "persona": "Value_Quant",
                "claim": "The 25% discount to the 5-year median multiple is unjustified given accelerating revenue.",
                "attack_points": ["Bear ignores the margin expansion to 21%",
                                  "Cloud deceleration is already priced at 18x"],
            },
            "thesis_b": {
                "persona": "Momentum_Quant",
                "claim": "The 200-day SMA reclaim on 2.1x volume signals institutional re-accumulation.",
                "attack_points": ["Volume spike may be index rebalancing"],
            },
        },
        "jury_verdict": {"votes": {"juror_1": "thesis_a", "juror_2": "thesis_a"},
                         "winner": "thesis_a"},
        "total_tokens": 233763,
        "elapsed_seconds": 264.5,
    }


def _fallback_tournament() -> dict:
    """Shape mirrors _build_fallback_result() — valid dict, zero work done."""
    return {
        "action": "HOLD",
        "confidence": 0,
        "winning_side": "fallback",
        "rationale": "Tournament fallback: no pitches survived validation",
        "pitches": [],
        "survivors": [],
        "h2h": {},
        "jury_verdict": {},
        "total_tokens": 12000,
        "elapsed_seconds": 0,
    }


def test_a_real_tournament_scores_well():
    result = score_artifact("tournament_debate", _full_tournament())
    assert result["quality_score"] >= 70, result
    assert result["flag"] == "good"


def test_the_fallback_path_is_not_mistaken_for_work():
    """HOLD@0 with empty stages is structurally valid but did nothing."""
    result = score_artifact("tournament_debate", _fallback_tournament())
    assert result["quality_score"] < 45, result
    assert result["flag"] in ("weak", "dead_end")


def test_hollow_tournament_scores_below_a_real_one():
    """The whole point: cost is identical, value is not."""
    good = score_artifact("tournament_debate", _full_tournament())["quality_score"]
    bad = score_artifact("tournament_debate", _fallback_tournament())["quality_score"]
    assert good - bad >= 25, f"good={good} bad={bad} — scorer cannot tell them apart"


def test_all_pitches_eliminated_is_penalised():
    """Pitches generated but nothing survived == no debate occurred."""
    art = _full_tournament()
    art["survivors"] = []
    collapsed = score_artifact("tournament_debate", art)["quality_score"]
    assert collapsed < score_artifact("tournament_debate", _full_tournament())["quality_score"]


def test_missing_jury_is_penalised():
    """A verdict with no jury is an unearned answer."""
    art = _full_tournament()
    art["jury_verdict"] = {}
    assert (score_artifact("tournament_debate", art)["quality_score"]
            < score_artifact("tournament_debate", _full_tournament())["quality_score"])


def test_one_sided_h2h_is_penalised():
    """A head-to-head needs two named theses to be a debate."""
    art = _full_tournament()
    art["h2h"] = {"thesis_a": art["h2h"]["thesis_a"], "thesis_b": {}}
    assert (score_artifact("tournament_debate", art)["quality_score"]
            < score_artifact("tournament_debate", _full_tournament())["quality_score"])


def test_thesis_claims_count_toward_content_density():
    """Substance lives in the theses, not in the one-line verdict — scoring
    only `rationale` would judge a 4-stage debate by its summary."""
    art = _full_tournament()
    rich = score_artifact("tournament_debate", art)["content_density"]

    stripped = _full_tournament()
    for p in stripped["pitches"]:
        p["claim"] = ""
    for s in stripped["survivors"]:
        s["claim"] = ""
    stripped["h2h"] = {"thesis_a": {"persona": "A", "claim": "", "attack_points": []},
                       "thesis_b": {"persona": "B", "claim": "", "attack_points": []}}
    thin = score_artifact("tournament_debate", stripped)["content_density"]

    assert rich > thin, f"claims not contributing to density (rich={rich} thin={thin})"


def test_scoring_never_raises_on_a_malformed_artifact():
    """Scoring runs inside the cycle — it must not be able to break one."""
    for bad in ({}, {"action": "BUY"}, {"pitches": None, "h2h": None},
                {"pitches": "not-a-list", "survivors": {}, "jury_verdict": []}):
        out = score_artifact("tournament_debate", bad)
        assert 0 <= out["quality_score"] <= 100
