"""Tests for the precomputed valuation block.

The block exists because the pipeline had no valuation math at all: no DCF, no
computed EV/EBITDA, and an `intrinsic_value_estimate` that a board persona was
asked to guess. That is the same hole the 2026-07-24 audit found in the quant
desk, where 171 of 305 reports carried an invented RSI.

Most of what is asserted here is NEGATIVE: what the block must refuse to say.
A valuation block that always emits a number is not a valuation block — it is a
fabrication surface with a computed veneer, which is strictly worse than the
absence it replaced, because a computed-looking number stops anyone checking.
"""

from datetime import date, timedelta

import pytest

from app.quant import valuation_block as vb


def _fundamentals(**overrides) -> dict:
    base = {
        "snapshot_date": date.today(),
        "market_cap": 400_000_000_000.0,
        "pe_ratio": 24.6,
        "forward_pe": 22.0,
        "peg_ratio": 2.10,
        "ev_to_ebitda": 14.1,
        "ev_to_sales": 4.0,
        "price_to_fcf": 26.0,
        "revenue": 101_200_000_000.0,
        "revenue_growth": 0.082,
        "beta": 0.94,
        "eps_ttm": 6.2,
        "eps_growth_next_5y": 0.11,      # FRACTION — 11%, per schema_pg.sql
        "eps_growth_past_5y": 0.09,
        "roic": 0.18,
        "oper_margin": 0.22,
        "shares_outstanding": 3_000_000_000.0,
    }
    base.update(overrides)
    return base


def _balance(**overrides) -> dict:
    base = {
        "period_end": date.today() - timedelta(days=200),
        "total_equity": 60_000_000_000.0,
        "cash": 37_500_000_000.0,
        "total_debt": 52_000_000_000.0,
    }
    base.update(overrides)
    return base


def _quarter(rev=25_300_000_000.0, oi=5_600_000_000.0, ni=4_100_000_000.0,
             eps=1.55, fcf=3_775_000_000.0, end=None) -> dict:
    return {
        "period_end": end or date.today(),
        "revenue": rev, "operating_income": oi, "net_income": ni,
        "eps": eps, "free_cash_flow": fcf,
    }


def _annual(rev, fcf, eps, years_ago) -> dict:
    return {
        "period_end": date(2025 - years_ago, 12, 31),
        "revenue": rev, "operating_income": rev * 0.22,
        "net_income": rev * 0.15, "eps": eps, "free_cash_flow": fcf,
    }


@pytest.fixture
def desk(monkeypatch):
    """Installs a healthy, fully-populated ticker. Tests mutate from here."""
    state = {
        "fundamentals": _fundamentals(),
        "balance": _balance(),
        "quarterly": [_quarter() for _ in range(4)],
        "annual": [
            _annual(101_200_000_000.0, 15_100_000_000.0, 6.20, 0),
            _annual(94_000_000_000.0, 14_200_000_000.0, 5.70, 1),
            _annual(86_000_000_000.0, 13_600_000_000.0, 5.10, 2),
            _annual(78_000_000_000.0, 13_300_000_000.0, 4.60, 3),
            _annual(73_500_000_000.0, 13_320_000_000.0, 4.20, 4),
        ],
        "risk_free": 0.042,
    }

    monkeypatch.setattr(vb, "_fetch_fundamentals",
                        lambda t: state["fundamentals"])
    monkeypatch.setattr(vb, "_fetch_balance_sheet", lambda t: state["balance"])
    monkeypatch.setattr(
        vb, "_fetch_periods",
        lambda t, period_type, limit: (
            state["quarterly"][:limit] if period_type == "quarterly"
            else state["annual"][:limit]
        ),
    )
    monkeypatch.setattr(vb, "_fetch_risk_free", lambda: state["risk_free"])
    return state


# ──────────────────────────────────────────────────────────────────────
# Fail-open and absence
# ──────────────────────────────────────────────────────────────────────

