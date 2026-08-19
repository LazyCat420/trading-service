#!/usr/bin/env python3
"""Run each translated SELECT against BOTH stores and compare the rows.

A translation that parses is not a translation that is correct. This is the
check that separates the two: execute the original SQL against Postgres,
execute the generated Mongo call against Mongo, and compare the result sets
field by field.

Only SELECTs are checked, and nothing is written to either store — a
differential test that mutates is not repeatable.

Parameters: most statements carry `%s`, and the real argument values live at
the call site, not in the SQL. Rather than invent values (an invented value
that satisfies a filter becomes evidence), this samples REAL values from the
Postgres column the placeholder is compared against. A statement whose
parameters cannot be sampled that way is reported as UNTESTED, never as passed.

Verdicts:
  MATCH     same rows, same values
  DIFFER    both ran, results disagree  <- a translation bug
  UNTESTED  could not build parameters, or the table is empty
  ERROR     one side raised

Exit code is non-zero when anything DIFFERs or ERRORs.

Usage:
    python scripts/verify_translations.py --limit 120
    python scripts/verify_translations.py --json reports/translation_parity.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.db import mongo_query, mongo_store  # noqa: E402
from scripts.quality_census import pg_url  # noqa: E402
from scripts.sql_to_mongo import Unsupported, translate  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PARAM_RE = re.compile(r"\{p(\d+)\}")


def placeholder_columns(sql: str) -> list[str | None]:
    """For each %s in order, the column it is compared against (or None).

    Purely textual, and deliberately so: it only has to be good enough to pull
    a plausible real value out of the table. When it guesses wrong the values
    simply match nothing, and the row-count comparison still holds.
    """
    out: list[str | None] = []
    for m in re.finditer(r"%s", sql):
        head = sql[: m.start()]
        # Anchor the operator FIRST, then take the identifier immediately left
        # of it. The previous pattern let the identifier class swallow part of
        # the operator, so `resolution =` yielded the column "reso" and the
        # sample query failed with UndefinedColumn — scored ERROR against the
        # translator for a fault in this regex.
        col = re.search(
            r'([A-Za-z_][A-Za-z0-9_]*)"?\s*(?:>=|<=|<>|!=|=|>|<|\bIN\s*\()\s*$',
            head, re.IGNORECASE,
        )
        out.append(col.group(1) if col else None)
    return out


def sample_params(cur, table: str, sql: str) -> list | None:
    cols = placeholder_columns(sql)
    if not cols:
        return []
    if any(c is None for c in cols):
        return None
    try:
        select = ", ".join(f'"{c}"' for c in cols)
        not_null = " AND ".join(f'"{c}" IS NOT NULL' for c in cols)
        cur.execute(f'SELECT {select} FROM "{table}" WHERE {not_null} LIMIT 1')
    except Exception:
        cur.connection.rollback()
        return None
    row = cur.fetchone()
    return list(row) if row else None


def normalise(v):
    # Values must be hashable: the comparison counts rows as tuples. JSON
    # columns arrive as dict/list and Mongo money as Decimal128, all unhashable
    # — they were crashing the comparison and being scored ERROR, which looked
    # like a translator fault and was not.
    if type(v).__name__ == "Decimal128":
        return float(v.to_decimal())
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return json.dumps({k: str(x) for k, x in sorted(v.items())}, sort_keys=True)
    if isinstance(v, (list, tuple)):
        return json.dumps([str(x) for x in v])
    if isinstance(v, datetime):
        # Postgres keeps microseconds, BSON keeps milliseconds. Comparing at
        # full precision would report every timestamp as drift.
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).replace(microsecond=(v.microsecond // 1000) * 1000)
    if isinstance(v, date):
        # BSON has no date-only type, so a Postgres DATE round-trips as a
        # datetime at midnight. That is the migration behaving correctly, not
        # drift — compare both sides as midnight UTC.
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    return v


def rows_from_pg(cur, sql: str, params: list) -> tuple[list[str], list[tuple]]:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return cols, [tuple(normalise(x) for x in r) for r in cur.fetchall()]


def rows_from_mongo(call: str, params: list) -> list[dict]:
    env = {
        # EVERY translated SELECT calls mongo_query -- find_rows/find_dicts/
        # agg_row/group_rows/join_rows. It was missing from this namespace, so
        # every comparison raised NameError and scored ERROR: the checker was
        # judging nothing while still printing a percentage over the (empty)
        # comparable set. The safety net against "parses cleanly but wrong"
        # was itself the thing that had silently stopped working.
        "mongo_query": mongo_query,
        "mongo_store": mongo_store,
        "datetime": datetime, "timezone": timezone, "timedelta": timedelta,
    }
    for i, p in enumerate(params):
        env[f"_p{i}"] = p
    src = PARAM_RE.sub(lambda m: f"_p{m.group(1)}", call)
    return eval(src, env)  # noqa: S307 - source is generated by our translator


def as_row_list(result, returns: str) -> list:
    """Normalise a helper's return value into a LIST OF ROWS.

    find_row()/agg_row() return one row or None, exactly as fetchone() did,
    while find_rows()/group_rows()/join_rows() return a list. Comparing an
    unwrapped single row measured len(tuple) — the number of COLUMNS — against
    the Postgres ROW count, so every one-row statement differed on arithmetic
    that had nothing to do with its translation.
    """
    if returns in ("row", "one"):
        return [] if result is None else [result]
    if isinstance(result, list):
        return result
    return [result]


def compare(pg_cols, pg_rows, mongo_docs) -> tuple[str, str]:
    if len(pg_rows) != len(mongo_docs):
        return "DIFFER", f"row count pg={len(pg_rows)} mongo={len(mongo_docs)}"
    if not pg_rows:
        return "MATCH", "both empty"
    # `SELECT 1 FROM t WHERE ...` is an existence probe: Postgres names the
    # literal `?column?`, the caller never reads it, and Mongo has no such
    # field. Only the row count carries meaning, and it already matched.
    if all(c in ("?column?", "1") for c in pg_cols):
        return "MATCH", f"{len(pg_rows)} rows (existence probe, count only)"
    pg_set = Counter(tuple(sorted((c, v) for c, v in zip(pg_cols, r))) for r in pg_rows)
    mg_set = Counter()
    for d in mongo_docs:
        if isinstance(d, dict):
            # find_dicts() — a whole document, addressed by name.
            vals = []
            for c in pg_cols:
                if c not in d:
                    return "DIFFER", f"mongo doc missing field {c!r}"
                vals.append((c, normalise(d[c])))
        else:
            # find_rows/find_row/agg_row/group_rows/join_rows return TUPLES in
            # the SQL's column order -- that shape compatibility is the whole
            # reason the codemod could rewrite positional call sites. This
            # branch was missing, so `c not in d` tested the tuple's VALUES for
            # a column NAME, was almost always true, and reported every such
            # statement as "mongo doc missing field '<first column>'".
            row = d if isinstance(d, (list, tuple)) else (d,)
            if len(row) != len(pg_cols):
                return "DIFFER", (f"mongo row has {len(row)} values, "
                                  f"pg has {len(pg_cols)} columns")
            vals = [(c, normalise(v)) for c, v in zip(pg_cols, row)]
        mg_set[tuple(sorted(vals))] += 1
    if pg_set == mg_set:
        return "MATCH", f"{len(pg_rows)} rows"
    only_pg = sum((pg_set - mg_set).values())
    only_mg = sum((mg_set - pg_set).values())
    return "DIFFER", f"{only_pg} rows only in pg, {only_mg} only in mongo"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--show-differ", action="store_true")
    # The default inventory is the CURRENT tree's, which on this branch holds
    # almost no SQL left to judge — app/ is converted. The sweep's subject is
    # the SQL the conversion started from, so point it at the master-era
    # artifact (reports/sql_inventory_<sha>.json) to re-run the real check.
    ap.add_argument("--inventory", type=Path,
                    default=REPO / "reports" / "sql_inventory.json")
    args = ap.parse_args()

    inv = json.loads(args.inventory.read_text())
    selects = [s for s in inv["sites"]
               if not s["schema_file"] and s["kind"] == "mechanical"
               and s["verb"].upper() == "SELECT"]

    results, verdicts = [], Counter()
    seeded: dict[str, bool | None] = {}
    with psycopg.connect(pg_url(), connect_timeout=30) as conn:
        cur = conn.cursor()
        for site in selects:
            if args.limit and len(results) >= args.limit:
                break
            sql = site["sql"]
            try:
                t = translate(sql)
            except Unsupported:
                continue
            entry = {"file": site["file"], "line": site["line"],
                     "table": t.table, "sql": sql[:160], "call": t.call[:200]}
            params = sample_params(cur, t.table, sql)
            if seeded.get(t.table) is None:
                seeded[t.table] = mongo_store.count_docs(t.table) > 0
            if not seeded[t.table]:
                # The collection has not been backfilled yet. Comparing against
                # an empty collection measures the backfill, not the
                # translation, and would score every statement on the table as
                # a translator bug.
                entry["verdict"] = "NOT_SEEDED"
                entry["detail"] = f"{t.table} has no documents yet"
            elif params is None:
                entry["verdict"], entry["detail"] = "UNTESTED", "no real parameter values"
            else:
                try:
                    cols, pg_rows = rows_from_pg(cur, sql, params)
                    docs = as_row_list(rows_from_mongo(t.call, params), t.returns)
                    entry["verdict"], entry["detail"] = compare(cols, pg_rows, docs)
                except Exception as exc:
                    conn.rollback()
                    entry["verdict"] = "ERROR"
                    entry["detail"] = f"{type(exc).__name__}: {exc}"[:200]
            verdicts[entry["verdict"]] += 1
            results.append(entry)

    total = len(results)
    print(f"checked {total} translated SELECTs against live Postgres + Mongo\n")
    for v in ("MATCH", "DIFFER", "UNTESTED", "NOT_SEEDED", "ERROR"):
        n = verdicts[v]
        print(f"  {v:<9} {n:>4}   {100*n/total:.1f}%" if total else f"  {v:<9} {n:>4}")
    tested = verdicts["MATCH"] + verdicts["DIFFER"]
    if tested:
        print(f"\nof the {tested} actually comparable: "
              f"{100*verdicts['MATCH']/tested:.1f}% match")
    print("\nUNTESTED and ERROR are NOT passes — they are statements this run "
          "could not judge.")

    if args.show_differ:
        for r in results:
            if r["verdict"] in ("DIFFER", "ERROR"):
                print(f"\n{r['verdict']} {r['file']}:{r['line']}  ({r['detail']})")
                print(f"   SQL  {r['sql']}")
                print(f"   CALL {r['call']}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {"counts": dict(verdicts), "results": results}, indent=2))
        print(f"\nwrote {args.json}")
    return 1 if (verdicts["DIFFER"] or verdicts["ERROR"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
