#!/usr/bin/env python3
"""Assert the backend flag map and the migration ledger agree.

app/db/mongo_backends.env has claimed since phase 0.1 that "scripts/
check_backend_map.py asserts they agree and fails the build if not". That
script did not exist. This is it.

Two files have to stay in step, and nothing was checking either direction:

  app/db/mongo_backends.env      MONGO_STORE_BACKEND=table:mode,...
                                 -- what the containers actually run
  app/db/migration_ledger.json   per-table `mode_now`
                                 -- what the migration believes it has done

A disagreement is not cosmetic. If the ledger says `mongo` and the map says
`pg`, the migration reports a table as finished while Postgres is still
serving it. If the map says `mongo` and the ledger says `pg`, a table was cut
over with no record, so the next audit re-migrates it.

The two repos also keep byte-identical copies of the map. They are staged into
the containers separately, so a drift there means the service and the client
disagree about which store is authoritative -- which is how a read and its
mirror end up in different databases.

Exit codes: 0 agree, 1 disagreement, 2 a file is missing or unreadable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_MODES = frozenset({"pg", "dual", "mongo_read", "mongo"})


def parse_map(path: Path) -> dict[str, str]:
    """Parse MONGO_STORE_BACKEND out of a mongo_backends.env file."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("MONGO_STORE_BACKEND="):
            continue
        value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
        modes: dict[str, str] = {}
        for pair in value.split(","):
            if ":" not in pair:
                continue
            table, mode = pair.split(":", 1)
            modes[table.strip()] = mode.strip()
        return modes
    raise ValueError(f"{path} has no MONGO_STORE_BACKEND= line")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sibling",
        default=None,
        help="path to trading-client, whose copy of the map must match",
    )
    args = ap.parse_args(argv)
    # Resolved after parsing, not as a default, so a test that patches
    # REPO_ROOT also moves the sibling with it.
    sibling = args.sibling or str(REPO_ROOT.parent / "trading-client")

    env_path = REPO_ROOT / "app" / "db" / "mongo_backends.env"
    ledger_path = REPO_ROOT / "app" / "db" / "migration_ledger.json"

    for p in (env_path, ledger_path):
        if not p.exists():
            print(f"FAIL: {p} is missing", file=sys.stderr)
            return 2

    try:
        flags = parse_map(env_path)
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse failure is fatal here
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    problems: list[str] = []

    bad = {t: m for t, m in flags.items() if m not in VALID_MODES}
    for table, mode in sorted(bad.items()):
        problems.append(f"{table}: mode {mode!r} is not one of {sorted(VALID_MODES)}")

    rows = {r["table"]: r for r in ledger["tables"]}

    # Direction 1: every flagged table must exist in the ledger and agree.
    for table, mode in sorted(flags.items()):
        row = rows.get(table)
        if row is None:
            problems.append(
                f"{table}: flagged {mode!r} in mongo_backends.env but absent "
                "from migration_ledger.json"
            )
        elif row["mode_now"] != mode:
            problems.append(
                f"{table}: mongo_backends.env says {mode!r}, ledger says "
                f"{row['mode_now']!r}"
            )

    # Direction 2: the ledger may not claim a promotion the map does not carry.
    # This is the one that catches a hand-edited ledger reporting progress the
    # containers never received.
    for table, row in sorted(rows.items()):
        if row["mode_now"] != "pg" and table not in flags:
            problems.append(
                f"{table}: ledger says {row['mode_now']!r} but the table is "
                "absent from mongo_backends.env, so the containers run it at pg"
            )

    # The client keeps its own copy, staged into its own container.
    sibling_env = Path(sibling) / "app" / "db" / "mongo_backends.env"
    if not sibling_env.exists():
        print(f"SKIP: {sibling_env} not found (sibling repo not checked out here)")
    elif sibling_env.read_bytes() != env_path.read_bytes():
        problems.append(
            f"{sibling_env} differs from {env_path} -- the two containers would "
            "disagree about which store is authoritative"
        )

    # Files both repos must carry BYTE-IDENTICALLY. They share no runtime code
    # path -- only files -- so a divergence here is two different answers to
    # the same question, running in two containers against one database.
    #
    # `money_policy.py` decides which COLUMNS are Decimal128. The service
    # writes through it and the client reads through it, so if the copies drift
    # the client renders a raw bson.Decimal128 where the service stored money,
    # or demotes to float a column the service keeps exact. `collection_map`
    # decides which physical collection a table name resolves to, and two
    # answers there means two collections.
    for rel in ("app/db/money_policy.py", "app/db/collection_map.json"):
        ours = REPO_ROOT / rel
        theirs = Path(sibling) / rel
        # A missing file on EITHER side is a skip, not a failure, and the two
        # sides are treated the same on purpose. This runs against synthetic
        # trees in its own tests and against partial checkouts; the claim being
        # checked is "if both exist they must agree", and turning an absent
        # file into a failure only teaches people to pass --sibling /dev/null.
        # `test_the_real_repo_passes` is what pins the real tree.
        if not ours.exists():
            print(f"SKIP: {ours} not found")
        elif not theirs.exists():
            print(f"SKIP: {theirs} not found (sibling repo not checked out here)")
        elif ours.read_bytes() != theirs.read_bytes():
            problems.append(
                f"{theirs} is not byte-identical to {ours} -- the two repos "
                "would disagree about the same contract at runtime"
            )

    if problems:
        print(f"FAIL: {len(problems)} disagreement(s) between the map and the ledger:")
        for p in problems:
            print(f"  - {p}")
        return 1

    counts: dict[str, int] = {}
    for mode in flags.values():
        counts[mode] = counts.get(mode, 0) + 1
    summary = ", ".join(f"{n} {m}" for m, n in sorted(counts.items()))
    print(f"OK: {len(flags)} flagged table(s) agree with the ledger ({summary}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
