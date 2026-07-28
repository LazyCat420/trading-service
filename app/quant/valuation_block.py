"""Verified valuation baseline — the multiples the desk must not invent.

This module exists for the same reason `technical_baseline` does. The
2026-07-24 audit found 171 of 305 quant reports carried an RSI that matched no
number anywhere on the desk, 148 of them from runs that made zero tool calls.
The fix was not a better prompt: it was computing the quantity in code,
injecting it as the authoritative value, and overwriting the artifact
afterwards while recording every disagreement.

Valuation had the same shape of hole, one layer down. Before this module the
pipeline had NO valuation math at all — `grep -rniE 'dcf|wacc|terminal_value'`
returned nothing, `ev_to_ebitda` existed only as a string scraped off Finviz,
and `intrinsic_value_estimate` was free text a board persona was asked to
guess. An agent asked "is this overvalued" with no computed multiple in front
of it invents one exactly the way the quant invented RSI.

What the model keeps: the verdict, the fair-value call, the basis, what would
change its mind. Judgment is the part no table can do.

## One honesty decision worth stating up front

**EBITDA cannot be computed anywhere in this system.** `financial_history`
carries revenue / gross_profit / operating_income / net_income / eps /
free_cash_flow, and `grep -rni "depreciation\\|amortization" app/` returns zero
hits — there is no D&A column to add back. So this module computes **EV/EBIT**
from TTM operating income and *labels it EV/EBIT*, printing the vendor's
scraped `ev_to_ebitda` beside it tagged unverified.

Silently calling EBIT "EBITDA" would understate the multiple for every
capital-intensive name. That is the invented-RSI failure in a new costume,
except it would be ours, in code, and no reconciliation pass could catch it.
"""

from __future__ import annotations

import logging
from datetime import date

from app.quant.technical_baseline import _finite

logger = logging.getLogger(__name__)

# ── Staleness ──
# 45 days, not the technical baseline's 3. Fundamentals move on a filing
# cadence, not a daily one: a snapshot from last month is the CURRENT view of a
# company that reports quarterly. Borrowing the technicals threshold here would
# label every healthy row stale.
_STALE_FUNDAMENTALS_DAYS = 45

# ── Reverse-DCF assumptions ──
# These are judgment calls, not data, so they are named constants and the block
# PRINTS them. An implied-growth number whose discount rate is invisible reads
# as parameter-free truth, which is the opposite of what it is.
_ERP = 0.05              # equity risk premium over the 10Y
_TERMINAL_GROWTH = 0.025  # Gordon terminal, ~long-run nominal GDP
_RF_FALLBACK = 0.042     # used when macro_indicators has no TREASURY_10Y
_DEFAULT_BETA = 1.0
_DCF_YEARS = 10

# Tax rate applied to EBIT when the reverse DCF has to fall back to an
# operating-income stream (see _dcf_flow). Explicit and printed, because the
# alternative — discounting pre-tax EBIT as if it were owner cash — overstates
# the flow for EVERY ticker, which biases EVERY verdict toward "cheap". A
# stated assumption is recoverable; a systematic directional bias is not.
_TAX_RATE = 0.21
# Bisection bracket. Outside it we report OUT OF BRACKET rather than clamping:
# a clamped 60% is a number, and a number is what the model will quote.
_G_LO, _G_HI = -0.30, 0.60

# Print a vendor cross-check line when ours and theirs differ by more than this.
# Disagreement is a data-quality signal; the block never silently picks a winner.
_VENDOR_DISAGREE = 0.10

# Reconcile tolerance, RELATIVE — see reconcile_valuation_metrics.
#
# 1%, not the 5% this started at. 5% was calibrated on nothing and let the
# single most likely valuation error straight through: an agent reporting
# MARKET CAP as enterprise value. On the test fixture that is $398B against a
# true $414.5B — a 4.0% gap, i.e. inside a 5% band, for a company whose net
# debt is 4% of EV. Most large caps sit there, so the guard would have been
# silently inert on exactly the population it was written for.
#
# 1% is still far beyond any plausible rounding: a 3-significant-figure figure
# copied from the block ("18.4" for 18.39) is off by 0.05%. And the asymmetry
# favours tightness — an over-eager correction merely rewrites a rounded value
# with the exact one, while a missed correction sends a wrong multiple to the
# Board.
_RECONCILE_TOLERANCE = 0.01

