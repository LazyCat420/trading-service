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
import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

from app.db import connection
from app.db import mongo_store
from app.db import table_spec


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
    # Keyed on request_id, NOT id: the live mirror wrote id-less docs keyed by
    # request_id until 2026-08-16 (the PG serial doesn't exist at mirror time;
    # the writer now uses RETURNING id). Upserting by request_id heals those
    # docs in place — adding the id — instead of duplicating every row.
    "agent_audit_log": (
        "SELECT id, request_id, endpoint, agent_name, model_used, system_prompt_hash, context_build_ms, "
        "inference_ms, tokens_input, tokens_output, tokens_total, is_truncated, fallback_triggered, "
        "circuit_breaker_open, ticker, cycle_id, status, detail, created_at FROM agent_audit_log",
        "request_id", _passthrough_doc,
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
    # v3_agent_telemetry and trade_results deliberately have NO entry here: they
    # are served by app/db/table_spec.py. Their hand-written column lists were
    # strict subsets of the table, which is not a style problem but a data one —
    # see the note above `_spec_for`.
    "llm_audit_logs": (
        "SELECT id, cycle_id, bot_id, ticker, agent_step, model, system_prompt_hash, context_hash, "
        "raw_response, tokens_used, execution_ms, created_at, endpoint_name, prompt_tokens, "
        "completion_tokens, queue_wait_ms, tokens_per_second, agent_task_id FROM llm_audit_logs",
        "id", _passthrough_doc,
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
    "context_blobs": (
        "SELECT context_hash, content, byte_size, created_at FROM context_blobs",
        "context_hash", _passthrough_doc,
    ),
}


def _spec_for(table: str):
    """(select_sql, key_field, mapper) — hand-written if one exists, else derived.

    A hand-written entry is now the exception, kept only where the schema cannot
    express the mapping: `pipeline_events` renames `data_json` -> `data`, which
    nothing in `information_schema` implies.

    Everything else is generated, because a hand-written column list is not a
    style choice — it is a place for the mirror to silently disagree with the
    table. Both hand-written specs removed here were strict SUBSETS of their
    tables: `v3_agent_telemetry` omitted 9 columns (7,279 PG rows carry
    `prompt_tokens`/`cached_tokens`/`sys_prompt_chars` while 36 Mongo documents
    had those fields at all), and `trade_results` omitted 4 that the LIVE writer
    does mirror, so re-running its backfill was a strip rather than a repair.
    A generated spec cannot drift from the table, because it IS the table.
    """
    if table in TABLES:
        return TABLES[table]
    with connection.get_db() as db:
        return table_spec.spec_for(table, db)


def known_tables() -> list[str]:
    """Every table this script can move: hand-written plus ledger `migrate`."""
    from app.db.table_spec import _ledger  # local: keeps import cost off --help
    ledger_tables = [
        row["table"] for row in _ledger().values()
        if row.get("disposition") == "migrate"
    ]
    return sorted(set(TABLES) | set(ledger_tables))


def _normalize(v):
    """Collapse representation differences that are not data differences:
    Mongo stores datetimes as naive-UTC with millisecond precision; PG hands
    back tz-aware microsecond datetimes and Decimals."""
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.replace(microsecond=(v.microsecond // 1000) * 1000)
    if isinstance(v, Decimal):
        return float(v)
    return v


def _values_equal(a, b) -> bool:
    a, b = _normalize(a), _normalize(b)
    if a is None and b is None:
        return True
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
        except (TypeError, ValueError):
            return False
    return a == b


def verify_fields(table: str, sample: int) -> int:
    """Field-level parity on a random sample — the count-only verify passed
    for weeks while (for example) a TTL index was silently deleting rows, so
    counts alone are not evidence of parity. Compares every mapped field of
    `sample` random PG rows against the Mongo doc with the same natural key.

    Note: ORDER BY random() is a full scan — fine for the sub-million-row
    tables migrated so far; switch to TABLESAMPLE for the time-series tier."""
    try:
        select_sql, key_field, mapper = _spec_for(table)
    except (KeyError, ValueError) as exc:
        print(f"cannot build a spec for {table!r}: {exc}", file=sys.stderr)
        return 2
    with connection.get_db() as db:
        cur = db.execute(f"{select_sql} ORDER BY random() LIMIT %s", [sample])
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
    mismatches: Counter = Counter()
    missing = 0
    for r in rows:
        expected = mapper(r, cols)
        docs = mongo_store.find_docs(table, {key_field: expected[key_field]}, limit=1)
        if not docs:
            missing += 1
            continue
        doc = docs[0]
        for k, v in expected.items():
            if not _values_equal(v, doc.get(k)):
                mismatches[k] += 1
    checked = len(rows) - missing
    print(f"[{table}] FIELD-VERIFY: sampled={len(rows)} compared={checked} "
          f"missing-in-mongo={missing}")
    for field, n in mismatches.most_common():
        print(f"[{table}]   field {field!r}: {n} mismatch(es)")
    ok = missing == 0 and not mismatches
    print(f"[{table}] FIELD-VERIFY: {'OK' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def verify_all(table: str, batch: int = 2000, examples: int = 5) -> int:
    """EXHAUSTIVE field-level parity: every row, not a sample.

    `verify_fields` draws N random rows, so its verdict depends on the draw —
    it scored `context_blobs` OK three runs out of four while a full sweep of
    the same unchanged data found 117 drifted `created_at` values and 2
    permanently missing documents. A sampled check cannot certify a table for
    promotion to `mongo`, because that is exactly the moment a missing row
    becomes unreadable and a drifted value becomes the only value there is.

    Implementation note: this walks PG in key batches and fetches the matching
    Mongo docs with `$in`, rather than sorting both sides and merge-joining.
    PG's default collation and Mongo's binary sort do not agree on string keys,
    so a merge-join would report spurious gaps on any text key (every hash- and
    ticker-keyed table here). Batched lookups are index-driven and
    collation-independent.
    """
    try:
        select_sql, key_field, mapper = _spec_for(table)
    except (KeyError, ValueError) as exc:
        print(f"cannot build a spec for {table!r}: {exc}", file=sys.stderr)
        return 2
    coll = mongo_store.get_doc_db()[table]

    mismatches: Counter = Counter()
    samples: dict[str, list] = {}
    missing_keys: list = []
    pg_total = 0

    # Same type-agnostic keyset pagination the backfill uses: the first page has
    # no WHERE clause, so an int id is never compared against a sentinel string.
    last_key = None
    while True:
        with connection.get_db() as db:
            if last_key is None:
                cur = db.execute(f"{select_sql} ORDER BY {key_field} ASC LIMIT %s", [batch])
            else:
                cur = db.execute(
                    f"{select_sql} WHERE {key_field} > %s ORDER BY {key_field} ASC LIMIT %s",
                    [last_key, batch],
                )
            rows = cur.fetchall()
            cols = [c[0] for c in cur.description]
        if not rows:
            break
        expected_by_key = {}
        for r in rows:
            pg_total += 1
            doc = mapper(r, cols)
            expected_by_key[doc[key_field]] = doc
        last_key = rows[-1][cols.index(key_field)]

        found = {
            d.get(key_field): d
            for d in coll.find({key_field: {"$in": list(expected_by_key)}})
        }
        for key, expected in expected_by_key.items():
            doc = found.get(key)
            if doc is None:
                if len(missing_keys) < examples:
                    missing_keys.append(key)
                mismatches["__missing__"] += 1
                continue
            for k, v in expected.items():
                if not _values_equal(v, doc.get(k)):
                    mismatches[k] += 1
                    if len(samples.setdefault(k, [])) < examples:
                        samples[k].append((key, v, doc.get(k)))

    mongo_total = coll.count_documents({})
    missing = mismatches.pop("__missing__", 0)
    drifted = sum(mismatches.values())

    print(f"[{table}] VERIFY-ALL: pg_rows={pg_total:,} mongo_docs={mongo_total:,} "
          f"missing-in-mongo={missing:,} drifted-fields={drifted:,}")
    if mongo_total > pg_total:
        print(f"[{table}]   NOTE: Mongo holds {mongo_total - pg_total:,} document(s) "
              f"with no PG row — orphans, or rows deleted from PG only")
    for field, n in mismatches.most_common():
        print(f"[{table}]   field {field!r}: {n:,} mismatch(es)")
        for key, pg_v, mg_v in samples.get(field, []):
            print(f"[{table}]      {key_field}={key!r}  pg={pg_v!r}  mongo={mg_v!r}")
    if missing_keys:
        print(f"[{table}]   missing keys (first {len(missing_keys)}): "
              + ", ".join(repr(k) for k in missing_keys))

    ok = missing == 0 and drifted == 0 and mongo_total == pg_total
    print(f"[{table}] VERIFY-ALL: {'OK' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def backfill(table: str, batch: int = 2000, verify_only: bool = False,
             rate_limit: float = 0.0) -> int:
    try:
        select_sql, key_field, mapper = _spec_for(table)
    except (KeyError, ValueError) as exc:
        print(f"cannot build a spec for {table!r}: {exc}", file=sys.stderr)
        return 2

    with connection.get_db() as db:
        pg_count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    mongo_before = mongo_store.count_docs(table)
    print(f"[{table}] postgres rows={pg_count}  mongo docs(before)={mongo_before}")

    if not verify_only:
        moved = 0
        # Keyset pagination — type-agnostic: the first page has NO where clause
        # (so we never compare an int id against a sentinel string), and later
        # pages use the actual last-row key value, which carries its own type.
        last_key = None
        while True:
            with connection.get_db() as db:
                if last_key is None:
                    cur = db.execute(f"{select_sql} ORDER BY {key_field} ASC LIMIT %s", [batch])
                else:
                    cur = db.execute(
                        f"{select_sql} WHERE {key_field} > %s ORDER BY {key_field} ASC LIMIT %s",
                        [last_key, batch],
                    )
                rows = cur.fetchall()
                cols = [c[0] for c in cur.description]
            if not rows:
                break
            batch_started = time.monotonic()
            docs = [mapper(r, cols) for r in rows]
            # One bulk round-trip per batch (not one upsert per doc).
            mongo_store.bulk_upsert(table, docs, key_field=key_field)
            moved += len(docs)
            last_key = dict(zip(cols, rows[-1]))[key_field]
            print(f"[{table}] upserted {moved}/{pg_count}", end="\r", flush=True)
            if rate_limit > 0:
                # Hold sustained throughput at --rate-limit rows/sec so a big
                # backfill cannot roll the oplog or starve the live service.
                min_batch_seconds = len(docs) / rate_limit
                sleep_for = min_batch_seconds - (time.monotonic() - batch_started)
                if sleep_for > 0:
                    time.sleep(sleep_for)
        print()

    mongo_after = mongo_store.count_docs(table)
    ok = mongo_after >= pg_count
    print(f"[{table}] VERIFY: postgres={pg_count}  mongo={mongo_after}  "
          f"{'OK' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table", nargs="?", help="table to backfill (or 'all' with --verify-fields)")
    ap.add_argument("--verify-only", action="store_true", help="count-compare only, no writes")
    ap.add_argument("--verify-fields", type=int, metavar="N", default=0,
                    help="field-level parity on N random rows per table, no writes")
    ap.add_argument("--batch", type=int, default=2000, help="rows per bulk round-trip")
    ap.add_argument("--rate-limit", type=float, default=0.0, metavar="ROWS_PER_SEC",
                    help="cap sustained backfill throughput (0 = unlimited)")
    ap.add_argument("--verify-all", action="store_true",
                    help="EXHAUSTIVE field parity over every row, no writes — "
                         "required before promoting a table to `mongo`")
    ap.add_argument("--examples", type=int, default=5, metavar="N",
                    help="offending rows to print per defect class (--verify-all)")
    ap.add_argument("--list", action="store_true", help="list supported tables")
    args = ap.parse_args()
    if args.list or not args.table:
        print("supported tables:", ", ".join(known_tables()))
        return 0
    if args.verify_all:
        tables = known_tables() if args.table == "all" else [args.table]
        worst = 0
        for t in tables:
            worst = max(worst, verify_all(t, batch=args.batch, examples=args.examples))
        return worst
    if args.verify_fields:
        tables = known_tables() if args.table == "all" else [args.table]
        worst = 0
        for t in tables:
            worst = max(worst, verify_fields(t, args.verify_fields))
        return worst
    return backfill(args.table, batch=args.batch, verify_only=args.verify_only,
                    rate_limit=args.rate_limit)


if __name__ == "__main__":
    raise SystemExit(main())
