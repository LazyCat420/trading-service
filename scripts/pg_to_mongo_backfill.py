#!/usr/bin/env python3
"""
Backfill a Postgres table into its MongoDB document collection, then verify.

Part of the Postgres → MongoDB consolidation (.agents/PLAN-mongodb-consolidation.md).
Reads a PG table in batches and upserts documents into the trading_bot Mongo DB
(via app.db.mongo_store), keyed on the table's natural key so re-runs are
idempotent. Read-only on Postgres.

Usage (inside the trading-service container or with its venv + PYTHONPATH):
    python scripts/pg_to_mongo_backfill.py pipeline_events
    python scripts/pg_to_mongo_backfill.py pipeline_events --verify-only
    python scripts/pg_to_mongo_backfill.py --list

Each supported table declares: the SELECT, the natural key field(s), and a
row→document mapper. Add a table by adding one entry to TABLES.
"""
import argparse
import json
import sys

from app.db import connection
from app.db import mongo_store


def _pipeline_events_doc(row, cols):
    d = dict(zip(cols, row))
    data = d.get("data_json")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    return {
        "id": d["id"],
        "cycle_id": d.get("cycle_id"),
        "timestamp": d.get("timestamp"),
        "phase": d.get("phase"),
        "step": d.get("step"),
        "detail": d.get("detail"),
        "status": d.get("status"),
        "data": data or {},
        "elapsed_ms": d.get("elapsed_ms") or 0,
    }


def _passthrough_doc(row, cols):
    """id + scalar columns straight through as a document (no JSON re-parse)."""
    return dict(zip(cols, row))


def _json_doc(*json_cols):
    """Mapper factory: passthrough, but parse the named text/json columns into
    native dicts so they round-trip as Mongo documents (not strings)."""
    def _mapper(row, cols):
        d = dict(zip(cols, row))
        for c in json_cols:
            v = d.get(c)
            if isinstance(v, str):
                try:
                    d[c] = json.loads(v)
                except Exception:
                    d[c] = {}
        return d
    return _mapper


_cycle_audit_doc = _json_doc("data")


# table -> (select_sql, key_field, row_mapper)
TABLES = {
    "pipeline_events": (
        "SELECT id, cycle_id, timestamp, phase, step, detail, status, data_json, elapsed_ms "
        "FROM pipeline_events",
        "id",
        _pipeline_events_doc,
    ),
    "execution_errors": (
        "SELECT id, cycle_id, phase, ticker, error_type, error_message, stack_trace, created_at "
        "FROM execution_errors",
        "id",
        _passthrough_doc,
    ),
    "cycle_audit_log": (
        "SELECT id, cycle_id, timestamp, audit_type, event_type, phase, ticker, severity, message, data "
        "FROM cycle_audit_log",
        "id",
        _cycle_audit_doc,
    ),
    "agent_audit_log": (
        "SELECT id, request_id, endpoint, agent_name, model_used, system_prompt_hash, context_build_ms, "
        "inference_ms, tokens_input, tokens_output, tokens_total, is_truncated, fallback_triggered, "
        "circuit_breaker_open, ticker, cycle_id, status, detail, created_at FROM agent_audit_log",
        "id", _passthrough_doc,
    ),
    "agent_tool_telemetry": (
        "SELECT id, cycle_id, agent_name, tool_name, args_hash, success, elapsed_ms, error_message, "
        "was_blocked, created_at, ticker FROM agent_tool_telemetry",
        "id", _passthrough_doc,
    ),
    "agent_traces": (
        "SELECT id, run_id, agent_name, task_type, goal, planned_next_action, tool_name, tool_args, "
        "tool_result_summary, why_tool_was_called, tokens_before, tokens_after, latency_ms, "
        "did_tool_change_decision, loop_step, stop_reason, created_at, endpoint_name, model_name, "
        "service_source FROM agent_traces",
        "id", _passthrough_doc,
    ),
    "v3_agent_telemetry": (
        "SELECT id, cycle_id, ticker, agent_name, phase, outcome, elapsed_ms, loops_used, token_usage, "
        "artifact_size_bytes, quality_score, created_at FROM v3_agent_telemetry",
        "id", _passthrough_doc,
    ),
    "llm_audit_logs": (
        "SELECT id, cycle_id, bot_id, ticker, agent_step, model, system_prompt_hash, context_hash, "
        "raw_response, tokens_used, execution_ms, created_at, endpoint_name, prompt_tokens, "
        "completion_tokens, queue_wait_ms, tokens_per_second, agent_task_id FROM llm_audit_logs",
        "id", _passthrough_doc,
    ),
    "trade_results": (
        "SELECT id, ticker, cycle_id, action, confidence, reasoning, signal_weights, signal_assessments, "
        "risk_flags, stop_loss, take_profit, position_size_pct, persona_used, regime, created_at "
        "FROM trade_results",
        "id", _json_doc("signal_weights", "signal_assessments", "risk_flags"),
    ),
    "ticker_reports": (
        "SELECT id, cycle_id, ticker, action, confidence, report_markdown, result_summary, is_summary, "
        "created_at FROM ticker_reports",
        "id", _json_doc("result_summary"),
    ),
    "analysis_results": (
        "SELECT id, cycle_id, bot_id, ticker, agent_name, result_json, confidence, created_at, triage_tier, "
        "thesis_verdict, thesis_confidence, thesis_summary, thesis_updated_at, thesis_unchanged, "
        "price_at_analysis, agent_task_id, analysis_price, analysis_rsi, analysis_fund_count "
        "FROM analysis_results",
        "id", _json_doc("result_json"),
    ),
}


def backfill(table: str, batch: int = 2000, verify_only: bool = False) -> int:
    if table not in TABLES:
        print(f"unknown table {table!r}; known: {', '.join(TABLES)}", file=sys.stderr)
        return 2
    select_sql, key_field, mapper = TABLES[table]

    with connection.get_db() as db:
        pg_count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    mongo_before = mongo_store.count_docs(table)
    print(f"[{table}] postgres rows={pg_count}  mongo docs(before)={mongo_before}")

    if not verify_only:
        moved = 0
        # Server-side pagination by key to avoid loading the whole table.
        last_key = ""
        while True:
            with connection.get_db() as db:
                cur = db.execute(
                    f"{select_sql} WHERE {key_field} > %s ORDER BY {key_field} ASC LIMIT %s",
                    [last_key, batch],
                )
                rows = cur.fetchall()
                cols = [c[0] for c in cur.description]
            if not rows:
                break
            docs = [mapper(r, cols) for r in rows]
            # One bulk round-trip per batch (not one upsert per doc).
            mongo_store.bulk_upsert(table, docs, key_field=key_field)
            moved += len(docs)
            last_key = dict(zip(cols, rows[-1]))[key_field]
            print(f"[{table}] upserted {moved}/{pg_count}", end="\r", flush=True)
        print()

    mongo_after = mongo_store.count_docs(table)
    ok = mongo_after >= pg_count
    print(f"[{table}] VERIFY: postgres={pg_count}  mongo={mongo_after}  "
          f"{'OK' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table", nargs="?", help="table to backfill")
    ap.add_argument("--verify-only", action="store_true", help="count-compare only, no writes")
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--list", action="store_true", help="list supported tables")
    args = ap.parse_args()
    if args.list or not args.table:
        print("supported tables:", ", ".join(TABLES))
        return 0
    return backfill(args.table, batch=args.batch, verify_only=args.verify_only)


if __name__ == "__main__":
    raise SystemExit(main())