# Fields we can verify deterministically. Anything not listed here stays the
# model's to judge — see reconcile_valuation_metrics.
VERIFIED_NUMERIC_FIELDS = (
    "enterprise_value", "ev_to_ebit", "ev_to_sales", "fcf_yield_pct",
    "ev_to_fcf", "pe_ratio", "peg", "net_debt_to_ebit",
    "revenue_cagr_pct", "ebit_cagr_pct", "fcf_cagr_pct", "eps_cagr_pct",
    "implied_growth_pct",
)

_FUNDAMENTAL_COLS = (
    "snapshot_date", "market_cap", "pe_ratio", "forward_pe", "peg_ratio",
    "ev_to_ebitda", "ev_to_sales", "price_to_fcf", "revenue", "revenue_growth",
    "beta", "eps_ttm", "eps_growth_next_5y", "eps_growth_past_5y", "roic",
    "oper_margin", "shares_outstanding",
)
_BALANCE_COLS = ("period_end", "total_equity", "cash", "total_debt")
_HISTORY_COLS = ("period_end", "revenue", "operating_income", "net_income",
                 "eps", "free_cash_flow")

# The TTM fields we sum from quarterlies. Each is all-or-nothing across the
# four rows — see _fetch_ttm.
_TTM_FIELDS = ("revenue", "operating_income", "net_income", "free_cash_flow")


# ──────────────────────────────────────────────────────────────────────
# Fetch
# ──────────────────────────────────────────────────────────────────────

_DATE_KEYS = ("snapshot_date", "period_end", "_period_end")


def _sanitize(d: dict) -> dict:
    """Re-apply `_finite` to every non-date value in a fetched row."""
    if not isinstance(d, dict):
        return {}
    return {
        k: (v if (k in _DATE_KEYS or k.startswith("_")) else _finite(v))
        for k, v in d.items()
    }


def _fetch_fundamentals(ticker: str) -> dict | None:
    from app.db.connection import get_db

    with get_db() as db:
        row = db.execute(
            f"SELECT {', '.join(_FUNDAMENTAL_COLS)} FROM fundamentals "
            "WHERE ticker = %s ORDER BY snapshot_date DESC LIMIT 1",
            [ticker],
        ).fetchone()
    if not row:
        return None
    return {
        k: (v if k == "snapshot_date" else _finite(v))
        for k, v in zip(_FUNDAMENTAL_COLS, row)
    }


def _fetch_balance_sheet(ticker: str) -> dict | None:
    """Latest balance sheet row, whatever its cadence.

    `balance_sheet` has NO period_type column, and the two writers disagree:
    yfinance_collector writes ANNUAL rows, fmp_collector writes QUARTERLY. So a
    200-day-old row is the normal case for a yfinance-sourced ticker, not an
    outage. The age is reported; nothing gates on it.
    """
    from app.db.connection import get_db

    with get_db() as db:
        row = db.execute(
            f"SELECT {', '.join(_BALANCE_COLS)} FROM balance_sheet "
            "WHERE ticker = %s ORDER BY period_end DESC LIMIT 1",
            [ticker],
        ).fetchone()
    if not row:
        return None
    return {
        k: (v if k == "period_end" else _finite(v))
        for k, v in zip(_BALANCE_COLS, row)
    }


def _fetch_periods(ticker: str, period_type: str, limit: int) -> list[dict]:
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            f"SELECT {', '.join(_HISTORY_COLS)} FROM financial_history "
            "WHERE ticker = %s AND period_type = %s "
            "ORDER BY period_end DESC LIMIT %s",
            [ticker, period_type, limit],
        ).fetchall()
    return [
        {k: (v if k == "period_end" else _finite(v))
         for k, v in zip(_HISTORY_COLS, r)}
        for r in rows
    ]


def _fetch_ttm(ticker: str) -> dict:
    """Trailing twelve months, summed from the last four quarterly rows.

    A field is summed ONLY when all four rows exist and the field is non-null
    in every one of them. A 3-of-4 sum is a 25% understatement wearing the
    costume of a real number — it would flow into EV/EBIT and FCF yield looking
    exactly like a computed multiple, and nothing downstream could tell.
    """
    quarters = _fetch_periods(ticker, "quarterly", 4)
    out: dict = {"_quarters": len(quarters)}
    if len(quarters) < 4:
        return out
    out["_period_end"] = quarters[0]["period_end"]
    for field in _TTM_FIELDS:
        vals = [q.get(field) for q in quarters]
        if all(v is not None for v in vals):
            out[field] = sum(vals)
    return out


