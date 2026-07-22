"""Memory/retrieval context blocks for the LIVE V3 prompt path.

These builders were originally written for the RLM harness's
build_rlm_prompt — a path that turned out to be dead (rlm_analyze lost its
caller in the SDK migration). Rehomed here so the live V3 pipeline
(orchestrator memory_context → agent_runner dynamic sections) actually
injects them.

Kept deliberately small: every block is char-capped so the combined
additions stay well under Prism's 2048-token user-message embed limit
(agent_runner reroutes oversized user content into the system prompt,
defeating the KV-cache split).
"""

import logging

logger = logging.getLogger(__name__)

# Tight per-block cap (~375 tokens each) — see module docstring.
BLOCK_MAX_CHARS = 1500


def _cap(text: str, max_chars: int = BLOCK_MAX_CHARS) -> str:
    """Truncate a block to a char budget, appending an elision marker."""
    if text and len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n… [truncated]"
    return text


def build_working_memory_block(ticker: str) -> str:
    """5-store working memory (reminders / facts / past cycles / patterns).

    Returns '' when the stores are empty — get_context always emits header
    scaffolding, so only inject when a real '### ' section is present.
    Non-fatal.
    """
    try:
        from app.services.memory.working_memory import working_memory

        ctx = working_memory.get_context(ticker)
        if ctx and "### " in ctx:
            return _cap(ctx)
    except Exception as e:
        logger.debug("[retrieval-ctx] working memory failed (non-fatal): %s", e)
    return ""


def build_retrieved_context(ticker: str, top_k: int = 4) -> str:
    """Semantic recall over the embedded corpus (news / analysis / graph
    claims) via the hybrid retriever (dense + BM25 + RRF). '' on empty or
    any failure — always non-fatal."""
    try:
        from app.services.retrieval_hybrid import hybrid_retriever

        chunks = hybrid_retriever.retrieve(
            ticker, f"{ticker} latest analysis news catalysts outlook", top_k=top_k
        )
    except Exception as e:
        logger.debug("[retrieval-ctx] hybrid retrieval failed (non-fatal): %s", e)
        return ""

    if not chunks:
        return ""

    lines = [f"### Retrieved Context [{ticker}] (semantic recall)"]
    for c in chunks:
        snippet = (c.content or "").strip().replace("\n", " ")[:220]
        lines.append(f"- [{c.source_table} · {c.score:.2f}] {snippet}")
    return _cap("\n".join(lines))


def build_brain_graph_block(ticker: str) -> str:
    """Activated brain-graph subgraph for this ticker (spreading activation
    over ontology_nodes/ontology_edges). The graph is written every cycle by
    graph_sync; this is the read half of that loop. '' when the graph has no
    neighborhood for the ticker, the feature flag is off, or on any failure."""
    try:
        from app.config.config_cognition import cognition_settings
        if not cognition_settings.ENABLE_ONTOLOGY_GRAPH:
            return ""
        from app.cognition.ontology.ontology_builder import BrainGraph

        ctx = BrainGraph.get_activated_context(ticker, max_chars=BLOCK_MAX_CHARS)
        if ctx:
            return _cap(ctx)
    except Exception as e:
        logger.debug("[retrieval-ctx] brain graph context failed (non-fatal): %s", e)
    return ""


def fred_curve_credit_lines() -> list[str]:
    """Yield-curve + credit-stress lines from FRED macro_indicators, for the
    Regime Engine's macro briefing. The curve (10Y−2Y, INVERTED flag) and the
    HY option-adjusted spread are REAL recession/credit signals already in the
    DB — strictly better than fetching ^TNX/^IRX or an HYG/LQD price-ratio
    proxy at agent time. [] on empty/any failure — non-fatal."""
    try:
        from app.db.connection import get_db

        with get_db() as db:
            rows = db.execute(
                "SELECT DISTINCT ON (indicator) indicator, value "
                "FROM macro_indicators WHERE source = 'fred' "
                "AND indicator IN ('TREASURY_10Y', 'TREASURY_2Y', 'HY_SPREAD') "
                "ORDER BY indicator, date DESC"
            ).fetchall()
    except Exception as e:
        logger.debug("[retrieval-ctx] fred curve/credit lines failed (non-fatal): %s", e)
        return []

    vals = {r[0]: float(r[1]) for r in rows if r[1] is not None}
    lines: list[str] = []
    t10, t2 = vals.get("TREASURY_10Y"), vals.get("TREASURY_2Y")
    if t10 is not None and t2 is not None:
        spread = t10 - t2
        lines.append(
            f"- Yield curve (FRED): 10Y {t10:.2f}% − 2Y {t2:.2f}% = "
            f"{spread:+.2f}pp{' (INVERTED — classic recession signal)' if spread < 0 else ''}"
        )
    hy = vals.get("HY_SPREAD")
    if hy is not None:
        stress = "elevated stress" if hy >= 5.0 else "watch" if hy >= 4.0 else "calm"
        lines.append(f"- High-yield credit spread (FRED OAS): {hy:.2f}pp ({stress})")
    return lines


