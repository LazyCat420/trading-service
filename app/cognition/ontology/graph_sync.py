"""Graph sync — feed completed V3 desks into the Brain Graph.

The pre-V3 pipeline populated ontology_nodes/ontology_edges (via the old
entity extractor) and emitted graph_node_events rows that trading-client's
WebSocket poller broadcasts to the Brain Graph UI. The V3 rewrite removed
that step, so the graph froze. This module restores a minimal, non-fatal
version: after each ticker's pipeline completes, upsert the ticker node,
claim nodes for the key artifacts, edges linking them, and matching
graph_node_events rows for the live view.
"""

import hashlib
import json
import logging

from app.cognition.ontology.ontology_builder import BrainGraph
from app.db.connection import get_db

logger = logging.getLogger(__name__)


def _claim_id(ticker: str, kind: str, text: str) -> str:
    digest = hashlib.sha1(f"{ticker}:{kind}:{text}".encode()).hexdigest()[:12]
    return f"claim_{digest}"


def _emit_event(db, event_type: str, ticker: str, **kwargs) -> None:
    if event_type == "node_added":
        db.execute(
            "INSERT INTO graph_node_events "
            "(event_type, node_id, node_type, label, metadata_json, ticker) "
            "VALUES ('node_added', %s, %s, %s, %s, %s)",
            [kwargs.get("node_id"), kwargs.get("node_type"),
             kwargs.get("label"), kwargs.get("metadata_json"), ticker],
        )
    elif event_type == "edge_added":
        db.execute(
            "INSERT INTO graph_node_events "
            "(event_type, source_id, target_id, relation, weight, ticker) "
            "VALUES ('edge_added', %s, %s, %s, %s, %s)",
            [kwargs.get("source_id"), kwargs.get("target_id"),
             kwargs.get("relation"), kwargs.get("weight", 0.5), ticker],
        )


def sync_desk_to_graph(desk, cycle_id: str) -> None:
    """Upsert ontology nodes/edges + live events from a completed desk.

    Non-fatal by design: any failure is logged and swallowed so graph
    bookkeeping can never break the trading pipeline.
    """
    ticker = desk.ticker
    try:
        claims: list[tuple[str, str, float]] = []  # (kind, text, weight)

        regime = getattr(desk, "regime_classification", None) or {}
        if regime.get("regime"):
            claims.append(("regime", f"Regime: {regime['regime']}", 0.4))

        for kind, artifact in (
            ("fundamental", getattr(desk, "fundamental_report", None)),
            ("quant", getattr(desk, "quant_report", None)),
        ):
            if artifact and artifact.get("summary"):
                direction = artifact.get("thesis_direction", "?")
                conf = artifact.get("confidence", 0)
                text = f"[{ticker}] {kind} thesis {direction} ({conf}%): {artifact['summary'][:180]}"
                claims.append((kind, text, min(1.0, (conf or 50) / 100.0)))

        tournament = getattr(desk, "tournament_result", None) or {}
        if tournament.get("summary"):
            text = (
                f"[{ticker}] tournament {tournament.get('action', 'HOLD')} "
                f"({tournament.get('confidence', 0)}%): {tournament['summary'][:180]}"
            )
            claims.append(("tournament", text, min(1.0, (tournament.get("confidence") or 50) / 100.0)))

        decision = desk.trade_decision or desk.final_decision or {}
        if decision.get("action"):
            text = (
                f"[{ticker}] decision {decision['action']} "
                f"({decision.get('confidence', 0)}%): {str(decision.get('reasoning', ''))[:180]}"
            )
            claims.append(("decision", text, min(1.0, (decision.get("confidence") or 50) / 100.0)))

        if not claims:
            return

        BrainGraph.upsert_node(ticker, "Ticker", label=ticker,
                               metadata={"last_cycle_id": cycle_id})
        with get_db() as db:
            _emit_event(db, "node_added", ticker, node_id=ticker,
                        node_type="Ticker", label=ticker)

            for kind, text, weight in claims:
                node_id = _claim_id(ticker, kind, text)
                metadata = {"ticker": ticker, "cycle_id": cycle_id, "kind": kind, "text": text}
                BrainGraph.upsert_node(node_id, "Claim", label=text[:120], metadata=metadata)
                BrainGraph.upsert_edge(ticker, node_id, "HAS_CLAIM", weight=weight,
                                       metadata={"cycle_id": cycle_id})
                _emit_event(db, "node_added", ticker, node_id=node_id, node_type="Claim",
                            label=text[:120], metadata_json=json.dumps(metadata))
                _emit_event(db, "edge_added", ticker, source_id=ticker, target_id=node_id,
                            relation="HAS_CLAIM", weight=weight)

        # Index the (already natural-language) claim strings into the vector
        # store so the hybrid retriever can recall this cycle's reasoning later.
        # Deterministic id per claim node → idempotent across re-syncs. Non-fatal.
        try:
            from app.services.embedding_ingest import index_text

            for kind, text, _weight in claims:
                index_text("graph_claims", _claim_id(ticker, kind, text), ticker, text)
        except Exception as embed_err:
            logger.debug("[GraphSync] %s: claim embedding failed (non-fatal): %s",
                         ticker, embed_err)

        logger.info("[GraphSync] %s: %d claims synced to brain graph", ticker, len(claims))
    except Exception as e:
        logger.warning("[GraphSync] %s: graph sync failed (non-fatal): %s", ticker, e)
