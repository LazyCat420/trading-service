"""Tests for the deterministic baseline score.

Every test below pins a defect that was actually present during development
and was caught by measuring against live rows, not by reading the code. They
are written against the pure core, which takes dicts and touches nothing.
"""

import pytest

from app.quant.decision_score import (
    WEIGHTS,
    build_decision_score_block,
    compute_risk_reward,
    rank_scores,
    score_decision,
    universe_percentile,
)


# A ticker with enough on file to score: mid-pack fundamentals, clean technicals.
def _fundamental(**over):
    base = {
        "pe_ratio": 24.0, "peg_ratio": 1.6, "price_to_book": 3.0,
        "price_to_sales": 3.1, "profit_margin": 0.10, "oper_margin": 0.16,
        "roe": 0.12, "roa": 0.044, "debt_to_equity": 0.69,
        "current_ratio": 1.38, "quick_ratio": 0.98, "revenue_growth": 0.083,
        "eps_growth_qoq": 0.166, "target_price": 115.0, "age_days": 10,
    }
    base.update(over)
    return base


def _technical(**over):
    base = {"close": 100.0, "atr": 2.0, "support": 95.0, "resistance": 106.0,
            "sma_50": 98.0, "sma_200": 92.0, "rsi": 55.0,
            "bollinger_pct": 0.6, "age_trading_days": 1}
    base.update(over)
    return base


class TestCoverage:
    def test_absent_pillar_is_dropped_not_scored_half(self):
        """StrategyRobot returns 0.5 for a pillar with no metrics, making an
        unknown indistinguishable from an average. Here it must be ABSENT."""
        s = score_decision(_fundamental(), _technical())
        assert s["pillars"]["dividend"]["covered"] is False
        assert s["pillars"]["dividend"]["score"] is None
        # ...and the weight must not be silently spent on it.
        assert s["coverage_pct"] == pytest.approx(
            100.0 * (1 - WEIGHTS["dividend"]) / sum(WEIGHTS.values()), abs=0.1)

    def test_no_dividend_does_not_penalise(self):
        """A non-payer must not be scored as a bad payer."""
        payer = score_decision(_fundamental(dividend_yield=0.019),
                               _technical())
        non_payer = score_decision(_fundamental(), _technical())
        assert non_payer["pillars"]["dividend"]["covered"] is False
        # The non-payer's composite is the payer's other pillars, renormalised
        # — not dragged toward zero by a missing tenth.
        assert non_payer["score"] > payer["score"] - 12

    def test_thin_data_is_not_scoreable_rather_than_mid(self):
        """The whole point: 'not enough on file' must never render as 50."""
        s = score_decision({"pe_ratio": 20.0}, {})
        assert s["band"] == "NOT_SCOREABLE"
        assert s["score"] is None
        assert s["confidence"] == 0
        assert "not_scoreable_reason" in s

    def test_empty_inputs_do_not_raise(self):
        s = score_decision(None, None)
        assert s["band"] == "NOT_SCOREABLE"
        assert s["score"] is None


