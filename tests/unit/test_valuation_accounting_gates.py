"""The accounting gates: loss quarters, fiscal windows, and reporting currency.

2026-07-28. The arithmetic in valuation_block was always correct — EV/Sales
recomputes exactly. The ACCOUNTING was not. Run over the 286 tickers carrying
both our EV/EBIT and an independently scraped vendor EV/EBITDA, 32 were wrong:
11 structurally impossible (ratio < 1.0, i.e. EV/EBIT below EV/EBITDA, which
requires EBIT > EBITDA) and 21 distorted beyond any D&A wedge.

The gate that let them through was `if ebit > 0` — a two-value test on a
continuous quantity, with nothing between "negative" and "healthy".
"""

from unittest.mock import patch

import pytest

from app.quant import valuation_block as vb


def _fund(**over):
    f = {
        "snapshot_date": "2026-07-28", "market_cap": 100_000.0,
        "ev_to_ebitda": 10.0, "beta": 1.0, "revenue": 50_000.0,
        "pe_ratio": None, "forward_pe": None, "peg_ratio": None,
        "ev_to_sales": None, "price_to_fcf": None, "revenue_growth": None,
        "eps_ttm": None, "eps_growth_next_5y": None,
        "eps_growth_past_5y": None, "roic": None, "oper_margin": None,
        "shares_outstanding": 1000.0,
    }
    f.update(over)
    return f


def _quarters(ebits, revs=None, start_days=0):
    """Four quarterly rows, newest first, spaced a real quarter apart."""
    from datetime import date, timedelta

    base = date(2026, 6, 30) - timedelta(days=start_days)
    revs = revs or [10_000.0] * len(ebits)
    return [
        {
            "period_end": base - timedelta(days=91 * i),
            "operating_income": e, "revenue": r,
            "net_income": e, "free_cash_flow": None, "eps": 1.0,
        }
        for i, (e, r) in enumerate(zip(ebits, revs))
    ]


def _run(quarters, fund=None, balance=None):
    fund = fund or _fund()
    balance = balance or {
        "period_end": "2026-06-30", "total_equity": 1.0,
        "cash": 0.0, "total_debt": 0.0,
    }
    with patch.object(vb, "_fetch_fundamentals", return_value=fund), \
         patch.object(vb, "_fetch_balance_sheet", return_value=balance), \
         patch.object(vb, "_fetch_periods",
                      side_effect=lambda t, p, n: quarters if p == "quarterly" else []), \
         patch.object(vb, "_fetch_risk_free", return_value=0.042):
        return vb.compute_valuation_baseline("TEST") or {}


class TestTheLossQuarterGate:
    def test_a_healthy_run_still_computes(self):
        """The gate must not simply refuse everything — 239 of 286 tickers
        still get a multiple after this change."""
        b = _run(_quarters([2500.0, 2500.0, 2500.0, 2500.0]))

        assert b.get("ev_to_ebit") is not None
        assert "ev_to_ebit" not in b.get("not_computable", {})

    def test_one_loss_quarter_withholds_the_multiple(self):
        """GM: 1459 + 2926 + (-3647) + 1076 = 1814, giving 103.4x against a
        vendor 11.9x. TTM is POSITIVE, so `ebit > 0` passed it clean with no
        data_gap and no warning."""
        b = _run(_quarters([1459.0, 2926.0, -3647.0, 1076.0]))

        assert b.get("ev_to_ebit") is None
        reason = b["not_computable"]["ev_to_ebit"]
        assert "negative operating income" in reason

    def test_the_ebit_is_still_reported_only_the_ratio_is_withheld(self):
        """The EBIT is a real figure and the desk may want it. Only the RATIO
        is suppressed, because that is the number that reads as a valuation."""
        b = _run(_quarters([1459.0, 2926.0, -3647.0, 1076.0]))

        assert b.get("ebit_ttm") == pytest.approx(1814.0)

    def test_a_depressed_denominator_without_a_loss_quarter(self):
        """No quarter is negative, but the sum is a fraction of the run-rate."""
        b = _run(_quarters([3000.0, 20.0, 20.0, 20.0]))

        assert b.get("ev_to_ebit") is None
        assert "depressed" in b["not_computable"]["ev_to_ebit"]

    def test_a_negative_ttm_keeps_its_own_distinct_reason(self):
        """The pre-existing branch must not be swallowed by the new one — the
        reason names the actual defect."""
        b = _run(_quarters([-100.0, -100.0, -100.0, -100.0]))

        assert b.get("ev_to_ebit") is None
        assert "negative" in b["not_computable"]["ev_to_ebit"]