class TestFailOpen:
    def test_a_db_error_never_raises(self, monkeypatch):
        def boom(_t):
            raise RuntimeError("connection pool exhausted")
        monkeypatch.setattr(vb, "_fetch_fundamentals", boom)

        assert vb.compute_valuation_baseline("AAPL") == {}
        assert vb.build_valuation_block("AAPL") == vb._NO_DATA

    def test_empty_ticker_is_not_a_query(self, monkeypatch):
        monkeypatch.setattr(vb, "_fetch_fundamentals",
                            lambda t: pytest.fail("should not query"))
        assert vb.compute_valuation_baseline("") == {}

    def test_no_data_ticker_gets_a_LOUD_block_not_an_empty_string(
            self, monkeypatch):
        """Absence must be louder than staleness.

        The predecessor of this branch in technical_baseline returned "" — so
        the one case where the agent knew least produced the least warning, and
        ASIC and ARCVF reached the board with no price history and nothing in
        the prompt saying so.
        """
        monkeypatch.setattr(vb, "_fetch_fundamentals", lambda t: None)

        block = vb.build_valuation_block("NEWCO")
        assert block != ""
        assert "NONE ON FILE" in block
        assert "Do NOT state valuation multiples as fact" in block


# ──────────────────────────────────────────────────────────────────────
# Missing data must never become a zero
# ──────────────────────────────────────────────────────────────────────

class TestMissingDataIsNeverZero:
    def test_three_quarters_does_not_become_a_ttm(self, desk):
        """A 3-of-4 sum is a 25% understatement wearing the costume of a real
        number — it would flow into EV/EBIT looking exactly like a multiple."""
        desk["quarterly"] = [_quarter() for _ in range(3)]

        b = vb.compute_valuation_baseline("AAPL")

        assert "ev_to_ebit" not in b
        assert "fcf_yield_pct" not in b
        assert "4 quarterly rows" in b["not_computable"]["ttm"] or \
               "of 4 quarterly rows" in b["not_computable"]["ttm"]

    def test_one_null_quarter_drops_only_that_field(self, desk):
        """Fail per-field, not per-block: one null FCF must not also destroy
        revenue and EBIT, which are perfectly good in all four rows."""
        desk["quarterly"][2] = _quarter(fcf=None)

        b = vb.compute_valuation_baseline("AAPL")

        assert "fcf_yield_pct" not in b
        assert "ev_to_ebit" in b
        assert "ev_to_sales" in b

    def test_null_cash_does_not_become_a_zero_enterprise_value(self, desk):
        """market_cap alone is not EV. Defaulting cash to 0 would silently
        report a levered EV as if the company held no cash."""
        desk["balance"] = _balance(cash=None)

        b = vb.compute_valuation_baseline("AAPL")

        assert "enterprise_value" not in b
        assert "net_debt" not in b
        assert "market cap alone is NOT enterprise value" in \
               b["not_computable"]["enterprise_value"]

    def test_a_missing_balance_sheet_is_named_not_inferred(self, desk):
        desk["balance"] = None

        b = vb.compute_valuation_baseline("AAPL")

        assert "enterprise_value" in b["not_computable"]
        # The P/E half of the analysis does not depend on the balance sheet and
        # must survive.
        assert "pe_ratio" in b

    def test_nan_is_filtered_rather_than_reported(self, desk):
        """NaN survives a NOT NULL check and compares false against every
        threshold, so an unfiltered one lands in the block looking like data."""
        desk["fundamentals"] = _fundamentals(market_cap=float("nan"))

        b = vb.compute_valuation_baseline("AAPL")

        assert "enterprise_value" not in b
        assert b["not_computable"]["enterprise_value"] == "no market cap on file"

    def test_negative_ebit_leverage_is_not_meaningful(self, desk):
        desk["quarterly"] = [_quarter(oi=-800_000_000.0) for _ in range(4)]

        b = vb.compute_valuation_baseline("AAPL")

        assert "ev_to_ebit" not in b
        assert "net_debt_to_ebit" not in b
        assert "not meaningful" in b["not_computable"]["ev_to_ebit"]

    def test_negative_fcf_yield_is_printed_not_suppressed(self, desk):
        """A suppressed negative yield reads as 'no data' when it is in fact
        the single most important fact about the company."""
        desk["quarterly"] = [_quarter(fcf=-900_000_000.0) for _ in range(4)]

        b = vb.compute_valuation_baseline("AAPL")
        block = vb.build_valuation_block("AAPL")

        assert b["fcf_yield_pct"] < 0
        assert "negative free cash flow" in block

    def test_a_sign_flip_has_no_compound_growth_rate(self, desk):
        desk["annual"] = [
            _annual(101_200_000_000.0, 15_100_000_000.0, 6.20, 0),
            _annual(94_000_000_000.0, 14_200_000_000.0, 5.70, 1),
            _annual(86_000_000_000.0, -2_000_000_000.0, 5.10, 2),
        ]

        b = vb.compute_valuation_baseline("AAPL")

        assert "fcf_cagr_pct" not in b
        assert "sign flip" in b["not_computable"]["fcf_cagr_pct"]
        assert "revenue_cagr_pct" in b  # unaffected