class TestRiskReward:
    def test_uses_one_stop_convention_across_names(self):
        """A ratio whose denominator is chosen per name ranks the choice of
        denominator. The stop is 2x ATR whenever an ATR exists, regardless of
        how near a support level happens to sit."""
        near = compute_risk_reward(_technical(support=99.0), {}, _fundamental())
        far = compute_risk_reward(_technical(support=80.0), {}, _fundamental())
        assert near["stop"] == far["stop"] == pytest.approx(96.0)
        assert near["ratio"] == far["ratio"]

    def test_no_atr_floors_the_stop(self):
        """Without an ATR the volatility floor cannot apply. A support level a
        hair under the close once produced a ratio of 37,175:1."""
        rr = compute_risk_reward(
            _technical(atr=None, support=99.99), {}, _fundamental())
        assert rr["stop"] == pytest.approx(97.0)  # the 3% floor
        assert rr["ratio"] < 10

    def test_prefers_a_thesis_horizon_target(self):
        """Median upside to resistance is 5.5% and to the analyst target 14.8%
        — gating a multi-quarter thesis on a swing high made the R:R floor a
        constant, failing 68% of the universe."""
        rr = compute_risk_reward(_technical(), {}, _fundamental())
        assert rr["target"] == pytest.approx(115.0)
        assert rr["target_horizon"] == "thesis"

    def test_falls_back_to_resistance_marked_near_term(self):
        rr = compute_risk_reward(_technical(), {}, _fundamental(target_price=None))
        assert rr["target"] == pytest.approx(106.0)
        assert rr["target_horizon"] == "near-term"

    def test_absurd_vendor_target_is_rejected_and_recorded(self):
        """AUUD carries a $3,272.50 target against a $1.03 close. Discarding it
        silently would leave a permanent invisible haircut."""
        rr = compute_risk_reward(
            _technical(), {}, _fundamental(target_price=3272.5))
        assert rr["target"] == pytest.approx(106.0)  # fell through to resistance
        assert rr["rejected_targets"]
        assert "sanity bound" in rr["rejected_targets"][0]

    def test_target_below_price_is_reported_not_erased(self):
        rr = compute_risk_reward(
            _technical(resistance=None), {}, _fundamental(target_price=80.0))
        assert rr["target"] == pytest.approx(80.0)
        assert rr["ratio"] < 0
        assert "BELOW the current price" in rr["note"]

    def test_no_close_is_not_computable(self):
        rr = compute_risk_reward({"atr": 2.0}, {}, _fundamental())
        assert rr["ratio"] is None
        assert "not computable" in rr["note"]


class TestGates:
    def test_unknown_is_never_folded_into_pass(self):
        s = score_decision(_fundamental(debt_to_equity=None), _technical())
        leverage = next(g for g in s["gates"] if g["name"] == "leverage")
        assert leverage["verdict"] == "UNKNOWN"
        assert "leverage" in s["gates_unknown"]
        assert "leverage" not in s["gates_failed"]

    def test_a_failed_gate_caps_the_band_without_rewriting_the_score(self):
        """Structural rejection is separate from scoring, so a content bonus
        cannot out-point a failure — and the score stays readable so 'good
        company, bad setup' is still visible in the shadow table."""
        s = score_decision(
            _fundamental(pe_ratio=8.0, peg_ratio=0.6, price_to_book=0.8,
                         price_to_sales=0.7, revenue_growth=0.45,
                         profit_margin=-0.10),
            _technical())
        assert "profitability" in s["gates_failed"]
        assert s["band"] == "NEUTRAL"
        assert s.get("band_before_gates") in ("CANDIDATE", "STRONG_CANDIDATE")
        # The SCORE is untouched — only the band was capped. Without this the
        # shadow table could not distinguish "mid-pack company" from "good
        # company, failed structural gate", which is the comparison the whole
        # exercise rests on. Asserted against the band floor, not against
        # itself: `x == approx(x)` is a tautology that asserts nothing.
        assert s["score"] >= 56.0
        assert s["warnings"]

    def test_near_term_target_cannot_fail_the_rr_gate(self):
        """A swing high is the wrong horizon to gate a multi-quarter thesis on;
        the ratio is reported but does not get a verdict it cannot earn."""
        s = score_decision(_fundamental(target_price=None),
                           _technical(resistance=101.0))
        rr_gate = next(g for g in s["gates"] if g["name"] == "risk_reward")
        assert rr_gate["verdict"] == "UNKNOWN"
        assert "wrong horizon" in rr_gate["detail"]