class TestTheFiscalWindow:
    def test_quarters_that_are_not_twelve_months_are_refused(self):
        """SMCI's stored quarters run 10,243 / 12,682 / 5,017 / 5,756 ($M
        revenue) — a 12.7B quarter beside a 5.0B one, i.e. restated or shifted
        boundaries. Summing them yields a TTM matching no real 12-month span."""
        from datetime import date

        q = _quarters([500.0] * 4)
        q[3]["period_end"] = date(2023, 1, 1)   # a two-year span

        b = _run(q)

        assert b.get("ev_to_ebit") is None
        assert "twelve-month window" in b["not_computable"]["ev_to_ebit"]

    def test_a_normal_calendar_passes(self):
        b = _run(_quarters([2500.0] * 4))

        assert b.get("ev_to_ebit") is not None


class TestTheVendorCrossCheck:
    def test_below_one_is_impossible_and_is_refused(self):
        """EBIT <= EBITDA always, so EV/EBIT >= EV/EBITDA always. Ours below
        theirs is proof the inputs disagree, not a valuation view. TSM lands at
        0.034x: USD market cap over TWD filings."""
        b = _run(_quarters([2500.0] * 4), fund=_fund(ev_to_ebitda=500.0))

        assert b.get("ev_to_ebit") is None
        assert "BELOW the vendor" in b["not_computable"]["ev_to_ebit"]
        assert b["ev_ebit_vendor_ratio"] < 1.0

    def test_far_above_the_wedge_is_refused(self):
        """The D&A wedge is 1.24x on clean mega-caps, median 1.34 across the
        universe. Above 3x, something has crushed the denominator that the
        per-quarter checks did not name."""
        b = _run(_quarters([2500.0] * 4), fund=_fund(ev_to_ebitda=1.0))

        assert b.get("ev_to_ebit") is None
        assert "beyond the D&A wedge" in b["not_computable"]["ev_to_ebit"]

    def test_the_wedge_band_is_preserved(self):
        """COST measured 1.24x, GE 1.23x by hand. Those must still pass — a
        gate that refuses the cases it was validated on is worthless."""
        b = _run(_quarters([2500.0] * 4), fund=_fund(ev_to_ebitda=8.0))

        assert b.get("ev_to_ebit") is not None
        assert 1.0 <= b["ev_ebit_vendor_ratio"] <= 3.0

    def test_the_ratio_is_recorded_even_when_it_passes(self):
        """Recorded so the rate stays countable, the same reason the reconcile
        passes preserve originals."""
        b = _run(_quarters([2500.0] * 4))

        assert b.get("ev_ebit_vendor_ratio") is not None
        assert b.get("vendor_ev_to_ebitda") == 10.0

    def test_no_vendor_figure_is_not_a_failure(self):
        """664 of ~1181 tickers carry a vendor EV/EBITDA. The rest must still
        get a multiple — absence of a control is not evidence of a fault."""
        b = _run(_quarters([2500.0] * 4), fund=_fund(ev_to_ebitda=None))

        assert b.get("ev_to_ebit") is not None
        assert b.get("ev_ebit_vendor_ratio") is None


class TestTheReverseDcfInheritsTheGate:
    def test_a_bad_denominator_voids_the_implied_growth_rate(self):
        """The DCF falls back to NOPAT = EBIT x (1 - tax), so a denominator bad
        enough to void EV/EBIT voids the growth rate built on the same figure —
        and that one is worse, because it reads as a forward-looking claim.
        GM's 103x became '36.7% implied NOPAT growth', which the synthesizer
        quoted verbatim as its reason to override a BUY."""
        b = _run(_quarters([1459.0, 2926.0, -3647.0, 1076.0]))

        assert b.get("implied_growth_pct") is None
        assert "NOT MEANINGFUL" in b["not_computable"]["implied_growth_pct"]

    def test_a_healthy_denominator_still_produces_one(self):
        b = _run(_quarters([2500.0] * 4))

        assert b.get("implied_growth_pct") is not None
