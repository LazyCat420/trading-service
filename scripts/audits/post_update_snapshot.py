"""Read-only audit snapshot. Run inside the configured Trading service environment.

Outputs counts and telemetry metadata only; never exports prompts, credentials or
market source documents. Production/observation attempts stay separate. These
observational cohorts cannot isolate code changes from ticker/model/load changes.
"""
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from app.db import mongo_store as m
from app.services.cycle_scope import is_synthetic_cycle
from app.services.learning.policy import enabled, skill_allowed
from app.services.parameter_store import get_param

now = datetime.now(timezone.utc)
cutoff = now - timedelta(days=3)
fields = ["cycle_id", "ticker", "agent_name", "outcome", "elapsed_ms", "loops_used",
          "token_usage", "prompt_tokens", "cached_tokens", "cost_partial", "failure_reason",
          "attempt_no", "model_used", "provider", "created_at", "artifact_size_bytes"]
rows = m.find_docs("v3_agent_telemetry", {"created_at": {"$gte": cutoff}},
                   sort=[("created_at", 1)], projection={"_id": 0, **{k: 1 for k in fields}})
groups = defaultdict(list)
for row in rows:
    cohort = "synthetic" if is_synthetic_cycle(row.get("cycle_id")) else "production"
    groups[(cohort, row.get("agent_name", "unknown"))].append(row)
summary = []
for (cohort, role), records in sorted(groups.items()):
    summary.append({"cohort": cohort, "role": role, "attempts": len(records),
      "outcomes": dict(Counter(r.get("outcome", "unknown") for r in records)),
      "partial_cost_attempts": sum(bool(r.get("cost_partial")) for r in records),
      "zero_reported_tokens": sum(not (r.get("token_usage") or r.get("prompt_tokens")) for r in records),
      "reported_total_tokens": sum(r.get("token_usage") or 0 for r in records),
      "reported_input_tokens": sum(r.get("prompt_tokens") or 0 for r in records),
      "elapsed_ms": sum(r.get("elapsed_ms") or 0 for r in records),
      "models": dict(Counter(r.get("model_used") or "unknown" for r in records))})
controls = {k: enabled(k, default=k != "skill_proposals")
            for k in ["skills", "skill_proposals", "indexing", "consolidation"]}
skills = m.find_docs("agent_skills", {"status": "active"})
state = m.find_docs("pipeline_state", {"singleton_id": "current"}, limit=1)
state_fields = ["cycle_id", "status", "trade_flag", "started_at", "phase", "updated_at"]
out = {"captured_at": now.isoformat(), "cutoff": cutoff.isoformat(),
       "learning_controls": controls, "canonical_serving": get_param("MEMORY_CONTEXT_ENABLED"),
       "active_skills": [{"role": r.get("agent_name"), "version": r.get("version"),
         "reviewed_content": skill_allowed(r.get("agent_name", ""), r.get("skill_text", ""))} for r in skills],
       "current_cycle": {k: state[0].get(k) for k in state_fields} if state else None,
       "roles": summary, "attempts": rows,
       "outcome_provenance": m.aggregate("decision_outcomes", [{"$group": {
         "_id": {"has_exit_date": {"$ne": [{"$ifNull": ["$exit_date", None]}, None]},
                 "horizon_days": "$horizon_days", "resolved": {"$ne": [{"$ifNull": ["$resolved_at", None]}, None]}},
         "n": {"$sum": 1}}}])}
print(json.dumps(out, default=str, indent=2))