def build_macro_block(ticker: str) -> str:
    """Macro backdrop from FRED data (macro_indicators). The desk was
    macro-blind before this: the table was collected for the dashboard but
    never reached an agent prompt. '' on empty/any failure — non-fatal."""
    try:
        from app.db.connection import get_db

        with get_db() as db:
            rows = db.execute(
                "SELECT DISTINCT ON (indicator) indicator, date, value "
                "FROM macro_indicators WHERE source = 'fred' "
                "ORDER BY indicator, date DESC"
            ).fetchall()
            # Exact one-year base per series (FRED monthly dates are always
            # the 1st, so the join is safe). Anchoring to CURRENT_DATE
            # instead landed 11 months back and understated YoY.
            yoy_rows = db.execute(
                "SELECT a.indicator, b.value FROM ("
                "  SELECT DISTINCT ON (indicator) indicator, date"
                "  FROM macro_indicators WHERE source = 'fred'"
                "  AND indicator IN ('CPI', 'PCE_CORE')"
                "  ORDER BY indicator, date DESC"
                ") a JOIN macro_indicators b ON b.indicator = a.indicator "
                "AND b.source = 'fred' "
                "AND b.date = (a.date - INTERVAL '1 year')::date"
            ).fetchall()
    except Exception as e:
        logger.debug("[retrieval-ctx] macro block failed (non-fatal): %s", e)
        return ""

    latest = {r[0]: (r[1], r[2]) for r in rows}
    year_ago = {r[0]: r[1] for r in yoy_rows}
    if not latest:
        return ""

    def val(key):
        return latest[key][1] if key in latest else None

    def yoy(key):
        now, old = val(key), year_ago.get(key)
        if now and old:
            return (now / old - 1.0) * 100.0
        return None

    lines = ["### Macro Backdrop (FRED, latest)"]

    t10, t2, ff = val("TREASURY_10Y"), val("TREASURY_2Y"), val("FED_FUNDS")
    if t10 is not None and t2 is not None:
        spread = t10 - t2
        curve = f"curve 10Y-2Y {spread:+.2f}pp{' (INVERTED)' if spread < 0 else ''}"
        rates = [f"Fed funds {ff:.2f}%"] if ff is not None else []
        rates += [f"10Y {t10:.2f}%", f"2Y {t2:.2f}%", curve]
        lines.append("- Rates: " + " | ".join(rates))

    infl = []
    cpi_yoy, pce_yoy = yoy("CPI"), yoy("PCE_CORE")
    if cpi_yoy is not None:
        infl.append(f"CPI YoY {cpi_yoy:.1f}%")
    if pce_yoy is not None:
        infl.append(f"Core PCE YoY {pce_yoy:.1f}%")
    if val("INFLATION_EXPECT") is not None:
        infl.append(f"5Y breakeven {val('INFLATION_EXPECT'):.2f}%")
    if infl:
        lines.append("- Inflation: " + " | ".join(infl))

    labor = []
    if val("UNEMPLOYMENT") is not None:
        labor.append(f"unemployment {val('UNEMPLOYMENT'):.1f}%")
    if val("INITIAL_CLAIMS") is not None:
        labor.append(f"initial claims {val('INITIAL_CLAIMS') / 1000:.0f}k")
    if labor:
        lines.append("- Labor: " + " | ".join(labor))

    risk = []
    if val("VIX") is not None:
        risk.append(f"VIX {val('VIX'):.1f}")
    if val("HY_SPREAD") is not None:
        risk.append(f"HY spread {val('HY_SPREAD'):.2f}pp")
    if val("DOLLAR_INDEX") is not None:
        risk.append(f"USD index {val('DOLLAR_INDEX'):.1f}")
    if risk:
        lines.append("- Risk: " + " | ".join(risk))

    if len(lines) < 2:
        return ""
    newest = max(d for d, _ in latest.values())
    lines.append(f"(as of {newest})")
    return _cap("\n".join(lines))


def build_memory_addenda(ticker: str) -> str:
    """Working-memory + retrieved-context + brain-graph + macro blocks,
    joined. '' when all empty."""
    blocks = [
        b for b in (
            build_working_memory_block(ticker),
            build_retrieved_context(ticker),
            build_brain_graph_block(ticker),
            build_macro_block(ticker),
        )
        if b
    ]
    return "\n\n".join(blocks)
