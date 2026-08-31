#!/usr/bin/env python3
"""Is anything in the trading cycle still touching Postgres?

The cutover's soak criterion is not "the code has no psycopg import" — that is
a static fact and it is checked elsewhere. It is that Postgres RECEIVES NOTHING
for the trading tables over three full cycles. This probe measures that from
the database's own counters, which no amount of application code can talk its
way past.

  scripts/pg_quiescence.py --snapshot > pg_baseline.json   # at T0
  scripts/pg_quiescence.py --diff pg_baseline.json         # after each cycle

WHY NOT pg_stat_database
------------------------
treesearch-service lives in the same `trading_bot` database and keeps writing
throughout — it is not part of this migration and never stops. Every
database-wide counter therefore moves forever, and a check reading one could
never go quiet. So every number here is per TABLE, with the 14 treesearch
tables excluded by an explicit allowlist (`quality_gates.FOREIGN_OWNERS`) — a
name pattern would silently adopt the next table someone adds.

WHAT COUNTS AS A TOUCH
----------------------
Reads as well as writes. `seq_scan` and `idx_scan` catch a SELECT, which is
what a forgotten dashboard query or a cron report looks like — it changes no
data, and it is exactly the coupling that turns "Postgres is frozen archive"
back into "Postgres is load-bearing". A nonzero delta NAMES its table, because
the next question is always which one.

READ-ONLY: SELECTs against the statistics views, nothing else. Safe to run
against production at any time.
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

from scripts.quality_gates import FOREIGN_OWNERS  # noqa: E402

FOREIGN_TABLES = {t for tables in FOREIGN_OWNERS.values() for t in tables}

COUNTERS = ("seq_scan", "idx_scan", "n_tup_ins", "n_tup_upd", "n_tup_del")


# The archive DSN comes from ONE place, and it is not this file. This used to
# be a byte-equivalent private copy of `quality_census.pg_url()`, which meant
# the seam close had two copies to find; worse, only one of them would have
# learned to prefer PG_ARCHIVE_URL, and the retirement instrument would have
# been the one still reading the ambient DATABASE_URL.
from scripts.quality_census import pg_url  # noqa: E402


def snapshot() -> dict:
    with psycopg.connect(pg_url(), connect_timeout=30) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT relname, seq_scan, idx_scan, n_tup_ins, n_tup_upd, n_tup_del "
            "FROM pg_stat_user_tables WHERE schemaname = 'public' ORDER BY relname")
        tables = {
            r[0]: dict(zip(COUNTERS, [int(v or 0) for v in r[1:]]))
            for r in cur.fetchall() if r[0] not in FOREIGN_TABLES
        }
        # `state` is included because an idle connection still names a client
        # that has not been switched off. `datname` is not a filter here — one
        # database, several projects.
        cur.execute(
            "SELECT application_name, client_addr::text, state, count(*) "
            "FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() GROUP BY 1,2,3 ORDER BY 4 DESC")
        clients = [
            {"application_name": r[0] or "", "client_addr": r[1] or "",
             "state": r[2] or "", "connections": int(r[3])}
            for r in cur.fetchall()
        ]
        # Counters reset on `pg_stat_reset()` and on a crash recovery. A diff
        # across a reset would read as "quiet" because every number got
        # SMALLER, so the reset time travels with the snapshot.
        cur.execute("SELECT stats_reset FROM pg_stat_database "
                    "WHERE datname = current_database()")
        row = cur.fetchone()

    return {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "stats_reset": row[0].isoformat() if row and row[0] else None,
        "excluded_foreign_tables": sorted(FOREIGN_TABLES),
        "tables": tables,
        "clients": clients,
    }


def diff(baseline: dict, now: dict) -> int:
    if baseline.get("stats_reset") != now.get("stats_reset"):
        print("INCONCLUSIVE: the statistics were reset between the two "
              f"snapshots ({baseline.get('stats_reset')} -> {now.get('stats_reset')}). "
              "Every counter restarted at zero, so a quiet diff here would be "
              "an artifact. Take a fresh baseline and restart the soak clock.")
        return 2

    moved: list[tuple[str, dict]] = []
    for table, counters in sorted(now["tables"].items()):
        before = baseline["tables"].get(table, dict.fromkeys(COUNTERS, 0))
        delta = {c: counters[c] - int(before.get(c, 0)) for c in COUNTERS}
        if any(v > 0 for v in delta.values()):
            moved.append((table, delta))

    # A table that DISAPPEARED between snapshots is not quiet, it is gone — and
    # a dropped table's counters vanish with it, which would otherwise read as
    # perfect silence.
    vanished = sorted(set(baseline["tables"]) - set(now["tables"]))

    print(f"baseline {baseline['taken_at']}  ->  now {now['taken_at']}")
    print(f"  {len(now['tables'])} non-treesearch tables "
          f"({len(FOREIGN_TABLES)} excluded)")
    if vanished:
        print(f"\n  {len(vanished)} table(s) present at baseline and gone now: "
              f"{', '.join(vanished)}")
    if moved:
        print(f"\n  NOT QUIESCENT — {len(moved)} table(s) touched:\n")
        width = max(len(t) for t, _ in moved)
        for table, delta in moved:
            parts = ", ".join(f"{c}+{v}" for c, v in delta.items() if v > 0)
            print(f"    {table:<{width}}  {parts}")
    else:
        print("\n  QUIESCENT: no reads, no writes on any non-treesearch table.")

    known = {c["application_name"] for c in baseline["clients"]}
    new_clients = [c for c in now["clients"] if c["application_name"] not in known]
    if new_clients:
        print(f"\n  {len(new_clients)} client(s) not present at baseline:")
        for c in new_clients:
            print(f"    {c['application_name'] or '(unnamed)'} "
                  f"{c['client_addr']} {c['state']} ×{c['connections']}")

    return 1 if (moved or vanished) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", action="store_true", help="write a snapshot to stdout")
    ap.add_argument("--diff", type=Path, metavar="BASELINE",
                    help="compare the live counters against a saved snapshot")
    args = ap.parse_args()

    if not args.snapshot and not args.diff:
        ap.error("pass --snapshot or --diff BASELINE")

    now = snapshot()
    if args.snapshot:
        print(json.dumps(now, indent=1))
        return 0
    return diff(json.loads(args.diff.read_text(encoding="utf-8")), now)


if __name__ == "__main__":
    raise SystemExit(main())