class TestCagrEndpointsAreChosenByData:
    """`financial_history` carries all-null placeholder rows for the year before
    coverage begins — MEASURED: COST and GE both have a fully-null 2021 row
    sitting underneath four clean years. Selecting the oldest endpoint by
    POSITION reads that None and silently deletes the CAGR for a ticker whose
    data is perfectly good, which is exactly what happened on the first live
    probe of this block."""

    def test_a_null_placeholder_row_does_not_delete_the_cagr(self, desk):
        desk["annual"] = desk["annual"][:4] + [
            {"period_end": date(2020, 12, 31), "revenue": None,
             "operating_income": None, "net_income": None,
             "eps": None, "free_cash_flow": None},
        ]

        b = vb.compute_valuation_baseline("AAPL")

        assert "revenue_cagr_pct" in b
        assert b["revenue_cagr_pct"] == pytest.approx(9.0, abs=1.5)

    def test_endpoints_are_per_field_not_per_row(self, desk):
        """Citigroup has four clean years of revenue and no operating_income at
        all. One missing column must not delete the metric beside it."""
        for row in desk["annual"]:
            row["operating_income"] = None

        b = vb.compute_valuation_baseline("AAPL")

        assert "revenue_cagr_pct" in b
        assert "ebit_cagr_pct" not in b

    def test_the_span_comes_from_dates_not_row_count(self, desk):
        """A gap year would otherwise compress the same growth into fewer
        years and overstate the rate."""
        desk["annual"] = [
            _annual(121_000_000_000.0, 15_100_000_000.0, 6.20, 0),
            _annual(101_200_000_000.0, 14_200_000_000.0, 5.70, 2),
            _annual(73_500_000_000.0, 13_320_000_000.0, 4.20, 4),
        ]

        b = vb.compute_valuation_baseline("AAPL")

        # 73.5B -> 121B over FOUR years is ~13.3%/yr. Counting rows would call
        # the span 2 years and report ~28%.
        assert b["revenue_cagr_pct"] == pytest.approx(13.3, abs=1.0)

    def test_two_points_is_not_a_trend(self, desk):
        desk["annual"] = desk["annual"][:2]

        b = vb.compute_valuation_baseline("AAPL")

        assert "revenue_cagr_pct" not in b
        assert "need 3" in b["not_computable"]["revenue_cagr_pct"]

    def test_the_dcf_compares_like_for_like(self, desk):
        """An implied NOPAT growth rate must be compared against realized EBIT
        growth, not revenue or EPS. Leading with a mismatched series is how
        'the price implies 17% and the company grew 7%' gets asserted when the
        two numbers measure different things."""
        desk["quarterly"] = [_quarter(fcf=None) for _ in range(4)]

        block = vb.build_valuation_block("AAPL")

        assert "REVERSE DCF on NOPAT" in block
        assert "realized EBIT CAGR" in block
        assert "the like-for-like series" in block

    def test_not_computable_reasons_reach_the_block(self, desk):
        desk["fundamentals"] = _fundamentals(eps_growth_next_5y=None)

        block = vb.build_valuation_block("AAPL")

        assert "NOT COMPUTABLE: peg" in block


# ──────────────────────────────────────────────────────────────────────
# The EBITDA invariant — the module's central honesty claim
# ──────────────────────────────────────────────────────────────────────

class TestEbitIsNeverCalledEbitda:
    def test_our_own_multiple_is_labelled_ev_to_ebit(self, desk):
        block = vb.build_valuation_block("AAPL")
        assert "EV/EBIT " in block

    def test_ev_to_ebitda_appears_only_as_a_tagged_vendor_figure(self, desk):
        """There is no D&A column anywhere in this system, so any unqualified
        'EV/EBITDA' in a prompt would be a number we invented in code — the
        failure the whole module exists to prevent, one layer deeper."""
        block = vb.build_valuation_block("AAPL")

        for line in block.splitlines():
            if "EV/EBITDA" in line:
                assert "unverified" in line or "cannot be computed" in line

    def test_the_no_dna_disclaimer_rides_with_the_multiple(self, desk):
        block = vb.build_valuation_block("AAPL")

        assert "ev_to_ebit" in vb.compute_valuation_baseline("AAPL")
        assert "EBITDA cannot be computed" in block


