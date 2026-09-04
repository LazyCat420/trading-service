"""Verified fundamental baseline — the ratios the desk must not invent.

The third module in the same family as `technical_baseline` and
`valuation_block`, and it closes the last hole of the three.

## Why this exists

The 2026-07-28 fidelity audit measured every V3 agent's numeric output against
what could be recomputed from stored data. The fundamental analyst was the only
research desk that emitted **no numeric fields at all**: across 163 artifacts in
seven days its sole numbers were `confidence` and `_quality_score`, both
metadata about itself. Its schema is prose — summary, pillars, risks, catalysts.

A prose-only artifact cannot be reconciled, so nothing checked the figures the
analyst quoted *inside* that prose. Spot-checking the P/E it stated against the
stored `fundamentals` row for cycle-v3-1785245400:

    PYPL 10.9 vs 10.9   ok        CARS  4.83 vs 27.99   WRONG (-83%)
    WMT  40.2 vs 40.2   ok        SMCI  8.46 vs 14.45   WRONG (-41%)
    GM  44.42 vs 44.42  ok        EXLS 11.78 vs 18.95   WRONG (-38%)

Four of seven wrong. This is the invented-RSI failure (171 of 305 quant reports)
in the one desk that never got a guard.

## The second reason: fundamentals arrived as adjectives

Technicals reach the deciding agents as precomputed, reconciled NUMBERS.
Fundamentals reached them as prose. Measured over 41 Board BUYs, when the
synthesizer overrode a BUY to HOLD it cited oscillators MORE (stochastic
+27.1pp, bollinger +13.8pp) and fundamentals LESS (eps -21.2pp, margin -16.9pp,
debt -16.7pp). Asked to weigh "RSI 78.0, Stochastic 98.9" against "fundamentals
remain robust", it was comparing three decimals against an adjective. The
oscillators won because they were the only things with numbers attached.

## Scope

This module reads the `fundamentals` snapshot and nothing else. It computes no
multiples of its own — `valuation_block` owns EV, EV/EBIT and the reverse DCF,
and duplicating that here would create two sources of truth for the same
quantity, which is worse than none. What this adds is the *quality and
efficiency* half of the picture (margins, returns, leverage, growth,
positioning) that no module was serving.

Coverage is honest rather than flattering: `roic` exists for 6.7% of tickers and
`eps_surprise` for 6.6%, so those usually render as NOT ON FILE. A named gap is
information; a silently omitted line is not.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from app.quant.technical_baseline import _finite, mark_conclusion_stale

logger = logging.getLogger(__name__)

# Same 45-day threshold as valuation_block, for the same reason: fundamentals
# move on a filing cadence, not a daily one. Borrowing the technical baseline's
# 3 days would label every healthy row stale.
_STALE_FUNDAMENTALS_DAYS = 45

# The fields the reconcile pass enforces on the artifact. Every one is a value
# read directly from a vendor snapshot — not a quantity we derive — so a
# disagreement means the model changed a number it was handed.
VERIFIED_NUMERIC_FIELDS = (
    "pe_ratio",
    "forward_pe",
    "peg_ratio",
    "price_to_book",
    "price_to_sales",
    "profit_margin",
    "oper_margin",
    "gross_margin",
    "roe",
    "roa",
    "roic",
    "debt_to_equity",
    "current_ratio",
    "quick_ratio",
    "revenue_growth",
    "eps_growth_qoq",
    "sales_growth_qoq",
    "short_float_pct",
    "inst_own_pct",
    "recom_score",
    "target_price",
    "dividend_yield",
    "beta",
)

_COLS = ("snapshot_date", "source") + VERIFIED_NUMERIC_FIELDS + (
    "eps_surprise", "sales_surprise", "earnings_date", "market_cap",
)

# Relative tolerance, matching valuation_block. These span 1e-3 (margins as
# fractions) to 1e12 (market cap), so an absolute threshold is simultaneously
# far too tight at one end and meaningless at the other.
_RECONCILE_TOLERANCE = 0.02

_NO_DATA = (
    "## FUNDAMENTAL SNAPSHOT: NO DATA ON FILE\n"
    "  No fundamentals row exists for this ticker. Do NOT supply ratios from "
    "memory — say so in data_gaps and rely on the desks that do have data."
)

# Fields stored as fractions that read naturally as percentages. Rendering 0.0416
# as \"4.16%\" is not a unit conversion of the stored value — the artifact keeps
# the raw number — it is only how the line prints.
_AS_PCT = {
    "profit_margin", "oper_margin", "gross_margin", "roe", "roa", "roic",
    "revenue_growth", "eps_growth_qoq", "sales_growth_qoq", "dividend_yield",
}


#: Ratios whose DENOMINATOR is shareholders' equity. When equity is negative
#: they are not missing — they are undefined, and the two must not print the
#: same word. Vendors label them "NM / not meaningful" and the academic factor
#: literature sets them to missing outright (Jensen-Kelly-Pedersen: "ratios set
#: to missing when book equity is non-positive"; Hou-Xue-Zhang the same for a
#: non-positive denominator).
#:
#: Why it matters here: DELL's book equity is negative (P/B around -227), so
#: yfinance and finnhub both return null for ROE and debt-to-equity, forever.
#: The block printed "NOT ON FILE", the prompt says to report NOT ON FILE
#: fields as data_gaps, and on 2026-09-03 the bear argued — correctly, given
#: what it was shown — that "ROE and debt-to-equity are NOT ON FILE for a
#: second consecutive cycle" was a load-bearing gap. It was a fact about the
#: balance sheet being unusual, not about our collection failing.
_UNDEFINED_ON_NEGATIVE_EQUITY = ("roe", "debt_to_equity")
_NA_NEG_EQUITY = "N/A (negative shareholders' equity)"


def _negative_equity(b: dict) -> bool:
    """True only on a REAL negative price-to-book. An absent P/B (an ETF, a
    ticker with no fundamentals at all) is not negative equity — 359 tickers
    carry a negative P/B in some row and many of the extremes are ETFs with no
    ratios whatsoever, so keying on absence would mislabel them."""
    pb = _finite(b.get("price_to_book"))
    return pb is not None and pb < 0


def _pct(v: float | None) -> str:
    """Human-readable percentage AND the raw stored value, always both.

    Measured on the first live cycle (2026-07-28, SMCI): this printed "ROE
    17.88%" while `fundamentals.roe` stores 0.17877, and the model copied
    17.88 into `metrics.roe` exactly as instructed. The reconcile then
    "corrected" 8 of 8 fields at a ratio of precisely 100.0.

    Decisions were never wrong — the reconcile overwrote every one — but the
    fabrication RATE was destroyed, which is the whole point of preserving
    originals. Eight guaranteed false positives per ticker would have buried
    any real invention, and a guard whose signal is all noise is worse than
    no guard because it looks like coverage.

    A rendering that forces the reader to infer the unit is the defect. Naming
    both removes the inference.
    """
    if v is None:
        return "NOT ON FILE"
    # Vendors disagree on whether a margin is 0.0416 or 4.16. Values inside
    # [-1.5, 1.5] are treated as fractions; anything larger is already a
    # percentage. The ambiguous band is narrow and the alternative — picking one
    # convention and being wrong for half the sources — is worse.
    shown = v * 100 if -1.5 <= v <= 1.5 else v
    return f"{shown:.2f}% [copy as {v:g}]"


def _num(v: float | None, digits: int = 2) -> str:
    return "NOT ON FILE" if v is None else f"{v:.{digits}f}"


def _as_date(value):
    """Mongo hands back a datetime; every consumer here does date arithmetic.

    THE DEAD GATE. `find_docs` does not decode dates on read, so
    `raw["snapshot_date"]` was a naive `datetime` and `date.today() - datetime`
    raises TypeError. The bare `except` below swallowed it, so `stale` was
    ALWAYS False and `age_days` never set — which silently disabled the STALE
    header, the stale branch of `reconcile_fundamental_metrics`, and
    `decision_score`'s `-8 fundamentals N days old` confidence term. Measured
    2026-09-03: no fundamentals row in the store had ever been called stale.
    """
    if isinstance(value, datetime):
        return value.date()
    return value


def latest_fundamentals(
    ticker: str, max_age_days: int = _STALE_FUNDAMENTALS_DAYS
) -> dict | None:
    """The fundamental snapshot, coalesced field-by-field over recent rows.

    WHY THIS IS NOT A SINGLE ROW READ. `fundamentals` is keyed
    (ticker, snapshot_date) and each vendor writes a DIFFERENT subset:
    `roic` comes only from finviz, which supplements 60 tickers a night, while
    yfinance/finnhub/FMP — the collectors a cycle actually runs — never write
    the key at all. So on any day the newest row is not finviz's, `roic` reads
    as absent. Measured 2026-09-03 over 1,152 tickers with a row inside the
    window: the newest row lacked `roic` for **991**, and for **946** of those
    an older row inside the same window HAD it. The value was bought, stored,
    and then hidden by the read.

    Worse, a supplement can create a row of its own: `finnhub_collector.
    _merge_into_fundamentals` used to upsert a 4-field `{ticker, snapshot_date,
    source, earnings_date}` document for TODAY, which then became "the newest
    row" and blanked all 23 verified fields for the rest of the cycle. That is
    how DELL reached a live debate on 2026-09-03 with every ratio NOT ON FILE
    while the previous day's 41-field row sat one document away.

    WHAT THIS IS NOT. It is not imputation. Every value returned is a real
    vendor observation that was really stored, and one that is older than the
    anchor row carries its own as-of date and source in `field_as_of` so the
    prompt can say when it was true. Nothing is averaged, filled, or inferred:
    a field absent from every row inside the window stays absent. The window is
    the module's existing staleness threshold, and it is far tighter than the
    academic convention of carrying accounting data forward 12 months.

    Returns None when no row exists at all.
    """
    from app.db import mongo_store

    rows = []
    try:
        cutoff = date.today() - timedelta(days=max_age_days)
        rows = mongo_store.find_docs(
            "fundamentals",
            {"ticker": ticker.upper(), "snapshot_date": {"$gte": cutoff}},
            sort=[("snapshot_date", -1)],
        ) or []
    except Exception as e:
        logger.warning("[FundamentalBlock] %s: windowed query failed: %s: %s",
                       ticker, type(e).__name__, e)

    if not rows:
        # Older than the window (or the query failed): read the single newest
        # row so a stale snapshot is still reported AS stale rather than as
        # "no data". Nothing to coalesce across in that case.
        rows = mongo_store.find_docs(
            "fundamentals", {"ticker": ticker.upper()},
            sort=[("snapshot_date", -1)], limit=1,
        ) or []
    if not rows:
        return None

    for r in rows:
        r["snapshot_date"] = _as_date(r.get("snapshot_date"))
        r["earnings_date"] = _as_date(r.get("earnings_date"))

    # The ANCHOR dates the snapshot. A supplement-only stub carries no verified
    # field, so letting it date the snapshot would report a 4-field row written
    # minutes ago as "today's fundamentals".
    anchor = next(
        (r for r in rows
         if any(_finite(r.get(f)) is not None for f in VERIFIED_NUMERIC_FIELDS)),
        rows[0],
    )

    b: dict = {
        "ticker": ticker,
        "as_of": anchor.get("snapshot_date"),
        "source": anchor.get("source"),
    }

    field_as_of: dict[str, dict] = {}
    carried_rows: set = set()
    for f in VERIFIED_NUMERIC_FIELDS + ("eps_surprise", "sales_surprise",
                                        "market_cap", "earnings_date"):
        for row in rows:  # already newest-first
            v = row.get(f) if f == "earnings_date" else _finite(row.get(f))
            if v is None:
                continue
            b[f] = v
            if row is not anchor and row.get("snapshot_date") != anchor.get("snapshot_date"):
                field_as_of[f] = {
                    "as_of": row.get("snapshot_date"),
                    "source": row.get("source"),
                }
                carried_rows.add((row.get("snapshot_date"), row.get("source")))
            break

    # Non-numeric on purpose: artifact_validators flattens every int/float key
    # of this baseline into the grounding truth set, and an as-of date is not a
    # quantity the analyst can be accused of misquoting.
    if field_as_of:
        b["field_as_of"] = field_as_of
        b["coalesced_from"] = [
            f"{d} {src or 'unknown'}" for d, src in sorted(carried_rows, key=lambda x: str(x[0]), reverse=True)
        ]

    try:
        age = (date.today() - anchor["snapshot_date"]).days
        b["age_days"] = age
        b["stale"] = age > max_age_days
    except Exception:
        b["stale"] = False

    return b


def compute_fundamental_baseline(ticker: str) -> dict | None:
    """The stored fundamental snapshot, cleaned. None when no row exists."""
    if not ticker:
        return None
    try:
        return latest_fundamentals(ticker)
    except Exception as e:
        logger.warning("[FundamentalBlock] %s: query failed: %s: %s",
                       ticker, type(e).__name__, e)
        return None


def build_fundamental_block(ticker: str) -> str:
    """The injectable briefing section.

    Never returns "" — a silent empty block is how a ticker reaches a desk with
    nothing and no complaint in the logs.
    """
    b = compute_fundamental_baseline(ticker)
    if not b:
        return _NO_DATA

    stale = bool(b.get("stale"))
    header = (
        "## STORED FUNDAMENTAL SNAPSHOT (computed in code from the stored "
        "vendor row — the snapshot is STALE, see below; treat these as the best "
        "available anchor and say so in data_gaps rather than inventing "
        "different numbers)"
        if stale else
        "## PRECOMPUTED FUNDAMENTAL SNAPSHOT (read in code this cycle from "
        "stored vendor data — these are the authoritative values. Cite them "
        "directly; do NOT restate them from memory.)"
    )
    lines = [header]

    src = f"fundamentals {b['as_of']}"
    if b.get("age_days") is not None:
        src += f" ({b['age_days']}d)"
    if b.get("source"):
        src += f", source {b['source']}"
    lines.append(f"  inputs: {src}")

    # Say when a value is older than the snapshot it sits in. The number is a
    # real observation either way, but a reader weighing it deserves its date.
    carried = b.get("field_as_of") or {}
    if carried:
        parts = ", ".join(
            f"{f} {b.get(f):g} ({meta.get('as_of')}, {meta.get('source') or 'unknown'})"
            if isinstance(b.get(f), (int, float))
            else f"{f} ({meta.get('as_of')}, {meta.get('source') or 'unknown'})"
            for f, meta in sorted(carried.items())
        )
        lines.append(
            "  CARRIED FORWARD from an older snapshot (the newest row did not "
            f"carry these): {parts}"
        )

    lines.append(
        f"- Valuation ratios: P/E {_num(b.get('pe_ratio'))}, "
        f"forward P/E {_num(b.get('forward_pe'))}, "
        f"PEG {_num(b.get('peg_ratio'))}, "
        f"P/B {_num(b.get('price_to_book'))}, "
        f"P/S {_num(b.get('price_to_sales'))}"
    )
    lines.append(
        f"- Margins: gross {_pct(b.get('gross_margin'))}, "
        f"operating {_pct(b.get('oper_margin'))}, "
        f"net {_pct(b.get('profit_margin'))}"
    )
    neg_eq = _negative_equity(b)

    def _equity_ratio(field: str, render) -> str:
        """A vendor-supplied value always wins — only the ABSENCE of one on a
        negative-equity balance sheet is relabelled."""
        if neg_eq and b.get(field) is None:
            return _NA_NEG_EQUITY
        return render(b.get(field))

    lines.append(
        f"- Returns: ROE {_equity_ratio('roe', _pct)}, "
        f"ROA {_pct(b.get('roa'))}, ROIC {_pct(b.get('roic'))}"
    )
    lines.append(
        f"- Leverage / liquidity: debt-to-equity "
        f"{_equity_ratio('debt_to_equity', _num)}, current ratio "
        f"{_num(b.get('current_ratio'))}, quick ratio "
        f"{_num(b.get('quick_ratio'))}"
    )
    lines.append(
        f"- Growth: revenue {_pct(b.get('revenue_growth'))}, "
        f"EPS QoQ {_pct(b.get('eps_growth_qoq'))}, "
        f"sales QoQ {_pct(b.get('sales_growth_qoq'))}"
    )
    lines.append(
        f"- Positioning: short float {_pct(b.get('short_float_pct'))}, "
        f"institutional ownership {_pct(b.get('inst_own_pct'))}, "
        f"analyst recommendation {_num(b.get('recom_score'))} "
        f"(1=strong buy, 5=strong sell), target price "
        f"{_num(b.get('target_price'))}"
    )

    # Earnings proximity is stated rather than left implied. "Binary earnings
    # risk" is a recurring stated override reason on the desk while
    # `earnings_date` was cited in 1.5% of decisions — it was being asserted far
    # more often than it was known.
    if b.get("earnings_date"):
        try:
            days = (b["earnings_date"] - date.today()).days
            when = (f"in {days} day(s)" if days >= 0
                    else f"{abs(days)} day(s) ago")
            lines.append(f"- Next earnings: {b['earnings_date']} ({when})")
        except Exception:
            lines.append(f"- Next earnings: {b['earnings_date']}")
    else:
        lines.append("- Next earnings: NOT ON FILE — do not assert earnings "
                     "timing you cannot see")

    if b.get("eps_surprise") is not None or b.get("sales_surprise") is not None:
        lines.append(
            f"- Last surprise: EPS {_pct(b.get('eps_surprise'))}, "
            f"sales {_pct(b.get('sales_surprise'))}"
        )

    undefined = [
        f for f in _UNDEFINED_ON_NEGATIVE_EQUITY if neg_eq and b.get(f) is None
    ]
    missing = [
        f for f in VERIFIED_NUMERIC_FIELDS
        if b.get(f) is None and f not in undefined
    ]
    if missing:
        lines.append(
            "  NOT ON FILE (report these as data_gaps, do not substitute "
            "remembered values): " + ", ".join(missing)
        )
    if undefined:
        # Named, not omitted — TestTheContract requires every verified field to
        # be rendered or named — but named as UNDEFINED, which is a fact about
        # the company, rather than as MISSING, which would be a fact about us.
        lines.append(
            f"  N/A BY CONSTRUCTION (negative shareholders' equity, "
            f"P/B {_num(b.get('price_to_book'))}): " + ", ".join(undefined) +
            " — these ratios have no meaning against a negative denominator. "
            "This is NOT a data gap: do not list them in data_gaps and do not "
            "treat their absence as missing research."
        )

    if stale:
        lines.append(
            f"  STALE: this snapshot is {b.get('age_days')} days old "
            f"(as of {b['as_of']})."
        )

    return "\n".join(lines)


def reconcile_fundamental_metrics(
    artifact: dict, ticker: str, *, model_used_tools: bool = False
) -> dict:
    """Replace the artifact's `metrics` with values read from stored data.

    Same contract as `reconcile_valuation_metrics`: the model's originals are
    preserved under `_model_reported_fundamentals` so the fabrication rate stays
    MEASURABLE rather than merely suppressed.

    Interpretive fields are NEVER touched — summary, pillars, risks, catalysts,
    thesis_direction, near_term_read, confidence. Judgment is the analyst's
    actual job and this module has no opinion about it.
    """
    if not isinstance(artifact, dict):
        return {}
    metrics = artifact.get("metrics")
    if not isinstance(metrics, dict):
        return {}

    baseline = compute_fundamental_baseline(ticker)
    if not baseline:
        return {}

    stale = bool(baseline.get("stale"))
    # A live tool call can legitimately beat a stale stored row, so we record
    # the disagreement without overwriting. Same rule as the valuation pass.
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
        artifact["_model_reported_fundamentals"] = original
        # thesis_direction and near_term_read are both reasoned from the
        # metrics; positioning_read carries its own stance flag already.
        mark_conclusion_stale(
            artifact,
            ["thesis_direction", "near_term_read"],
            corrected,
            "fundamental metrics",
        )
    elif corrected:
        artifact["_unreconciled_fundamentals"] = corrected

    if stale:
        # [MATERIAL] — see the severity note in technical_baseline.py. A
        # fundamentals snapshot ages slowly; 45+ days old is worth flagging but
        # is not the same class of problem as having no row at all.
        artifact.setdefault("data_gaps", []).append(
            f"[MATERIAL] Estimate: stored fundamentals snapshot is "
            f"{baseline.get('age_days')} days old (as of {baseline['as_of']})"
        )

    return {
        "corrected": corrected,
        "applied": apply_corrections,
        "stale": stale,
        "as_of": str(baseline.get("as_of", "")),
    }
