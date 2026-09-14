"""Read-only, versioned evidence for a cycle. Unknown is not zero."""
import json
from collections import Counter, defaultdict
from app.db import mongo_store


def build_evidence(summary, telemetry, results, firings):
    rows = [r for r in telemetry if r.get("model_used") or r.get("token_usage") or r.get("elapsed_ms")]
    measured = [r for r in rows if r.get("usage_requests", 0) > 0 and r.get("completion_tokens") is not None]
    complete = sum(r.get("usage_coverage") == "complete" for r in rows)
    repaired = sum((r.get("detail") or {}).get("repaired") is True for r in firings)
    repairable = [r for r in firings if "repaired" in (r.get("detail") or {})]
    models = defaultdict(lambda: {"runs": 0, "elapsed_ms": 0, "tokens": 0, "outcomes": Counter()})
    for row in rows:
        attempts = row.get("usage_attempts") or [{"provider": row.get("provider"),
            "model_used": row.get("model_used"), "tokens_used": row.get("token_usage")}]
        identities = set()
        for attempt in attempts:
            # Missing served identity stays unknown; requested identity is not proof.
            key = (attempt.get("provider"), attempt.get("model_used"))
            entry = models[key]
            entry["tokens"] += attempt.get("tokens_used") or 0
            entry["attempts"] = entry.get("attempts", 0) + 1
            identities.add(key)
        for key in identities:
            entry = models[key]
            entry["runs"] += 1
            entry["outcomes"][row.get("outcome", "unknown")] += 1
            # Run latency cannot be allocated across models without attempt timing.
            if len(identities) == 1:
                entry["elapsed_ms"] += row.get("elapsed_ms") or 0
            else:
                entry["latency_coverage"] = "partial"
    decisions = []
    for row in results:
        value = row.get("result_json") or {}
        if isinstance(value, str):
            try: value = json.loads(value)
            except ValueError: value = {}
        if not isinstance(value, dict): value = {}
        decisions.append({"ticker": row.get("ticker"), "action": value.get("action"),
            "confidence": value.get("confidence"), "policy_action": value.get("policy_action"),
            "hold_reason": value.get("hold_reason"), "trade_attempted": value.get("trade_attempted"),
            "trade_executed": value.get("trade_executed"),
            "financial_quality_metrics": value.get("financial_quality_metrics"),
            "financial_execution_validation": value.get("financial_execution_validation"),
            "financial_evidence_present": bool(value.get("financial_evidence_record"))})
    return {"version": 2, "cycle_id": summary.get("cycle_id"), "status": summary.get("status"),
        "build_sha": summary.get("build_sha"), "model_discovery": summary.get("model_discovery"),
        "decision_count": len(results), "summary_count_matches": summary.get("analysis_results_count") == len(results),
        "usage": {"model_runs": len(rows), "measured_runs": len(measured), "complete_runs": complete,
            "completion_tokens_measured": sum(r["completion_tokens"] for r in measured) if measured else None,
            "coverage": "complete" if rows and complete == len(rows) else "partial" if measured else "unknown",
            "total_tokens_recorded": sum(r.get("token_usage") or 0 for r in rows) if rows else None},
        "artifact_recovery": {"repairable_events": len(repairable), "recovered_events": repaired,
                              "unrecovered_events": len(repairable) - repaired},
        "models": [{"provider": p, "model": m, **v, "outcomes": dict(v["outcomes"])} for (p,m),v in models.items()],
        "decisions": decisions,
        "limits": ["Recorded token totals may have incomplete historical coverage.",
                   "Recovered output and lifecycle completion do not establish decision quality.",
                   "Latency summed over concurrent agents is not cycle wall time."]}


def read_cycle_evidence(cycle_id):
    query = {"cycle_id": cycle_id}
    summaries = mongo_store.find_docs("cycle_run_summaries", query, limit=1)
    if not summaries:
        return None
    telemetry = mongo_store.find_docs("v3_agent_telemetry", query, projection={"_id": 0}, limit=10001)
    results = mongo_store.find_docs("analysis_results", query, projection={"_id": 0}, limit=1001)
    firings = mongo_store.find_docs("v3_guardrail_firings", query, projection={"_id": 0}, limit=10001)
    result = build_evidence(summaries[0], telemetry[:10000], results[:1000], firings[:10000])
    result["truncated"] = len(telemetry)>10000 or len(results)>1000 or len(firings)>10000
    return result
