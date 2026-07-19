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


def build_memory_addenda(ticker: str) -> str:
    """Working-memory + retrieved-context + brain-graph blocks, joined.
    '' when all empty."""
    blocks = [
        b for b in (
            build_working_memory_block(ticker),
            build_retrieved_context(ticker),
            build_brain_graph_block(ticker),
        )
        if b
    ]
    return "\n\n".join(blocks)