def _fetch_risk_free() -> float | None:
    """10Y Treasury as a decimal. Stored in PERCENT by fred_collector (DGS10)."""
    from app.db.connection import get_db

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM macro_indicators "
                "WHERE indicator = 'TREASURY_10Y' AND country = 'US' "
                "ORDER BY date DESC LIMIT 1"
            ).fetchone()
        val = _finite(row[0]) if row else None
        return val / 100.0 if val is not None else None
    except Exception as e:  # noqa: BLE001 — an assumption, never a blocker
        logger.debug("[ValuationBlock] risk-free fetch failed: %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────
# Math
# ──────────────────────────────────────────────────────────────────────

def _cagr(latest: float | None, oldest: float | None, years: float) -> float | None:
    """Compound annual growth as a PERCENT, or None when meaningless.

    Both endpoints must be positive. A sign flip (loss to profit, or the
    reverse) has no compound growth rate — the formula would return a complex
    number or a confident nonsense figure, and "revenue grew 40%/yr" computed
    across a sign change is worse than no line at all.
    """
    if latest is None or oldest is None or years <= 0:
        return None
    if latest <= 0 or oldest <= 0:
        return None
    return ((latest / oldest) ** (1.0 / years) - 1.0) * 100.0


# Minimum distinct annual observations before a CAGR means anything. Two points
# is a line through two dots, not a trend.
_MIN_CAGR_POINTS = 3


def _series_cagr(rows: list[dict], field: str) -> tuple[float | None, str]:
    """CAGR for `field` across the outermost rows that actually CARRY it.

    Endpoints are selected by whether the value exists, NOT by position in the
    result set. That distinction is the whole function: `financial_history`
    carries all-null placeholder rows for the year before coverage begins
    (measured: COST and GE both have a fully-null 2021 row sitting under four
    clean years), so an oldest-by-position endpoint reads None and the CAGR
    silently vanishes for a ticker whose data is perfectly good. Only 5 of 510
    tickers have a null NEWEST annual row — every real failure was at the far
    end, on data that was there all along.

    Per-field rather than per-row for the same reason: Citigroup has four clean
    years of revenue and no operating_income at all, and one missing column
    must not delete the metric next to it.
    """
    pts = [(r["period_end"], r[field])
           for r in rows
           if r.get(field) is not None and r.get("period_end") is not None]
    if len(pts) < _MIN_CAGR_POINTS:
        return None, (f"only {len(pts)} annual observations carry this field "
                      f"— need {_MIN_CAGR_POINTS}")

    pts.sort(key=lambda p: p[0], reverse=True)
    (new_end, new_val), (old_end, old_val) = pts[0], pts[-1]
    # Span from the actual dates, not a row count: a gap year would otherwise
    # compress the same growth into fewer years and overstate the rate.
    years = (new_end - old_end).days / 365.25
    if years < 1.5:
        return None, "annual observations span less than 2 years"

    val = _cagr(new_val, old_val, years)
    if val is None:
        return None, ("endpoints are not both positive — a sign flip has no "
                      "compound growth rate")
    return val, f"{old_end} → {new_end} ({years:.1f}y)"


def _dcf_pv(fcf: float, g: float, r: float,
            years: int = _DCF_YEARS, gt: float = _TERMINAL_GROWTH) -> float:
    """Present value of `fcf` growing at `g` for `years`, plus a Gordon terminal."""
    pv = 0.0
    cf = fcf
    for t in range(1, years + 1):
        cf *= (1.0 + g)
        pv += cf / ((1.0 + r) ** t)
    terminal = cf * (1.0 + gt) / (r - gt)
    return pv + terminal / ((1.0 + r) ** years)


def _dcf_flow(fcf_ttm: float | None,
              ebit_ttm: float | None) -> tuple[float | None, str, str | None]:
    """Pick the cash-flow stream the reverse DCF discounts. (flow, label, caveat)

    Free cash flow is the right input and is preferred whenever it exists. It
    almost never does: MEASURED 2026-07-27, `financial_history.free_cash_flow`
    is non-null in **1 of 3060** quarterly rows and **0 of 2412** annual rows,
    across every ticker in the database. The cause is not a gap in coverage but
    an active overwrite — `yfinance_collector` hardcodes None for the column
    ("FCF from cash flow statement, not income stmt") and its upsert carries
    `free_cash_flow = EXCLUDED.free_cash_flow`, so each yfinance run clobbers
    whatever fmp_collector wrote. yfinance owns 955 of 1073 latest snapshots.

    So the fallback is NOPAT = EBIT x (1 - tax), which has 422/487 coverage.
    That is a coherent pairing rather than a convenience: enterprise value is
    the claim on the whole pre-financing operating stream, and NOPAT is a
    pre-financing operating stream. What it is NOT is cash — it ignores capex
    and working capital, so for a capital-hungry business the implied growth
    rate will read low. The label says so, every time.
    """
    if fcf_ttm is not None and fcf_ttm > 0:
        return fcf_ttm, "TTM free cash flow", None
    if ebit_ttm is not None and ebit_ttm > 0:
        return (
            ebit_ttm * (1.0 - _TAX_RATE),
            f"NOPAT (TTM EBIT less an assumed {_TAX_RATE:.0%} tax)",
            "no free-cash-flow data exists anywhere in this system, so this is "
            "an OPERATING-INCOME growth rate, not a cash one — it ignores capex "
            "and working capital and will read LOW for capital-hungry names",
        )
    return None, "", None


def _implied_growth(ev: float | None, flow: float | None,
                    r: float | None) -> tuple[float | None, str | None]:
    """Solve for the growth rate today's EV implies. (percent, reason).

    Returns (None, reason) rather than a number whenever the question is not
    well posed. The reason is printed — an absent line and an unsolvable one
    are different claims, and the agent needs to tell them apart.
    """
    if ev is None or ev <= 0:
        return None, "enterprise value not computable"
    if flow is None:
        return None, ("no discountable flow on file — neither TTM free cash "
                      "flow nor positive TTM operating income")
    if flow <= 0:
        return None, "the flow is negative — a growth rate cannot be solved"
    if r is None or r <= _TERMINAL_GROWTH + 0.005:
        return None, "discount rate not above terminal growth"

    lo, hi = _G_LO, _G_HI
    if _dcf_pv(flow, lo, r) > ev:
        return None, f"price implies growth below {lo:.0%} — outside the solver bracket"
    if _dcf_pv(flow, hi, r) < ev:
        return None, f"price implies growth above {hi:.0%} — outside the solver bracket"

    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _dcf_pv(flow, mid, r) < ev:
            lo = mid
        else:
            hi = mid
    return ((lo + hi) / 2.0) * 100.0, None


# ──────────────────────────────────────────────────────────────────────
# Baseline
# ──────────────────────────────────────────────────────────────────────

def compute_valuation_baseline(ticker: str) -> dict:
    """Computed valuation metrics for `ticker`.

    Returns {} when there is no fundamentals row at all — callers must treat an
    empty dict as "no verified valuation", never as "fairly valued".

    Three states are kept distinct throughout, and none of them is a zero:
      - a key present in the dict was computed
      - a key in `not_computable` was attempted and could not be resolved, with
        the reason
      - a key in neither was never reachable given the inputs on file
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {}

    try:
        fund = _fetch_fundamentals(ticker)
        if not fund:
            return {}

        bs = _fetch_balance_sheet(ticker) or {}
        ttm = _fetch_ttm(ticker)
        annual = _fetch_periods(ticker, "annual", 6)

        # Sanitize HERE, where the values are consumed — not only in the
        # fetchers. Defense in depth, for the reason technical_baseline gives
        # at its own second pass: NaN compares false against every threshold,
        # so a single one slipping through lands in valuation_metrics reading
        # as a real multiple to everything downstream. A fetcher is one edit
        # away from being the hole.
        fund = _sanitize(fund)
        bs = _sanitize(bs)
        ttm = _sanitize(ttm)
        annual = [_sanitize(a) for a in annual]

        b: dict = {
            "as_of": fund["snapshot_date"],
            "provenance": {},
            "not_computable": {},
            "vendor": {},
        }
        nc = b["not_computable"]
        prov = b["provenance"]

        def _mark(field: str, reason: str) -> None:
            nc[field] = reason

        # ── Staleness of the fundamentals snapshot ──
        try:
            age = (date.today() - fund["snapshot_date"]).days
            b["age_days"] = age
            b["stale"] = age > _STALE_FUNDAMENTALS_DAYS
        except Exception:
            b["stale"] = False

        if bs.get("period_end"):
            try:
                b["balance_sheet_age_days"] = (date.today() - bs["period_end"]).days
            except Exception:
                pass
            b["balance_sheet_as_of"] = bs["period_end"]

        market_cap = fund.get("market_cap")
        cash = bs.get("cash")
        debt = bs.get("total_debt")

        # ── Enterprise value ──
        if market_cap is None:
            _mark("enterprise_value", "no market cap on file")
        elif debt is None or cash is None:
            _mark("enterprise_value",
                  "no balance sheet row — net debt unknown, and market cap "
                  "alone is NOT enterprise value")
        else:
            b["net_debt"] = debt - cash
            b["enterprise_value"] = market_cap + debt - cash
            b["market_cap"] = market_cap
            b["cash"] = cash
            b["total_debt"] = debt
            prov["enterprise_value"] = (
                f"market cap {fund['snapshot_date']}, balance sheet "
                f"{bs.get('period_end', 'unknown')}"
            )

        ev = b.get("enterprise_value")
        ebit = ttm.get("operating_income")
        rev_ttm = ttm.get("revenue")
        fcf_ttm = ttm.get("free_cash_flow")

        if ttm.get("_quarters", 0) < 4:
            _mark("ttm", f"only {ttm.get('_quarters', 0)} of 4 quarterly rows on "
                         "file — no TTM computed (a partial sum would understate)")
        else:
            prov["ttm"] = f"4 quarters ending {ttm.get('_period_end')}"
            # ONE line naming all of them, not one line each. Four consecutive
            # "null in at least one of the 4 quarters" bullets is prompt bloat
            # that buries the metrics which DID compute.
            absent = [f for f in _TTM_FIELDS if f not in ttm]
            if absent:
                _mark("ttm fields", ", ".join(absent)
                      + " — null in at least one of the 4 quarters")

        # ── EV/EBIT (NOT EBITDA — see the module docstring) ──
        if ev is not None and ebit is not None and ebit > 0:
            b["ev_to_ebit"] = ev / ebit
            b["ebit_ttm"] = ebit
        elif ev is not None and ebit is not None and ebit <= 0:
            b["ebit_ttm"] = ebit
            _mark("ev_to_ebit", "TTM operating income is negative — the multiple "
                                "is not meaningful")

        # ── EV/Sales ──
        if ev is not None and rev_ttm is not None and rev_ttm > 0:
            b["ev_to_sales"] = ev / rev_ttm
            b["revenue_ttm"] = rev_ttm

        # ── FCF yield and EV/FCF ──
        if fcf_ttm is not None:
            b["fcf_ttm"] = fcf_ttm
            if market_cap:
                # Printed negative when it is negative. A suppressed negative
                # yield reads as "no data" when it is in fact the single most
                # important fact about the company.
                b["fcf_yield_pct"] = fcf_ttm / market_cap * 100.0
            if ev and ev > 0 and fcf_ttm > 0:
                b["ev_to_fcf"] = ev / fcf_ttm
            elif ev and fcf_ttm <= 0:
                _mark("ev_to_fcf", "TTM free cash flow is negative")

        # ── Leverage ──
        if b.get("net_debt") is not None and ebit is not None:
            if ebit > 0:
                b["net_debt_to_ebit"] = b["net_debt"] / ebit
            else:
                _mark("net_debt_to_ebit", "EBIT negative — leverage multiple not "
                                          "meaningful")

        # ── P/E and PEG ──
        # UNITS: percent-like fundamentals columns are stored as FRACTIONS
        # (schema_pg.sql: 0.27 == 27%), so the growth rate must be scaled to
        # percent before dividing a P/E by it. Getting this wrong yields a PEG
        # 100x off, which is exactly the kind of number that reads as plausible.
        pe = fund.get("pe_ratio")
        if pe is not None:
            b["pe_ratio"] = pe
        g5 = fund.get("eps_growth_next_5y")
        if pe is not None and g5 is not None and g5 > 0:
            b["peg"] = pe / (g5 * 100.0)
            b["eps_growth_next_5y_pct"] = g5 * 100.0
        elif pe is not None and (g5 is None or g5 <= 0):
            _mark("peg", "5y EPS growth estimate missing or non-positive — PEG "
                         "has no meaning against a negative denominator")

        # ── CAGRs from the annual series, endpoints chosen per-field ──
        for src, dest in (("revenue", "revenue_cagr_pct"),
                          ("operating_income", "ebit_cagr_pct"),
                          ("free_cash_flow", "fcf_cagr_pct"),
                          ("eps", "eps_cagr_pct")):
            val, note = _series_cagr(annual, src)
            if val is not None:
                b[dest] = val
                prov[dest] = note
            else:
                _mark(dest, note)

        # ── Reverse DCF ──
        rf = _fetch_risk_free()
        b["risk_free_pct"] = (rf if rf is not None else _RF_FALLBACK) * 100.0
        b["risk_free_assumed"] = rf is None
        beta = fund.get("beta")
        b["beta_assumed"] = beta is None
        beta = beta if beta is not None else _DEFAULT_BETA
        b["beta"] = beta
        r = (rf if rf is not None else _RF_FALLBACK) + beta * _ERP
        b["discount_rate_pct"] = r * 100.0
        b["terminal_growth_pct"] = _TERMINAL_GROWTH * 100.0

        flow, flow_label, flow_caveat = _dcf_flow(fcf_ttm, ebit)
        b["dcf_flow_basis"] = flow_label
        if flow_caveat:
            b["dcf_flow_caveat"] = flow_caveat
        implied, reason = _implied_growth(ev, flow, r)
        if implied is not None:
            b["implied_growth_pct"] = implied
        else:
            _mark("implied_growth_pct", reason or "not computable")

        # ── Vendor cross-checks. Never silently pick a winner. ──
        for ours, theirs, label in (("ev_to_sales", "ev_to_sales", "EV/Sales"),
                                    ("peg", "peg_ratio", "PEG")):
            mine, vendor = b.get(ours), fund.get(theirs)
            if mine is not None and vendor is not None and vendor != 0:
                if abs(mine - vendor) / abs(vendor) > _VENDOR_DISAGREE:
                    b["vendor"][label] = {"ours": mine, "vendor": vendor}
        if fund.get("ev_to_ebitda") is not None:
            b["vendor_ev_to_ebitda"] = fund["ev_to_ebitda"]

        return b
    except Exception as e:  # noqa: BLE001 — never block a cycle on grounding
        logger.warning("[ValuationBlock] %s failed: %s: %s",
                       ticker, type(e).__name__, e)
        return {}


# ──────────────────────────────────────────────────────────────────────
# Block
# ──────────────────────────────────────────────────────────────────────

_NO_DATA = (
    "VALUATION MATH: **NONE ON FILE** — this ticker has no stored fundamentals "
    "row, so there is no verified enterprise value, multiple, or implied growth "
    "rate. Do NOT state valuation multiples as fact and do NOT infer them from "
    "the price; say so explicitly in data_gaps and let that missing evidence "
    "lower your confidence."
)


def _money(v: float) -> str:
    """$412.6B / $1.2M / $940 — never scientific notation in a prompt."""
    a = abs(v)
    for cutoff, suffix, div in ((1e12, "T", 1e12), (1e9, "B", 1e9),
                               (1e6, "M", 1e6), (1e3, "K", 1e3)):
        if a >= cutoff:
            return f"${v / div:,.1f}{suffix}"
    return f"${v:,.0f}"


def build_valuation_block(ticker: str) -> str:
    """The injectable briefing section.

    Never returns "" — a silent empty block is how a ticker reaches the board
    with nothing and no complaint in the logs (the ASIC/ARCVF failure that put
    the NO DATA branch into technical_baseline).
    """
    b = compute_valuation_baseline(ticker)
    if not b:
        return _NO_DATA

    stale = bool(b.get("stale"))
    header = (
        "STORED VALUATION MATH (computed in code from stored filings — the "
        "snapshot is STALE, see below; prefer a live tool call if you have one, "
        "otherwise treat these as the best available anchor and say so in "
        "data_gaps rather than inventing different numbers):"
        if stale else
        "## PRECOMPUTED VALUATION MATH (computed in code this cycle from stored "
        "filings — these are the authoritative values. Cite them directly; do "
        "NOT restate them from memory and do NOT re-derive them from prose.)"
    )
    lines = [header]

    src = [f"fundamentals {b['as_of']}"]
    if b.get("age_days") is not None:
        src[-1] += f" ({b['age_days']}d)"
    if b.get("balance_sheet_as_of"):
        note = ""
        if (b.get("balance_sheet_age_days") or 0) > 120:
            note = (" — this table is annual-cadence for most tickers, so this "
                    "is normal, not an outage")
        src.append(f"balance sheet {b['balance_sheet_as_of']} "
                   f"({b.get('balance_sheet_age_days', '?')}d{note})")
    if b.get("provenance", {}).get("ttm"):
        src.append(f"TTM = {b['provenance']['ttm']}")
    lines.append("  inputs: " + "; ".join(src))

    if "enterprise_value" in b:
        lines.append(
            f"- Enterprise value {_money(b['enterprise_value'])} = market cap "
            f"{_money(b['market_cap'])} + total debt {_money(b['total_debt'])} "
            f"- cash {_money(b['cash'])} (net debt {_money(b['net_debt'])})"
        )

    if "ev_to_ebit" in b:
        line = (f"- EV/EBIT {b['ev_to_ebit']:.1f}x (TTM operating income "
                f"{_money(b['ebit_ttm'])}). **No D&A is stored anywhere in this "
                f"system, so EBITDA cannot be computed** — this is EV/EBIT, a "
                f"HIGHER multiple than EV/EBITDA would be.")
        if b.get("vendor_ev_to_ebitda") is not None:
            line += f" Vendor EV/EBITDA (unverified): {b['vendor_ev_to_ebitda']:.1f}x"
        lines.append(line)

    if "ev_to_sales" in b:
        lines.append(f"- EV/Sales {b['ev_to_sales']:.1f}x (TTM revenue "
                     f"{_money(b['revenue_ttm'])})")

    if "fcf_yield_pct" in b:
        line = (f"- FCF yield {b['fcf_yield_pct']:.1f}% on equity "
                f"({_money(b['fcf_ttm'])} TTM FCF)")
        if "ev_to_fcf" in b:
            line += f"; EV/FCF {b['ev_to_fcf']:.1f}x"
        if b["fcf_yield_pct"] < 0:
            line += " — **negative free cash flow**"
        lines.append(line)

    if "pe_ratio" in b:
        line = f"- P/E {b['pe_ratio']:.1f}"
        if "peg" in b:
            line += (f" vs 5y EPS growth {b['eps_growth_next_5y_pct']:.1f}%/yr "
                     f"→ PEG {b['peg']:.2f}")
        lines.append(line)

    if "net_debt_to_ebit" in b:
        lines.append(f"- Net debt / EBIT {b['net_debt_to_ebit']:.2f}x")

    cagr_bits = [
        f"{label} {b[key]:+.1f}%/yr"
        for key, label in (("revenue_cagr_pct", "revenue"),
                           ("ebit_cagr_pct", "EBIT"),
                           ("fcf_cagr_pct", "FCF"),
                           ("eps_cagr_pct", "EPS"))
        if key in b
    ]
    if cagr_bits:
        span = (b.get("provenance", {}).get("revenue_cagr_pct")
                or b.get("provenance", {}).get("ebit_cagr_pct") or "")
        lines.append(f"- Realized annual CAGR ({span}): " + ", ".join(cagr_bits))

    # ── The headline. Assumptions printed inline, never implicit. ──
    assumptions = (
        f"{b['discount_rate_pct']:.1f}% discount rate (10Y "
        f"{b['risk_free_pct']:.1f}%{' ASSUMED' if b.get('risk_free_assumed') else ''}"
        f" + beta {b['beta']:.2f}{' ASSUMED' if b.get('beta_assumed') else ''}"
        f" x {_ERP:.1%} ERP) and {b['terminal_growth_pct']:.1f}% terminal growth"
    )
    if "implied_growth_pct" in b:
        line = (f"- REVERSE DCF on {b['dcf_flow_basis']}: at a {assumptions}, "
                f"today's enterprise value implies "
                f"**{b['implied_growth_pct']:.1f}%/yr growth in that flow for "
                f"{_DCF_YEARS} years**.")
        # Like-for-like first: an implied NOPAT growth rate is compared against
        # realized EBIT growth, not against revenue or EPS. Leading with a
        # mismatched series is how "the price implies 17% and the company grew
        # 7%" gets asserted when the two numbers measure different things.
        like = "ebit_cagr_pct" if b["dcf_flow_basis"].startswith("NOPAT") \
            else "fcf_cagr_pct"
        comps = []
        if like in b:
            comps.append(f"realized {'EBIT' if like.startswith('ebit') else 'FCF'} "
                         f"CAGR was {b[like]:+.1f}%/yr (the like-for-like series)")
        for key, label in (("revenue_cagr_pct", "revenue"),
                           ("eps_cagr_pct", "EPS")):
            if key in b:
                comps.append(f"realized {label} CAGR was {b[key]:+.1f}%/yr")
        if "eps_growth_next_5y_pct" in b:
            comps.append(f"consensus 5y EPS growth is "
                         f"{b['eps_growth_next_5y_pct']:.1f}%/yr")
        if comps:
            line += " For comparison, " + "; ".join(comps) + "."
        else:
            line += (" NOTE: no realized growth series is on file for this "
                     "ticker, so there is nothing to compare it against — the "
                     "implied rate alone does not say cheap or expensive.")
        lines.append(line)
        if b.get("dcf_flow_caveat"):
            lines.append(f"  ⚠ {b['dcf_flow_caveat']}")
    else:
        lines.append(f"- REVERSE DCF: NOT COMPUTABLE — "
                     f"{b['not_computable'].get('implied_growth_pct', 'unknown')} "
                     f"(assumptions would have been a {assumptions})")

    # ── Vendor disagreements ──
    for label, cmp_ in (b.get("vendor") or {}).items():
        lines.append(f"- ⚠ {label}: ours {cmp_['ours']:.2f} vs vendor "
                     f"{cmp_['vendor']:.2f} — a >10% gap between an independently "
                     f"scraped figure and ours is a DATA QUALITY signal; do not "
                     f"pick one silently, note it in data_gaps")

    # ── Absence, stated explicitly. A missing line is not a zero. ──
    nc = {k: v for k, v in (b.get("not_computable") or {}).items()
          if k != "implied_growth_pct"}
    for field, reason in nc.items():
        lines.append(f"- NOT COMPUTABLE: {field} ({reason})")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Reconcile
# ──────────────────────────────────────────────────────────────────────

def reconcile_valuation_metrics(
    artifact: dict, ticker: str, *, model_used_tools: bool = False
) -> dict:
    """Replace the artifact's verifiable valuation_metrics with computed values.

    Returns a report of what disagreed, so fabrication stays MEASURABLE rather
    than merely suppressed. `reconcile_risk_metrics` exists at all because
    somebody was able to count 171 invented RSIs out of 305; this returns the
    same shape so the equivalent count is possible here.

    The model's originals are preserved under `_model_reported_valuation` — the
    point is to stop a bad number reaching the Board, not to hide that it was
    produced.

    Interpretive fields are NEVER touched: verdict, fair_value_estimate,
    fair_value_basis, price_implied_assumption, what_would_change_my_mind,
    thesis_direction, confidence. Judgment is the agent's actual job, and this
    module has no opinion about it.
    """
    if not isinstance(artifact, dict):
        return {}
    metrics = artifact.get("valuation_metrics")
    if not isinstance(metrics, dict):
        return {}

    baseline = compute_valuation_baseline(ticker)
    if not baseline:
        return {}

    stale = bool(baseline.get("stale"))
    apply_corrections = (not stale) or (not model_used_tools)

    corrected: dict = {}
    original: dict = {}

    for field in VERIFIED_NUMERIC_FIELDS:
        verified = baseline.get(field)
        if verified is None:
            continue
        stated = metrics.get(field)
        try:
            stated_f = float(stated)
        except (TypeError, ValueError):
            stated_f = None
        if stated_f is not None and stated_f != stated_f:  # NaN
            stated_f = None

        # RELATIVE tolerance, not the absolute 1.0 that technical_baseline uses
        # for RSI. These fields span 1e-2 (yields) to 1e11 (enterprise value):
        # an absolute threshold is simultaneously far too tight for EV and
        # meaningless for a percentage.
        disagrees = (
            stated_f is None
            or abs(stated_f - verified) / max(abs(verified), 1e-9)
            > _RECONCILE_TOLERANCE
        )
        if disagrees:
            if stated_f is not None:
                corrected[field] = {"model": stated_f, "verified": verified}
                original[field] = stated_f
            if apply_corrections:
                metrics[field] = verified

    if original and apply_corrections:
        artifact["_model_reported_valuation"] = original
    elif corrected:
        artifact["_unreconciled_valuation"] = corrected

    if stale:
        artifact.setdefault("data_gaps", []).append(
            f"Estimate: stored fundamentals snapshot is "
            f"{baseline.get('age_days')} days old (as of {baseline['as_of']})"
        )

    return {
        "corrected": corrected,
        "applied": apply_corrections,
        "stale": stale,
        "as_of": str(baseline.get("as_of", "")),
    }
