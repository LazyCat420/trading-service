"""
Evolution Repository — DB operations for the ASI-Evolve strategy evolution system.

Pure MongoDB implementation for evolution_nodes collection.
"""

import json
import logging
import random
from typing import Optional

from app.db import mongo_store
from app.schemas.evolution import (
    EvolutionNode,
    EvolutionMetrics,
    EvolutionSessionSummary,
)

logger = logging.getLogger(__name__)


def append_node(node: EvolutionNode) -> None:
    """Insert a new evolution node into MongoDB."""
    metrics_json = node.metrics.model_dump_json() if node.metrics else None
    mongo_store.insert_docs(
        "evolution_nodes",
        [{
            "id": node.id,
            "session_id": node.session_id,
            "round": node.round,
            "parent_id": node.parent_id,
            "motivation": node.motivation,
            "code": node.code,
            "metrics": metrics_json,
            "score": node.score,
            "status": node.status,
            "analysis": node.analysis,
            "timestamp": node.timestamp,
        }]
    )


def _doc_to_node(doc: dict) -> EvolutionNode:
    """Convert a MongoDB doc to an EvolutionNode."""
    metrics = None
    raw_metrics = doc.get("metrics")
    if raw_metrics:
        try:
            if isinstance(raw_metrics, str):
                metrics = EvolutionMetrics(**json.loads(raw_metrics))
            elif isinstance(raw_metrics, dict):
                metrics = EvolutionMetrics(**raw_metrics)
        except Exception:
            pass
    return EvolutionNode(
        id=doc.get("id", ""),
        session_id=doc.get("session_id", ""),
        round=doc.get("round", 0),
        parent_id=doc.get("parent_id"),
        motivation=doc.get("motivation") or "",
        code=doc.get("code") or "",
        metrics=metrics,
        score=doc.get("score"),
        status=doc.get("status") or "DISCARD",
        analysis=doc.get("analysis") or "",
        timestamp=doc.get("timestamp") or "",
    )


def sample_nodes(
    k: int = 5, strategy: str = "score_weighted", session_id: Optional[str] = None
) -> list[EvolutionNode]:
    """Sample k nodes from MongoDB evolution_nodes."""
    query = {"session_id": session_id} if session_id else {}
    docs = mongo_store.find_docs(
        "evolution_nodes",
        query,
        sort=[("timestamp", -1)],
        limit=200
    )

    if not docs:
        return []

    nodes = [_doc_to_node(d) for d in docs]

    if strategy == "epsilon_greedy":
        epsilon = 0.2
        if random.random() < epsilon or not any(n.score for n in nodes):
            return random.sample(nodes, min(k, len(nodes)))
        scored = sorted(
            [n for n in nodes if n.score is not None],
            key=lambda n: n.score,
            reverse=True,
        )
        return scored[:k]

    # score_weighted
    scored = [n for n in nodes if n.score is not None and n.score > 0]
    if not scored:
        return random.sample(nodes, min(k, len(nodes)))

    weights = [max(n.score, 0.01) for n in scored]
    total = sum(weights)
    weights = [w / total for w in weights]
    count = min(k, len(scored))
    selected = []
    pool = list(zip(scored, weights))
    for _ in range(count):
        if not pool:
            break
        items, ws = zip(*pool)
        ws_list = list(ws)
        total_w = sum(ws_list)
        ws_list = [w / total_w for w in ws_list]
        idx = random.choices(range(len(items)), weights=ws_list, k=1)[0]
        selected.append(items[idx])
        pool.pop(idx)
    return selected


def get_best_node(session_id: str) -> Optional[EvolutionNode]:
    """Return the highest-scoring KEEP node for a session."""
    docs = mongo_store.find_docs(
        "evolution_nodes",
        {
            "session_id": session_id,
            "status": {"$in": ["KEEP", "SUCCESS", "keep", "success"]},
        },
        sort=[("score", -1)],
        limit=1
    )
    if not docs:
        return None
    return _doc_to_node(docs[0])


def get_session_summary(session_id: str) -> dict:
    """Return aggregate stats for an evolution session."""
    docs = mongo_store.find_docs("evolution_nodes", {"session_id": session_id})
    summary = EvolutionSessionSummary(session_id=session_id)
    total = len(docs)
    best_score = None

    for d in docs:
        status_upper = (d.get("status") or "").upper()
        if status_upper in ("KEEP", "SUCCESS"):
            summary.kept_count += 1
        elif status_upper in ("DISCARD", "REJECTED"):
            summary.discarded_count += 1
        elif status_upper in ("SYNTAX_ERROR", "RUNTIME_ERROR", "ERROR"):
            summary.error_count += 1
        elif status_upper in ("TIMEOUT",):
            summary.timeout_count += 1

        sc = d.get("score")
        if sc is not None:
            if best_score is None or sc > best_score:
                best_score = sc

    summary.total_rounds = total
    summary.best_score = best_score

    best = get_best_node(session_id)
    if best:
        summary.best_node_id = best.id

    return summary.model_dump()


def get_all_nodes(
    session_id: Optional[str] = None, limit: int = 200
) -> list[EvolutionNode]:
    """Return all evolution nodes, optionally filtered by session."""
    if session_id:
        docs = mongo_store.find_docs(
            "evolution_nodes",
            {"session_id": session_id},
            sort=[("round", -1)],
            limit=limit
        )
    else:
        docs = mongo_store.find_docs(
            "evolution_nodes",
            {},
            sort=[("timestamp", -1)],
            limit=limit
        )
    return [_doc_to_node(d) for d in docs]


def get_sessions() -> list[dict]:
    """Return a list of all evolution sessions with summary stats."""
    docs = mongo_store.find_docs("evolution_nodes", {})
    sessions_map: dict[str, dict] = {}

    for d in docs:
        sid = d.get("session_id")
        if not sid:
            continue
        if sid not in sessions_map:
            sessions_map[sid] = {
                "session_id": sid,
                "rounds": 0,
                "best_score": None,
                "started": d.get("timestamp"),
                "last_updated": d.get("timestamp"),
                "kept": 0,
                "discarded": 0,
                "errors": 0,
            }
        s = sessions_map[sid]
        s["rounds"] += 1
        sc = d.get("score")
        if sc is not None:
            if s["best_score"] is None or sc > s["best_score"]:
                s["best_score"] = sc

        ts = d.get("timestamp")
        if ts:
            if not s["started"] or ts < s["started"]:
                s["started"] = ts
            if not s["last_updated"] or ts > s["last_updated"]:
                s["last_updated"] = ts

        status_upper = (d.get("status") or "").upper()
        if status_upper in ('KEEP', 'SUCCESS'):
            s["kept"] += 1
        elif status_upper in ('DISCARD', 'REJECTED'):
            s["discarded"] += 1
        elif status_upper in ('SYNTAX_ERROR', 'RUNTIME_ERROR', 'TIMEOUT', 'ERROR', 'FAILED'):
            s["errors"] += 1

    return list(sessions_map.values())