# ──────────────────────────────────────────────────────────────────────
# Reverse DCF
# ──────────────────────────────────────────────────────────────────────

class TestReverseDcf:
    def test_implied_growth_recovers_a_known_input(self):
        """Round-trip against an EV constructed to BE the DCF of a 10% grower.

        This is the only test here that can catch an algebra error — every
        other assertion would pass just as happily against a plausible-looking
        wrong formula.
        """
        fcf, r, g = 1_000.0, 0.09, 0.10
        ev = vb._dcf_pv(fcf, g, r)

        implied, reason = vb._implied_growth(ev, fcf, r)

        assert reason is None
        assert implied == pytest.approx(10.0, abs=0.2)

    def test_monotonic_in_price(self):
        """A higher price must imply higher growth, or the solver is inverted."""
        fcf, r = 1_000.0, 0.09
        low, _ = vb._implied_growth(vb._dcf_pv(fcf, 0.04, r), fcf, r)
        high, _ = vb._implied_growth(vb._dcf_pv(fcf, 0.20, r), fcf, r)
        assert low < high

    def test_a_negative_flow_is_unsolvable_not_clamped(self):
        implied, reason = vb._implied_growth(400e9, -900e6, 0.09)

        assert implied is None
        assert "negative" in reason

    def test_no_flow_at_all_is_unsolvable(self):
        implied, reason = vb._implied_growth(400e9, None, 0.09)

        assert implied is None
        assert "no discountable flow" in reason

    def test_out_of_bracket_is_reported_not_pinned_to_the_edge(self):
        """A clamped 60% is a number, and a number is what the model quotes."""
        fcf, r = 1_000.0, 0.09
        absurd_ev = vb._dcf_pv(fcf, 0.95, r)

        implied, reason = vb._implied_growth(absurd_ev, fcf, r)

        assert implied is None
        assert "outside the solver bracket" in reason

    def test_discount_rate_must_exceed_terminal_growth(self):
        implied, reason = vb._implied_growth(400e9, 15e9, 0.02)
        assert implied is None
        assert "terminal growth" in reason

    def test_missing_treasury_still_computes_and_names_the_assumption(self, desk):
        """An assumed rate is fine. An assumed rate the reader cannot see is
        not — it turns a judgment call into apparent fact."""
        desk["risk_free"] = None

        b = vb.compute_valuation_baseline("AAPL")
        block = vb.build_valuation_block("AAPL")

        assert b["risk_free_assumed"] is True
        assert "implied_growth_pct" in b
        assert "ASSUMED" in block

    def test_assumptions_are_printed_even_when_unsolvable(self, desk):
        """No EBIT and no FCF: nothing left to discount."""
        desk["quarterly"] = [_quarter(fcf=None, oi=None) for _ in range(4)]

        block = vb.build_valuation_block("AAPL")

        assert "REVERSE DCF: NOT COMPUTABLE" in block
        assert "discount rate" in block