class TestConfidence:
    def test_is_recomputable_from_named_terms(self):
        """The number this replaces was a self-report that drifted 17 points in
        three weeks. Every term must be inspectable."""
        s = score_decision(_fundamental(), _technical())
        assert s["confidence_terms"]
        assert all(t.startswith(("+", "-")) for t in s["confidence_terms"])

    def test_capped_below_a_desk_that_read_the_filing(self):
        """This layer sees ratios and price levels. A deterministic screen that
        can emit 95 would outrank a desk that read the 10-K."""
        s = score_decision(
            _fundamental(pe_ratio=8.0, peg_ratio=0.5, price_to_book=0.7,
                         price_to_sales=0.6, revenue_growth=0.60,
                         eps_growth_qoq=1.8, profit_margin=0.35,
                         dividend_yield=0.05, target_price=180.0),
            _technical())
        assert s["confidence"] <= 85

    def test_staleness_costs_confidence(self):
        fresh = score_decision(_fundamental(), _technical())
        stale = score_decision(_fundamental(),
                               _technical(age_trading_days=9))
        assert stale["confidence"] < fresh["confidence"]

    def test_does_not_collapse_to_one_value(self):
        """A scorer emitting two values is a coin, not a filter — check the
        output DISTRIBUTION, not the logic."""
        seen = set()
        for pe in range(8, 60, 4):
            for growth in (-0.05, 0.05, 0.20, 0.45):
                s = score_decision(
                    _fundamental(pe_ratio=float(pe), revenue_growth=growth),
                    _technical())
                seen.add((s["score"], s["confidence"]))
        assert len(seen) > 20


class TestBanding:
    def test_bands_are_not_all_one_value(self):
        """The first cut of these thresholds put 77% of 881 real tickers into
        NEUTRAL — the exact failure the module exists to fix, in a new
        vocabulary."""
        bands = set()
        for pe, pb, ps, g, m in (
            (8.0, 0.8, 0.7, 0.50, 0.30),
            (16.0, 1.6, 1.4, 0.20, 0.20),
            (25.0, 3.1, 3.1, 0.08, 0.10),
            (40.0, 7.0, 6.5, 0.02, 0.02),
            (70.0, 16.0, 15.0, -0.05, -0.06),
        ):
            s = score_decision(
                _fundamental(pe_ratio=pe, price_to_book=pb, price_to_sales=ps,
                             revenue_growth=g, profit_margin=m,
                             oper_margin=m + 0.05),
                _technical())
            bands.add(s["band"])
        assert len(bands) >= 3

    def test_band_vocabulary_is_not_executable(self):
        """Shadow only. If the band ever reads BUY/SELL/HOLD someone will wire
        it to the executor by accident."""
        s = score_decision(_fundamental(), _technical())
        assert s["band"] not in ("BUY", "SELL", "HOLD")


class TestUniversePercentile:
    """The live path scores ONE ticker at a time, so `rank_scores` can never
    run there. Auditing cycle-v3-1785962005 found all 11 rows with a NULL
    percentile — the column existed and nothing filled it."""

    def test_every_scoreable_result_carries_a_percentile(self):
        s = score_decision(_fundamental(), _technical(), ticker="T")
        assert s["percentile"] is not None
        assert s["percentile_universe"] > 0

    def test_not_scoreable_carries_none(self):
        s = score_decision({"pe_ratio": 20.0}, {}, ticker="THIN")
        assert s.get("percentile") is None

    def test_monotonic_in_the_score(self):
        good = score_decision(_fundamental(pe_ratio=9.0, revenue_growth=0.40,
                                           profit_margin=0.30), _technical())
        bad = score_decision(_fundamental(pe_ratio=70.0, revenue_growth=-0.05,
                                          profit_margin=-0.06), _technical())
        assert good["score"] > bad["score"]
        assert good["percentile"] > bad["percentile"]

    def test_clamped_at_both_ends(self):
        assert universe_percentile(0.0) == 0.0
        assert universe_percentile(100.0) == 100.0
        assert universe_percentile(None) is None

    def test_median_composite_lands_near_the_middle(self):
        # p50 of the measured universe is a composite of 52.3.
        assert 45.0 <= universe_percentile(52.3) <= 55.0


