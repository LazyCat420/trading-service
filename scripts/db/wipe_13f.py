"""Delete every 13F holding and reset the filer metadata that indexes it.

Destructive and irreversible. Run it only to rebuild the 13F corpus from
scratch:

    python3 scripts/db/wipe_13f.py --dry-run   # report what would go
    python3 scripts/db/wipe_13f.py --yes       # actually delete

> This script has never once run successfully. It called `db = get_db()`
> against a `@contextmanager`, so the first `.execute` raised AttributeError
> and the bare `except` turned it into exit code 2. That bug was the only
> safety this file had, and it had two others hiding behind it:
>
>   * **no `if __name__ == "__main__"` guard** — the DELETE sat at module
>     scope, so merely IMPORTING this module was enough to wipe the table.
>     `scripts/db/` has no `__init__.py`, which is the only reason a stray
>     `from scripts.db import *` never found it.
>   * **no confirmation** of any kind for an irreversible mass delete.
>
> Repairing only the `get_db()` call — which is what the open item asked for —
> would have armed the other two. Hence the flag, the guard, and the counts.
"""

import argparse
import os
import sys

# Run as a script from anywhere, the way the sibling db/ scripts did.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.db.connection import get_db  # noqa: E402 — must follow the path bootstrap


def wipe_13f(*, dry_run: bool) -> int:
    """Delete all 13F holdings. Returns the number of holdings removed."""
    with get_db() as db:
        holdings = db.execute("SELECT count(*) FROM sec_13f_holdings").fetchone()[0]
        filers = db.execute(
            "SELECT count(*) FROM sec_13f_filers WHERE latest_quarter IS NOT NULL"
        ).fetchone()[0]

        if dry_run:
            print(f"[dry-run] would delete {holdings} row(s) from sec_13f_holdings")
            print(f"[dry-run] would reset metadata on {filers} filer(s)")
            print("[dry-run] nothing was changed")
            return holdings

        db.execute("DELETE FROM sec_13f_holdings")
        print(f"[db] Deleted {holdings} row(s) from sec_13f_holdings")
        db.execute(
            "UPDATE sec_13f_filers SET latest_quarter = NULL, next_expected_filing = NULL"
        )
        print(f"[db] Reset metadata on {filers} filer(s)")
        return holdings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be deleted and change nothing")
    parser.add_argument("--yes", action="store_true",
                        help="required to actually delete; there is no undo")
    args = parser.parse_args(argv)

    if not args.dry_run and not args.yes:
        parser.error(
            "refusing to delete without --yes. Run --dry-run first to see the "
            "row counts; this cannot be undone."
        )

    try:
        wipe_13f(dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 — a CLI should not traceback at a human
        print(f"[db] FAILED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
