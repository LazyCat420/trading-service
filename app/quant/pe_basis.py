"""Which P/E is the desk looking at, and over WHAT WINDOW.

THE CASE THAT PRODUCED THIS MODULE
==================================
2026-08-20, COF. The desk read a P/E of 12.2; finviz showed 79. Both were
arithmetically correct, and neither was stale — our row was written that day.
They were summing DIFFERENT TRAILING TWELVE MONTHS, and `financial_history`,
which we already store, says so outright:

    2025-06-30   EPS  -8.58   <- the Discover merger charge
    2025-09-30   EPS  +4.83
    2025-12-31   EPS  +3.26
    2026-03-31   EPS  +3.34
    2026-06-30   EPS  +4.73

    TTM including the charge quarter = 2.85  ->  P/E ~= 80   (finviz)
    TTM once it rolls off             = 16.16 ->  P/E ~= 14   (yfinance)

A P/E without its window is not a number, it is two numbers wearing one label.
Nothing in the pipeline recorded the window, so the two were interchangeable —
and which one the desk saw depended on **which collector ran last that day**.

WHY NOT JUST PREFER ONE VENDOR
==============================
Because the vendor that is right FLIPS BY TICKER. Measured over the 501
tickers carrying both sources, 16 disagree by >=2x, and adjudicating each
against our own stored `market_cap / net_income`:

    COF   yf 12.2   finviz 77.2   implied 13.8   -> yfinance right
    DD    yf 59.4   finviz 347.4  implied 61.6   -> yfinance right
    NRG   yf 30.1   finviz 150.0  implied 32.4   -> yfinance right
    AEP   yf 108.3  finviz  19.7  implied 21.8   -> FINVIZ right
    PSA   yf 155.9  finviz  30.9  implied 32.7   -> FINVIZ right

A fixed precedence would be wrong on a third of the disagreements it is meant
to settle. So this module does not rank vendors — it ranks each vendor's claim
against a quantity the desk computed itself.

WHAT IT DOES NOT DO
===================
It does not write, does not fetch, and does not change any action, confidence
or gate. It is a read-side resolver plus a recorded basis, in the shape
`hold_reason`/`disposition` established: derive from what is already stored, so
the answer recomputes over history instead of depending on when it ran.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How far apart two vendors must be before the disagreement is worth naming.
#: 2x is not a rounding difference or a stale-by-a-day difference; it is a
#: different denominator. Below that, the newest row is fine.
DISAGREEMENT_RATIO = 2.0

#: A P/E computed from a TTM window that is not ~12 months is not a TTM P/E.
#: Reuses valuation_block's measured bounds rather than inventing new ones:
#: four quarters span ~273 days end to end, widened for 52/53-week retail
#: calendars and filing jitter.
_SPAN_MIN_DAYS = 245
_SPAN_MAX_DAYS = 425

BASIS_TTM_EPS = "ttm_eps_from_quarterlies"
BASIS_MCAP_TTM_NI = "market_cap_over_ttm_net_income"
BASIS_MCAP_NI = "market_cap_over_net_income"
BASIS_NONE = "unadjudicated"


def _num(v: Any) -> float | None:
    """A finite float, or None. Rejects bool, strings and inf/nan.

    All three appear in `fundamentals` today: one `pe_ratio` is the string
    form and one (SDA) is `inf`. `float("inf")` compares happily against every
    threshold in this module and would win or lose every comparison silently.
    """
    # `float("12.2")` succeeds, so a plain try/float would silently accept the
    # string-typed row and hide the type defect instead of surfacing it. The
    # column's contract is numeric; a str in it is a writer bug (there is
    # exactly one in 10,781 rows) and it gets dropped from adjudication rather
    # than laundered into a valid-looking candidate.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def ttm_eps(ticker: str) -> dict:
    """TTM diluted EPS summed from the last four quarterly rows.

    All-or-nothing across the four, for the reason `_fetch_ttm` gives: a
    3-of-4 sum is a 25% understatement that looks exactly like a real number.
    Returns the window it used, because the window is the entire point.
    """
    out: dict = {"eps_ttm": None, "ttm_as_of": None, "quarters": 0,
                 "span_days": None, "span_ok": None}
    try:
        from app.quant.valuation_block import _fetch_periods
    except Exception as e:  # noqa: BLE001
        logger.debug("[pe_basis] %s: cannot import _fetch_periods: %s", ticker, e)
        return out

    try:
        quarters = _fetch_periods(ticker, "quarterly", 4)
    except Exception as e:  # noqa: BLE001
        logger.debug("[pe_basis] %s: quarterly fetch failed: %s", ticker, e)
        return out

    out["quarters"] = len(quarters)
    if len(quarters) < 4:
        return out

    values = [_num(q.get("eps")) for q in quarters]
    if any(v is None for v in values):
        return out

    try:
        newest, oldest = quarters[0]["period_end"], quarters[-1]["period_end"]
        span = (newest - oldest).days
        out["span_days"] = span
        out["span_ok"] = _SPAN_MIN_DAYS <= span <= _SPAN_MAX_DAYS
        out["ttm_as_of"] = newest
    except Exception:  # noqa: BLE001
        out["span_ok"] = None

    out["eps_ttm"] = sum(values)
    return out


def _ttm_net_income(ticker: str) -> float | None:
    """TTM net income, summed by `valuation_block._fetch_ttm`'s own rules.

    Strictly better than the point-in-time `fundamentals.net_income` field: it
    is a stated twelve-month window rather than whatever the vendor had in the
    column that day, which is the same defect being adjudicated here.
    """
    try:
        from app.quant.valuation_block import _fetch_ttm
        return _num(_fetch_ttm(ticker).get("net_income"))
    except Exception as e:  # noqa: BLE001
        logger.debug("[pe_basis] %s: TTM net income unavailable: %s", ticker, e)
        return None


def implied_pe(price: float | None, eps_total: float | None) -> float | None:
    """price / TTM EPS. None when EPS is <= 0 — a negative P/E is not a cheap
    stock, it is a company that lost money, and printing one invites exactly
    the misreading this module exists to stop."""
    p, e = _num(price), _num(eps_total)
    if p is None or e is None or e <= 0:
        return None
    return p / e


def _ratio(a: float, b: float) -> float:
    lo, hi = sorted((abs(a), abs(b)))
    return hi / lo if lo > 0 else float("inf")


def adjudicate(candidates: list[dict], reference: float | None,
               reference_basis: str = BASIS_TTM_EPS) -> dict:
    """Pick the vendor claim closest to a reference the desk computed itself.

    `candidates` are `{"source", "pe_ratio", "snapshot_date"}` dicts. With no
    usable reference this returns the NEWEST candidate and says so via
    `basis=unadjudicated` — the status quo, but labelled, so a reader can tell
    "we checked and yfinance agrees with our own arithmetic" apart from
    "nobody checked".
    """
    usable = [c for c in candidates if _num(c.get("pe_ratio")) is not None]
    result: dict = {
        "value": None, "source": None, "basis": BASIS_NONE,
        "reference": reference, "disagreement": None, "candidates": len(usable),
        "rejected": [],
    }
    if not usable:
        return result


    by_recency = sorted(
        usable, key=lambda c: (c.get("snapshot_date") is not None,
                               c.get("snapshot_date")), reverse=True)

    ref = _num(reference)
    if ref is None or ref <= 0:
        winner = by_recency[0]
        result.update(value=_num(winner.get("pe_ratio")),
                      source=winner.get("source"))
    else:
        winner = min(usable, key=lambda c: _ratio(_num(c["pe_ratio"]), ref))
        result.update(value=_num(winner.get("pe_ratio")),
                      source=winner.get("source"), basis=reference_basis)

    # The losers are RECORDED, never dropped. A disagreement that leaves no
    # trace is indistinguishable from agreement, and the whole finding here
    # was that nothing recorded one.
    result["rejected"] = [
        {"source": c.get("source"), "pe_ratio": _num(c.get("pe_ratio")),
         "snapshot_date": c.get("snapshot_date")}
        for c in usable if c is not winner
    ]

    spread = [_num(c["pe_ratio"]) for c in usable]
    if len(spread) > 1:
        worst = _ratio(max(spread), min(spread))
        if worst >= DISAGREEMENT_RATIO:
            result["disagreement"] = round(worst, 2)
    return result


def resolve_pe(ticker: str, *, price: float | None = None,
               rows: list[dict] | None = None) -> dict:
    """The desk-facing answer: one P/E, its basis, and what it beat.

    `rows` are `fundamentals` documents for the ticker (newest first). Passing
    them in keeps this function pure and testable; callers that have already
    read the collection should not read it twice.
    """
    rows = rows or []
    candidates, seen = [], set()
    for r in rows:
        src = r.get("source") or "unknown"
        if src in seen:
            continue  # newest row per source only
        seen.add(src)
        candidates.append({"source": src, "pe_ratio": r.get("pe_ratio"),
                           "snapshot_date": r.get("snapshot_date")})

    ttm = ttm_eps(ticker)
    reference = implied_pe(price, ttm.get("eps_ttm")) if ttm.get("span_ok") else None
    reference_basis = BASIS_TTM_EPS if reference is not None else BASIS_NONE

    # Second choice: market cap over TTM net income, both already stored.
    #
    # Deliberately preferred over reading a price. `price_history` is keyed
    # `(ticker, date, source)` and the vendors disagree by a mean 20% (yfinance
    # publishes adjusted closes, polygon raw), so an unpinned "latest close"
    # would import a 20% error into the very comparison meant to settle a
    # disagreement — and `test_price_history_one_vendor_guard` rightly refuses
    # the unpinned read. Callers holding an already-pinned price may still pass
    # one, which is why `price` stays in the signature.
    if reference is None:
        ttm_ni = _ttm_net_income(ticker)
        if ttm_ni is not None and ttm_ni > 0:
            for r in rows:
                mc = _num(r.get("market_cap"))
                if mc:
                    reference = mc / ttm_ni
                    reference_basis = BASIS_MCAP_TTM_NI
                    break

    # Fallback reference: the desk's own market_cap / net_income off the same
    # row. Weaker than TTM EPS (net_income is a point-in-time snapshot field,
    # not a summed window) but it is what caught COF, and it is free.
    if reference is None:
        for r in rows:
            mc, ni = _num(r.get("market_cap")), _num(r.get("net_income"))
            if mc and ni and ni > 0:
                reference = mc / ni
                reference_basis = BASIS_MCAP_NI
                break

    out = adjudicate(candidates, reference, reference_basis=reference_basis)
    out["reference_basis"] = reference_basis if reference is not None else None
    out["eps_ttm"] = ttm.get("eps_ttm")
    out["ttm_as_of"] = ttm.get("ttm_as_of")
    out["ttm_span_days"] = ttm.get("span_days")
    out["ttm_window_ok"] = ttm.get("span_ok")
    return out


def describe(resolved: dict) -> str:
    """One line for a prompt or a report. Says the window, or says it cannot."""
    if not resolved or resolved.get("value") is None:
        return "P/E unavailable"
    parts = [f"P/E {resolved['value']:.1f}"]
    as_of = resolved.get("ttm_as_of")
    if resolved.get("eps_ttm") is not None and as_of is not None:
        parts.append(f"(TTM EPS {resolved['eps_ttm']:.2f} through {str(as_of)[:10]})")
    else:
        parts.append("(TTM window UNKNOWN — vendor figure, basis unverified)")
    if resolved.get("source"):
        parts.append(f"[{resolved['source']}]")
    if resolved.get("disagreement"):
        rej = ", ".join(
            f"{r['source']} {r['pe_ratio']:.1f}"
            for r in resolved.get("rejected", []) if r.get("pe_ratio") is not None
        )
        parts.append(f"⚠ sources disagree {resolved['disagreement']}x — also saw {rej}")
    return " ".join(parts)