class TestRanking:
    def test_percentile_spans_the_scored_set(self):
        scores = [
            score_decision(_fundamental(pe_ratio=float(pe)), _technical(),
                           ticker=f"T{pe}")
            for pe in (8, 16, 25, 40, 70)
        ]
        rank_scores(scores)
        pcts = sorted(s["percentile"] for s in scores)
        assert pcts[0] == 0.0 and pcts[-1] == 100.0

    def test_not_scoreable_gets_no_percentile(self):
        """Ranking a name whose score does not exist would invent one."""
        scores = [
            score_decision(_fundamental(), _technical(), ticker="GOOD"),
            score_decision(_fundamental(pe_ratio=40.0), _technical(),
                           ticker="MEH"),
            score_decision({"pe_ratio": 20.0}, {}, ticker="THIN"),
        ]
        rank_scores(scores)
        thin = next(s for s in scores if s["ticker"] == "THIN")
        assert "percentile" not in thin

    def test_single_name_keeps_its_universe_rank(self):
        """A WITHIN-SET percentile over one name is 100 by construction and
        means nothing, so `rank_scores` must decline to compute one — and in
        declining it must not clobber the universe percentile the scorer
        already attached, which is the only meaningful rank for a lone name."""
        scores = [score_decision(_fundamental(), _technical(), ticker="ONE")]
        before = scores[0]["percentile"]
        rank_scores(scores)
        assert scores[0]["percentile"] == before
        assert scores[0]["percentile_universe"] > 1


class TestBlock:
    def test_absence_is_louder_than_a_bad_score(self):
        block = build_decision_score_block(
            "THIN", score_decision({"pe_ratio": 20.0}, {}, ticker="THIN"))
        assert "NOT SCOREABLE" in block
        assert "NOT as a neutral reading" in block

    def test_tells_the_agent_not_to_copy_the_number(self):
        """The measured risk of injecting a number is that it gets copied —
        the quant-math block was copied 127/127 faithfully, and the HRP target
        weight was read straight through as an order size."""
        block = build_decision_score_block(
            "T", score_decision(_fundamental(), _technical(), ticker="T"))
        assert "Do NOT copy" in block
        assert "argue with" in block

    def test_names_the_metrics_behind_each_pillar(self):
        block = build_decision_score_block(
            "T", score_decision(_fundamental(), _technical(), ticker="T"))
        assert "pe_ratio" in block
        assert "ABSENT" in block  # the dividend pillar
        assert "RISK/REWARD" in block

    def test_never_returns_empty_for_an_attempted_ticker(self):
        for f, t in ((_fundamental(), _technical()), ({}, {}),
                     (_fundamental(), {})):
            assert build_decision_score_block("X", score_decision(f, t)).strip()


class TestUnits:
    def test_margins_stored_as_percentages_are_normalised(self):
        """Vendors disagree on whether a margin is 0.0416 or 4.16. The same
        [-1.5, 1.5] band as fundamental_block, deliberately — two conventions
        for one column is how a margin becomes a 100x error."""
        as_fraction = score_decision(_fundamental(profit_margin=0.25),
                                     _technical())
        as_percent = score_decision(_fundamental(profit_margin=25.0),
                                    _technical())
        assert as_fraction["pillars"]["growth"]["score"] == \
            as_percent["pillars"]["growth"]["score"]

    def test_negative_multiples_are_not_scored_as_cheap(self):
        """A P/E of -8 is a loss-making company, not the best value name in the
        universe."""
        s = score_decision(_fundamental(pe_ratio=-8.0), _technical())
        pe = s["pillars"]["value"]["metrics"]["pe_ratio"]
        assert pe["score"] is None
        assert "not scoreable" in pe["note"]

    def test_nan_does_not_silently_pass_a_band(self):
        """NaN compares false against every threshold, so it declines to score
        while looking scored."""
        s = score_decision(_fundamental(pe_ratio=float("nan")), _technical())
        assert "pe_ratio" not in s["pillars"]["value"]["metrics"]

    def test_bollinger_reads_the_numeric_column(self):
        """`bollinger_position` is the ENUM ('UPPER'/'LOWER'); reading it here
        would be a permanent silent skip. `bollinger_pct` is the number."""
        with_num = score_decision(_fundamental(),
                                  _technical(bollinger_pct=0.9))
        assert "bollinger_pct" in with_num["pillars"]["momentum"]["metrics"]
        enum_only = _technical()
        enum_only.pop("bollinger_pct")
        enum_only["bollinger_position"] = "UPPER"
        s = score_decision(_fundamental(), enum_only)
        assert "bollinger_position" not in s["pillars"]["momentum"]["metrics"]


class TestDeterminism:
    def test_same_input_same_output(self):
        a = score_decision(_fundamental(), _technical(), ticker="T")
        b = score_decision(_fundamental(), _technical(), ticker="T")
        assert a == b