class TestDcfFlowFallback:
    """MEASURED 2026-07-27: financial_history.free_cash_flow is non-null in 1 of
    3060 quarterly rows and 0 of 2412 annual rows — yfinance_collector hardcodes
    None and its upsert overwrites what fmp_collector writes. So the FCF path,
    which is the CORRECT one, is dead across the whole database, and the DCF
    falls back to NOPAT. These tests pin that fallback's honesty."""

    def test_fcf_wins_when_it_exists(self):
        flow, label, caveat = vb._dcf_flow(15e9, 22e9)
        assert flow == 15e9
        assert label == "TTM free cash flow"
        assert caveat is None

    def test_nopat_is_taxed_not_raw_ebit(self):
        """Discounting pre-tax EBIT as if it were owner cash overstates the
        flow for EVERY ticker, which biases EVERY verdict toward 'cheap'."""
        flow, label, _ = vb._dcf_flow(None, 22e9)

        assert flow == pytest.approx(22e9 * (1 - vb._TAX_RATE))
        assert flow < 22e9
        assert "21% tax" in label

    def test_the_fallback_announces_it_is_not_cash(self, desk):
        desk["quarterly"] = [_quarter(fcf=None) for _ in range(4)]

        b = vb.compute_valuation_baseline("AAPL")
        block = vb.build_valuation_block("AAPL")

        assert "implied_growth_pct" in b
        assert "NOPAT" in block
        assert "not a cash one" in block
        assert "ignores capex" in block

    def test_a_negative_flow_beats_no_flow(self):
        """Negative FCF must not veto a perfectly good EBIT stream."""
        flow, label, _ = vb._dcf_flow(-900e6, 22e9)
        assert flow == pytest.approx(22e9 * (1 - vb._TAX_RATE))

    def test_no_comparison_series_is_stated_not_silently_omitted(self, desk):
        """An implied growth rate with nothing to compare against says nothing
        about cheap or expensive, and must not be left looking like a verdict."""
        desk["annual"] = []
        desk["fundamentals"] = _fundamentals(eps_growth_next_5y=None)

        block = vb.build_valuation_block("AAPL")

        assert "nothing to compare it against" in block

    def test_the_comparison_to_realized_growth_is_present(self, desk):
        """The implied rate alone is inert; it only means something beside the
        company's own realized growth."""
        block = vb.build_valuation_block("AAPL")

        assert "REVERSE DCF" in block
        assert "For comparison" in block
        assert "realized FCF CAGR" in block


# ──────────────────────────────────────────────────────────────────────
# Units and vendor cross-checks
# ──────────────────────────────────────────────────────────────────────

class TestUnits:
    def test_peg_scales_the_fractional_growth_column(self, desk):
        """schema_pg.sql stores percent-like values as FRACTIONS (0.11 == 11%).
        Dividing a P/E by 0.11 instead of 11.0 yields a PEG 100x off — which is
        precisely the kind of number that reads as plausible."""
        b = vb.compute_valuation_baseline("AAPL")

        assert b["peg"] == pytest.approx(24.6 / 11.0, abs=0.01)
        assert 1.0 < b["peg"] < 5.0

    def test_enterprise_value_is_market_cap_plus_net_debt(self, desk):
        b = vb.compute_valuation_baseline("AAPL")

        assert b["enterprise_value"] == pytest.approx(
            400e9 + 52e9 - 37.5e9, rel=1e-9)
        assert b["net_debt"] == pytest.approx(52e9 - 37.5e9, rel=1e-9)

    def test_money_is_never_rendered_in_scientific_notation(self, desk):
        block = vb.build_valuation_block("AAPL")
        assert "e+" not in block and "E+" not in block


class TestVendorCrossChecks:
    def test_a_large_disagreement_is_surfaced_not_resolved(self, desk):
        desk["fundamentals"] = _fundamentals(peg_ratio=0.4)

        block = vb.build_valuation_block("AAPL")

        assert "DATA QUALITY signal" in block
        assert "do not pick one silently" in block

    def test_agreement_is_quiet(self, desk):
        b = vb.compute_valuation_baseline("AAPL")
        # PEG ours 2.24 vs vendor 2.10 is inside the 10% band.
        assert "PEG" not in (b.get("vendor") or {})


# ──────────────────────────────────────────────────────────────────────
# Staleness
# ──────────────────────────────────────────────────────────────────────

class TestStaleness:
    def test_a_stale_snapshot_never_claims_authority(self, desk):
        """CVX was once served a 1963 RSI under 'these are the authoritative
        values'. A stale block must not claim authority ANYWHERE in its text —
        the model reads the sentence, not the flag."""
        desk["fundamentals"] = _fundamentals(
            snapshot_date=date.today() - timedelta(days=200))

        block = vb.build_valuation_block("AAPL")

        assert "authoritative" not in block
        assert "STALE" in block

    def test_an_old_balance_sheet_is_explained_not_alarmed_about(self, desk):
        """balance_sheet has no period_type and yfinance writes ANNUAL rows, so
        a 200-day-old row is the normal case, not an outage."""
        block = vb.build_valuation_block("AAPL")

        assert "annual-cadence" in block
        assert "normal, not an outage" in block

    def test_a_fresh_snapshot_does_claim_authority(self, desk):
        block = vb.build_valuation_block("AAPL")
        assert "authoritative" in block
