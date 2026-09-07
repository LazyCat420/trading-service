"""Bounded retrieval over deterministic research facets, without an LLM rewrite.

The original question is preserved in every facet. Results are deduplicated
by source identity and remain subject to the hybrid retriever's eligibility.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_DECOMPOSE_SYSTEM_PROMPT = """You break a trading research question into focused search sub-queries.
Output ONLY the sub-queries, one per line, no numbering, no commentary.
Each sub-query should target a distinct facet (fundamentals, technicals, news/catalysts, risks, macro).
Emit at most {n} sub-queries. If the question is already narrow, emit just one."""


def _parse_subqueries(text: str, max_subqueries: int) -> list[str]:
    """Extract clean one-per-line sub-queries from the LLM response."""
    if not text:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*0123456789. ").strip()
        if line and line not in out:
            out.append(line)
        if len(out) >= max_subqueries:
            break
    return out


async def decompose_and_retrieve(
    ticker: str,
    question: str,
    max_subqueries: int = 5,
    top_k: int = 8,
):
    """Decompose `question` into sub-queries, hybrid-retrieve each, merge.

    Returns a list of RetrievedChunk (deduped by (source_table, source_id),
    best score wins), sorted by score descending, capped at `top_k`.
    """
    from app.services.retrieval_hybrid import hybrid_retriever

    # The live caller asks the same broad question every time. Generating
    # facet names with an LLM added minutes without adding source evidence.
    # Use deterministic facets; preserve the original question in each query.
    subqueries = [f"{ticker} {question} {facet}" for facet in
                  ("fundamentals guidance cash flow", "technicals price volume", "news catalysts risks")][:max_subqueries]

    # Retrieve each sub-query (hybrid_retriever is blocking → offload).
    per_query = max(4, top_k // 2)
    merged: dict[tuple[str, str], object] = {}
    for subq in subqueries:
        try:
            chunks = await asyncio.to_thread(
                hybrid_retriever.retrieve, ticker, subq, per_query
            )
        except Exception as e:
            logger.debug("[decompose] retrieve failed for %r (non-fatal): %s", subq, e)
            continue
        for c in chunks:
            key = (c.source_table, c.source_id)
            existing = merged.get(key)
            if existing is None or c.score > existing.score:
                merged[key] = c

    results = sorted(merged.values(), key=lambda c: c.score, reverse=True)[:top_k]
    logger.info(
        "[decompose] %s: %d sub-queries → %d unique chunks",
        ticker, len(subqueries), len(results),
    )
    return results


def render_block(ticker: str, chunks) -> str:
    """Render decomposed-retrieval chunks as a prompt block. '' if empty."""
    if not chunks:
        return ""
    lines = [
        "========================================",
        f"## Deep Retrieved Context [{ticker}] (decomposed recall)",
        "========================================",
    ]
    for c in chunks:
        snippet = (c.content or "").strip().replace("\n", " ")[:280]
        lines.append(f"- [{c.source_table} · {c.score:.2f}] {snippet}")
    return "\n".join(lines)


async def build_decomposed_block(ticker: str, question: str) -> str:
    """Convenience: decompose + retrieve + render, fully non-fatal."""
    try:
        chunks = await decompose_and_retrieve(ticker, question)
        return render_block(ticker, chunks)
    except Exception as e:
        logger.debug("[decompose] build_decomposed_block failed for %s: %s", ticker, e)
        return ""
