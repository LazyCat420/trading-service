#!/usr/bin/env python3
"""Measure every quality gate. READ-ONLY — this script never writes to Postgres.

Emits two artifacts:

  reports/data_quality_census.json              machine-readable, per gate
  reports/data_collection_improvement_checklist.md   the human deliverable:
      what we are collecting badly, with the number that proves it

Run it BEFORE the purge to see what will go, and AFTER to prove it went: every
gate must report 0 the second time. A census that only ever runs once cannot
tell you whether the purge worked.

Usage:
    python scripts/quality_census.py                # writes reports/
    python scripts/quality_census.py --stdout       # print, write nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from scripts import quality_gates as QG  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
# reports/ is gitignored — fine for the machine-readable census, which is
# regenerable. The human checklist goes in docs/, which is tracked, so the
# claim it makes is versioned beside the code it describes.
REPORTS = REPO / "reports"
DOCS = REPO / "docs"
CHECKLIST = DOCS / "DATA_COLLECTION_IMPROVEMENT_CHECKLIST.md"
LEDGER = REPO / "app" / "db" / "migration_ledger.json"


_ARCHIVE_FALLBACK_WARNED = False


def _dsn_from_file(path: Path, key: str) -> str | None:
    """One `KEY=value` out of a dotenv file, without importing dotenv."""
    try:
        for line in path.read_text().splitlines():
            if line.startswith(key + "=") or line.startswith(key + " ="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def pg_url() -> str:
    """The Postgres ARCHIVE DSN, for the tooling that legitimately wants it.

    Resolution order, and the order is the point:

      1. `PG_ARCHIVE_URL` in the environment
      2. `PG_ARCHIVE_URL` in `.env.migration` — the file
         `.env.migration.example` tells you to create, loaded EXPLICITLY here
         rather than into the ambient environment
      3. `DATABASE_URL` from the environment or `.env`, with a warning

    Step 3 is the compatibility path and it is on its way out. An archive DSN
    sitting in the ambient environment is exactly how ~36 legacy scripts kept
    reporting July numbers as current: they did not ask for the archive, they
    just found it. When `DATABASE_URL` is finally dropped from `.env` the only
    thing that should break is code that never said which store it wanted —
    and this function will already be reading the right variable for everything
    that did.

    The warning names the caller, because "something read the archive" is not
    actionable and "purge_bad_data read the archive" is.
    """
    global _ARCHIVE_FALLBACK_WARNED

    url = os.environ.get("PG_ARCHIVE_URL") or _dsn_from_file(
        REPO / ".env.migration", "PG_ARCHIVE_URL")
    if not url:
        url = os.environ.get("DATABASE_URL") or _dsn_from_file(REPO / ".env",
                                                               "DATABASE_URL")
        if url and not _ARCHIVE_FALLBACK_WARNED:
            _ARCHIVE_FALLBACK_WARNED = True
            import traceback
            caller = "?"
            for frame in reversed(traceback.extract_stack()[:-1]):
                if "quality_census" not in frame.filename:
                    caller = f"{Path(frame.filename).name}:{frame.lineno}"
                    break
            print(f"[pg_url] {caller} reached the ARCHIVE through DATABASE_URL. "
                  f"That variable is being removed; put PG_ARCHIVE_URL in "
                  f".env.migration (see .env.migration.example).", file=sys.stderr)
    if not url:
        raise SystemExit(
            "no archive DSN: set PG_ARCHIVE_URL in .env.migration "
            "(see .env.migration.example), or DATABASE_URL in the environment")
    return url.replace("postgresql+asyncpg://", "postgresql://")


def live_tables(cur) -> dict[str, dict]:
    cur.execute(
        """
        SELECT c.relname, pg_total_relation_size(c.oid), c.reltuples::bigint
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        """
    )
    return {r[0]: {"bytes": r[1], "est_rows": int(r[2])} for r in cur.fetchall()}


def exact_count(cur, table: str) -> int:
    cur.execute(f'SELECT count(*) FROM "{table}"')
    return cur.fetchone()[0]


def ledger_dispositions() -> dict[str, str]:
    if not LEDGER.exists():
        return {}
    data = json.loads(LEDGER.read_text())
    rows = data["tables"] if isinstance(data, dict) else data
    return {r["table"]: r.get("disposition") for r in rows}


def resolve_table_gates(cur, tables: dict[str, dict]) -> list[QG.TableGate]:
    """Static gates + the groups resolved against the live DB and the ledger."""
    gates = [g for g in QG.STATIC_TABLE_GATES if g.table in tables]
    named = {g.table for g in gates}

    # empty: 0 rows, trading-owned. Uses an EXACT count — reltuples is an
    # estimate and reports -1 for a never-analysed table.
    reason, evidence = QG.GROUP_REASONS["empty"]
    for t in sorted(tables):
        if t in named or t in QG.FOREIGN_TABLES:
            continue
        if exact_count(cur, t) == 0:
            gates.append(
                QG.TableGate(t, "empty", reason, evidence, "empty", dynamic=True)
            )
            named.add(t)

    # archive-only per the migration ledger
    reason, evidence = QG.GROUP_REASONS["archive_only"]
    for t, disp in sorted(ledger_dispositions().items()):
        if disp != "archive-only" or t in named or t in QG.FOREIGN_TABLES:
            continue
        if t in tables:
            gates.append(
                QG.TableGate(
                    t, "archive_only", reason, evidence, "archive_only", dynamic=True
                )
            )
            named.add(t)

    QG.assert_no_foreign([g.table for g in gates])
    return gates


def run_census(cur) -> dict:
    tables = live_tables(cur)
    table_gates = resolve_table_gates(cur, tables)

    row_results = []
    for g in QG.ROW_GATES:
        if g.table not in tables:
            row_results.append({**gate_meta(g), "skipped": "table absent"})
            continue
        total = exact_count(cur, g.table)
        try:
            cur.execute(f'SELECT count(*) FROM "{g.table}" WHERE {g.predicate}')
            hit = cur.fetchone()[0]
        except Exception as exc:
            # A gate whose predicate does not run measures nothing, and
            # "measured nothing" prints identically to "found nothing bad".
            # Fail closed and loudly instead of recording a zero.
            cur.connection.rollback()
            row_results.append({**gate_meta(g), "error": str(exc)[:200]})
            continue
        except KeyboardInterrupt:
            raise
        row_results.append(
            {
                **gate_meta(g),
                "rows_total": total,
                "rows_gated": hit,
                "rows_surviving": total - hit,
                "pct_gated": round(100.0 * hit / total, 2) if total else 0.0,
            }
        )

    table_results = [
        {
            "table": g.table,
            "gate": g.name,
            "group": g.group,
            "reason": g.reason,
            "evidence": g.evidence,
            "rows": exact_count(cur, g.table),
            "bytes": tables[g.table]["bytes"],
        }
        for g in table_gates
    ]

    foreign = sorted(t for t in QG.FOREIGN_TABLES if t in tables)
    foreign_rows = {t: exact_count(cur, t) for t in foreign}

    dropped = {r["table"] for r in table_results}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": "trading_bot",
        "totals": {
            "tables_live": len(tables),
            "tables_foreign_protected": len(foreign),
            "tables_to_drop": len(dropped),
            "tables_after_purge": len(tables) - len(dropped),
            "bytes_live": sum(t["bytes"] for t in tables.values()),
            "bytes_in_dropped_tables": sum(r["bytes"] for r in table_results),
            "rows_gated_in_kept_tables": sum(
                r.get("rows_gated", 0) for r in row_results
            ),
        },
        "row_gates": row_results,
        "table_gates": sorted(table_results, key=lambda r: (r["group"], r["table"])),
        "foreign_protected": foreign_rows,
    }


def gate_meta(g) -> dict:
    return {
        "table": g.table,
        "gate": g.name,
        "reason": g.reason,
        "evidence": g.evidence,
        "predicate": g.predicate,
    }


# ---------------------------------------------------------------------------
# The improvement checklist — measured collection defects, not the purge log
# ---------------------------------------------------------------------------
def collection_defects(cur) -> list[dict]:
    """Each defect: what is being collected badly, the number, where to fix it.

    Every number here is measured live at census time, so the checklist cannot
    quietly go stale against the database it describes.
    """
    out = []

    def add(area, defect, sql, fix, where, *, ratio=True, unit=""):
        """ratio=False for a metric that is a COUNT, not a share of rows.

        Reporting `count(DISTINCT step)` as "5.8% of rows" is a number computed
        correctly over the wrong set — it reads as a defect rate and is not one.
        Such metrics are reported as a bare count and sorted last.
        """
        try:
            cur.execute(sql)
            row = cur.fetchone()
        except Exception as exc:
            cur.connection.rollback()
            out.append({"area": area, "defect": defect, "measure": f"ERROR: {exc}"[:160],
                        "fix": fix, "where": where, "pct": -1})
            return
        total, bad = row[0], row[1]
        if not ratio:
            out.append({"area": area, "defect": defect,
                        "measure": f"{bad:,}{unit} (over {total:,} rows)",
                        "bad": bad, "total": total, "pct": -1,
                        "fix": fix, "where": where})
            return
        pct = round(100.0 * bad / total, 1) if total else 0.0
        out.append(
            {
                "area": area,
                "defect": defect,
                "measure": f"{bad:,} of {total:,} ({pct}%)",
                "bad": bad,
                "total": total,
                "pct": pct,
                "fix": fix,
                "where": where,
            }
        )

    add("pipeline_events", "Events are logged with an empty `{}` payload — the step "
        "fired but recorded nothing about what it did",
        "SELECT count(*), count(*) FILTER (WHERE data_json::text='{}') FROM pipeline_events",
        "Make the payload mandatory at the emit site; a step with nothing to say "
        "should not emit an event.",
        "app/db/pipeline_state.py, collectors that emit `news_scraped` and `consensus`")

    add("pipeline_events", "Step names embed the ticker and agent "
        "(`v2_debate_UBS_Technical_bull_t`), so every ticker invents new step kinds "
        "and no query can group by step",
        "SELECT count(*), count(DISTINCT step) FROM pipeline_events",
        "Emit `step='v2_debate'` with ticker/agent/side as separate fields.",
        "app/v3 debate emitters", ratio=False, unit=" distinct step names")

    add("news_articles", "quality_status is never assigned — the quality pipeline "
        "never ran on these rows",
        "SELECT count(*), count(*) FILTER (WHERE quality_status IS NULL) FROM news_articles",
        "Backfill the classifier over unscored rows and make scoring part of ingest.",
        "app/collectors news ingest + quality scorer")

    add("news_articles", "grounded_facts never extracted",
        "SELECT count(*), count(*) FILTER (WHERE grounded_facts IS NULL) FROM news_articles",
        "Either run fact extraction on ingest or drop the column from the contract.",
        "app/processors fact extraction")

    add("news_articles", "qualitative_draft never written",
        "SELECT count(*), count(*) FILTER (WHERE qualitative_draft IS NULL) FROM news_articles",
        "Same: populate on ingest or remove the column.",
        "app/processors")

    add("youtube_transcripts", "quality_status is never assigned",
        "SELECT count(*), count(*) FILTER (WHERE quality_status IS NULL) FROM youtube_transcripts",
        "Score transcripts at ingest like news articles.",
        "app/collectors youtube ingest")

    add("fundamentals", "market_cap missing — the provider returned a partial record",
        "SELECT count(*), count(*) FILTER (WHERE market_cap IS NULL) FROM fundamentals",
        "Retry partial fundamentals fetches and record which provider fields were "
        "absent instead of writing NULL silently.",
        "app/collectors/fund_scanner.py")

    add("fundamentals", "both P/E fields missing",
        "SELECT count(*), count(*) FILTER (WHERE pe_ratio IS NULL AND forward_pe IS NULL) FROM fundamentals",
        "As above.", "app/collectors/fund_scanner.py")

    add("fundamentals", "revenue missing",
        "SELECT count(*), count(*) FILTER (WHERE revenue IS NULL) FROM fundamentals",
        "As above.", "app/collectors/fund_scanner.py")

    add("decision_outcomes", "models_used not recorded — a decision cannot be "
        "attributed to the model that made it",
        "SELECT count(*), count(*) FILTER (WHERE models_used IS NULL) FROM decision_outcomes",
        "Record model provenance on every decision write.",
        "decision outcome writer")

    add("decision_outcomes", "skill_versions not recorded",
        "SELECT count(*), count(*) FILTER (WHERE skill_versions IS NULL) FROM decision_outcomes",
        "As above.", "decision outcome writer")

    add("cycle_run_summaries", "collector_failures empty — a cycle that lost "
        "collectors looks identical to a clean one",
        "SELECT count(*), count(*) FILTER (WHERE collector_failures IS NULL "
        "OR collector_failures::text = '[]' OR collector_failures::text = '{}') "
        "FROM cycle_run_summaries",
        "Record every collector failure on the cycle summary.",
        "app/services/cycle_scheduler.py")

    add("quant_equation_library", "parameters empty — an equation with no "
        "parameters cannot be reproduced",
        "SELECT count(*), count(*) FILTER (WHERE parameters IS NULL "
        "OR parameters::text = '{}') FROM quant_equation_library",
        "Persist parameters when the equation is registered.",
        "app/processors/quant_processor.py")

    add("price_history", "the same (ticker,date) day is carried by more than one "
        "source with no documented reader preference",
        "SELECT count(*), (SELECT count(*) FROM (SELECT ticker,date FROM price_history "
        "GROUP BY 1,2 HAVING count(DISTINCT source) > 1) d) FROM price_history",
        "NOT a duplicate defect — the natural key is (ticker,date,source) and it has "
        "zero duplicates. Pick a documented source priority for readers and add a "
        "UNIQUE index on (ticker,date,source) in Mongo.",
        "app/db/collections.py index spec + price readers",
        ratio=False, unit=" multi-source days")

    add("data_archive", "rows sit past the purge_after date the archiver stamped "
        "on them — the retention job never ran",
        "SELECT count(*), count(*) FILTER (WHERE purge_after < now()) FROM data_archive",
        "Schedule the retention sweep, or stop writing purge_after.",
        "archiver / janitor job")

    return out


def write_checklist(census: dict, defects: list[dict], path: Path) -> None:
    ts = census["generated_at"][:10]
    t = census["totals"]
    lines = [
        "# Data Collection Improvement Checklist",
        "",
        f"_Generated by `scripts/quality_census.py` on {ts} against the live "
        f"`trading_bot` database. Every number below is measured, not estimated —"
        f" re-run the script to refresh them._",
        "",
        "This is the **fix-the-collector** list: places where the pipeline stored a "
        "row but not the data the row was supposed to carry. It is separate from the "
        "purge itself — purging removes the bad rows we already have, this list stops "
        "us making more.",
        "",
        "## Defects, worst first",
        "",
        "| # | Area | What is wrong | Measured | Where to fix |",
        "|---|---|---|---|---|",
    ]
    # Rate-style defects first, worst rate at the top; count-style metrics
    # (pct == -1) sort to the bottom, since they are not a share of anything.
    ranked = sorted(defects, key=lambda d: (d.get("pct", 0) < 0, -d.get("pct", 0)))
    for i, d in enumerate(ranked, 1):
        lines.append(
            f"| {i} | `{d['area']}` | {d['defect']} | **{d['measure']}** | {d['where']} |"
        )
    lines += ["", "## What each fix is", ""]
    for i, d in enumerate(ranked, 1):
        lines.append(f"{i}. **`{d['area']}` — {d['defect']}**  ")
        lines.append(f"   Measured: {d['measure']}. Fix: {d['fix']}  ")
        lines.append(f"   Location: `{d['where']}`")
        lines.append("")
    lines += [
        "## Census summary at generation time",
        "",
        f"- Live tables: **{t['tables_live']}** "
        f"({t['tables_foreign_protected']} owned by other projects and protected)",
        f"- Tables gated for drop: **{t['tables_to_drop']}** "
        f"→ {t['tables_after_purge']} remain",
        f"- Bad rows gated inside kept tables: **{t['rows_gated_in_kept_tables']:,}**",
        f"- Database size: **{t['bytes_live'] / 1e9:.2f} GB**",
        "",
        "> A gate reporting 0 rows after the purge is the check that it worked. "
        "Re-run this script post-purge; every row gate must read 0.",
        "",
    ]
    path.write_text("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdout", action="store_true", help="print, write no files")
    args = ap.parse_args()

    with psycopg.connect(pg_url(), connect_timeout=30) as conn:
        cur = conn.cursor()
        census = run_census(cur)
        defects = collection_defects(cur)

    t = census["totals"]
    print(f"live tables ............... {t['tables_live']}")
    print(f"foreign (protected) ....... {t['tables_foreign_protected']}")
    print(f"tables gated for drop ..... {t['tables_to_drop']}")
    print(f"tables after purge ........ {t['tables_after_purge']}")
    print(f"bad rows in kept tables ... {t['rows_gated_in_kept_tables']:,}")
    print()
    print(f"{'TABLE':<34} {'GATE':<26} {'GATED':>10} {'OF':>12}")
    for r in census["row_gates"]:
        if "rows_gated" not in r:
            print(f"{r['table']:<34} {r['gate']:<26} {'-':>10}  {r.get('skipped') or r.get('error','')}")
            continue
        print(f"{r['table']:<34} {r['gate']:<26} {r['rows_gated']:>10,} {r['rows_total']:>12,}")
    print()
    by_group: dict[str, list] = {}
    for r in census["table_gates"]:
        by_group.setdefault(r["group"], []).append(r)
    for grp, rows in sorted(by_group.items()):
        n_rows = sum(r["rows"] for r in rows)
        mb = sum(r["bytes"] for r in rows) / 1e6
        print(f"drop group {grp:<14} {len(rows):>3} tables  {n_rows:>10,} rows  {mb:8.1f} MB")

    broken = [r for r in census["row_gates"] if "error" in r]
    if broken:
        print("\nBROKEN GATES — these measured NOTHING, which is not the same as", file=sys.stderr)
        print("finding nothing. Fix them before trusting this census:", file=sys.stderr)
        for r in broken:
            print(f"  {r['table']}.{r['gate']}: {r['error']}", file=sys.stderr)
        return 2

    if args.stdout:
        print(json.dumps(census, indent=2)[:2000])
        return 0

    REPORTS.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    (REPORTS / "data_quality_census.json").write_text(json.dumps(census, indent=2))
    write_checklist(census, defects, CHECKLIST)
    print(f"\nwrote {REPORTS / 'data_quality_census.json'}")
    print(f"wrote {CHECKLIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
