"""
Box Scorecard — Per-endpoint performance summary at cycle end.

Queries llm_audit_logs for the cycle and generates a structured
performance report per hardware box (Jetson, DGX Spark, Goldspark).
The scorecard is:
  1. Printed to terminal (for operator visibility)
  2. Returned as dict (for storage in cycle_run_summaries.summary_json)
  3. Available to autoresearch for self-optimization feedback
"""

import logging
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


def generate_box_scorecard(cycle_id: str) -> dict:
    """Generate per-endpoint performance scorecard for a cycle.

    Returns dict keyed by endpoint_name with stats, plus an
    '_aggregate' key with cycle-level totals.
    """
    scorecard = {}

    try:
        # Per-endpoint breakdown. GROUP BY on COALESCE(endpoint_name,'unknown')
        # plus model is a compound _id; the COALESCE(SUM(..),0) wrappers become
        # `or 0` on read, since $sum over an empty/absent field yields 0/None.
        rows = mongo_store.aggregate(
            "llm_audit_logs",
            [
                {"$match": {"cycle_id": cycle_id}},
                {
                    "$group": {
                        "_id": {
                            "ep": {"$ifNull": ["$endpoint_name", "unknown"]},
                            "model": "$model",
                        },
                        "calls": {"$sum": 1},
                        "total_tokens": {"$sum": {"$ifNull": ["$tokens_used", 0]}},
                        "total_prompt": {"$sum": {"$ifNull": ["$prompt_tokens", 0]}},
                        "total_completion": {
                            "$sum": {"$ifNull": ["$completion_tokens", 0]}
                        },
                        "avg_latency_ms": {"$avg": "$execution_ms"},
                        "min_latency_ms": {"$min": "$execution_ms"},
                        "max_latency_ms": {"$max": "$execution_ms"},
                        "total_ms": {"$sum": {"$ifNull": ["$execution_ms", 0]}},
                        "avg_queue_wait_ms": {"$avg": "$queue_wait_ms"},
                        "avg_tok_per_sec": {"$avg": "$tokens_per_second"},
                    }
                },
                {"$sort": {"total_tokens": -1}},
            ],
        )

        for d in rows:
            ep_name = d["_id"]["ep"]
            model_name = d["_id"].get("model") or "unknown"
            total_tokens = d.get("total_tokens") or 0
            total_ms = d.get("total_ms") or 0

            entry = {
                "model": model_name,
                "calls": d.get("calls") or 0,
                "total_tokens": total_tokens,
                "prompt_tokens": d.get("total_prompt") or 0,
                "completion_tokens": d.get("total_completion") or 0,
                "avg_latency_ms": round(d.get("avg_latency_ms") or 0),
                "min_latency_ms": d.get("min_latency_ms") or 0,
                "max_latency_ms": d.get("max_latency_ms") or 0,
                "total_time_s": round(total_ms / 1000, 1),
                "avg_queue_wait_ms": round(d.get("avg_queue_wait_ms") or 0),
                "avg_tok_per_sec": round(d.get("avg_tok_per_sec") or 0, 1),
                # Derived: aggregate throughput
                "aggregate_tok_per_sec": round(total_tokens / (total_ms / 1000), 1)
                if total_ms > 0
                else 0,
            }
            scorecard[ep_name] = entry

        # Aggregate totals
        agg = mongo_query.agg_row(
            "llm_audit_logs",
            {"cycle_id": cycle_id},
            [
                ("count", None),
                ("sum", "tokens_used"),
                ("sum", "execution_ms"),
                ("avg", "execution_ms"),
                ("avg", "queue_wait_ms"),
            ],
        )

        if agg:
            scorecard["_aggregate"] = {
                "total_calls": agg[0],
                "total_tokens": agg[1] or 0,
                "total_time_s": round(agg[2] / 1000, 1) if agg[2] else 0,
                "avg_latency_ms": round(agg[3]) if agg[3] else 0,
                "avg_queue_wait_ms": round(agg[4]) if agg[4] else 0,
            }

        # Slowest calls (top 5)
        slow = mongo_query.find_rows(
            "llm_audit_logs",
            {"cycle_id": cycle_id},
            ["agent_step", "ticker", "execution_ms", "endpoint_name"],
            sort=[("execution_ms", -1)],
            limit=5,
        )

        scorecard["_slowest"] = [
            {
                "agent_step": s[0],
                "ticker": s[1],
                "execution_ms": s[2],
                "endpoint": s[3] if s[3] is not None else "unknown",
            }
            for s in slow
        ]

    except Exception as e:
        logger.error("[BOX_SCORECARD] Query failed: %s", e)
        return {}

    return scorecard


def print_box_scorecard(scorecard: dict) -> None:
    """Print a human-readable box scorecard to the logger."""
    if not scorecard:
        return

    lines = ["", "╔═══════════════════════════════════════════════════════════════╗"]
    lines.append("║              BOX PERFORMANCE SCORECARD                        ║")
    lines.append("╠═══════════════════════════════════════════════════════════════╣")

    for ep_name, stats in scorecard.items():
        if ep_name.startswith("_"):
            continue

        lines.append(f"║  {ep_name.upper():20s}  ({stats.get('model', '?')[:30]})")
        lines.append(
            f"║    Calls: {stats['calls']:>5,d}  │  Tokens: {stats['total_tokens']:>10,d}  "
            f"│  Time: {stats['total_time_s']:>6.0f}s"
        )
        lines.append(
            f"║    Prompt: {stats['prompt_tokens']:>8,d}  │  Completion: {stats['completion_tokens']:>8,d}  "
            f"│  Tok/s: {stats['aggregate_tok_per_sec']:>6.1f}"
        )
        lines.append(
            f"║    Avg Latency: {stats['avg_latency_ms']:>6,d}ms  │  Queue Wait: {stats['avg_queue_wait_ms']:>5,d}ms  "
            f"│  Min/Max: {stats['min_latency_ms']:,}/{stats['max_latency_ms']:,}ms"
        )
        lines.append("║")

    agg = scorecard.get("_aggregate", {})
    if agg:
        lines.append(
            "╠═══════════════════════════════════════════════════════════════╣"
        )
        lines.append(
            f"║  TOTAL: {agg.get('total_calls', 0):>5,d} calls  │  "
            f"{agg.get('total_tokens', 0):>12,d} tokens  │  "
            f"{agg.get('total_time_s', 0):>6.0f}s"
        )

    slowest = scorecard.get("_slowest", [])
    if slowest:
        lines.append("║  Slowest calls:")
        for s in slowest[:3]:
            lines.append(
                f"║    {s['agent_step']:30s} {s['ticker']:6s} "
                f"{s['execution_ms']:>8,d}ms ({s['endpoint']})"
            )

    lines.append("╚═══════════════════════════════════════════════════════════════╝")

    for line in lines:
        logger.info("[BOX_SCORECARD] %s", line)
