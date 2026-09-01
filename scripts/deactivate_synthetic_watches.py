#!/usr/bin/env python3
"""Disarm watch-desk triggers that a TEST cycle armed.

WHY THIS EXISTS. `pipeline_service` auto-arms a baseline watch for every
analyzed ticker, and it did so with no `trade_flag` or cycle-kind gate. A watch
trip enqueues `START_V3_CYCLE` with `"trade": True` — watch_desk's own comment
calls a trip "a real decision moment" — so an observation run was able to point
the live wake machinery at price levels it computed with trading disabled.

The arming side is fixed (`pipeline_service` now refuses a synthetic cycle, see
`app/services/cycle_scope.py`). This script clears the rows that were armed
before that gate existed.

Found 2026-09-01: 8 active watches from observe cycles — NVDA/JPM/MP from
cycle-observe-1788220872 (the 2026-08-31 ladder) and ANET/APP/FSLR/GEV/NKE from
cycle-observe-1786140052, armed since 2026-08-07.

Reversible by design: sets `is_active` False and stamps why, never deletes, and
prints the exact ids so the change can be undone one row at a time.

    python3 scripts/deactivate_synthetic_watches.py              # dry run
    python3 scripts/deactivate_synthetic_watches.py --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import mongo_store  # noqa: E402
from app.services.cycle_scope import (  # noqa: E402
    SYNTHETIC_CYCLE_PREFIXES,
    is_synthetic_cycle,
)

DEACTIVATION_NOTE = "disarmed: armed by a synthetic cycle (see scripts/deactivate_synthetic_watches.py)"


def _is_synthetic_watch(row: dict) -> bool:
    """A watch is synthetic if its source cycle is, by id or by stated reason.

    Both are checked because `source_cycle_id` is not always populated on
    older rows, and the human-readable `reason` carries the cycle id verbatim
    ("watch-desk baseline from cycle <id> (HOLD)").
    """
    if is_synthetic_cycle(row.get("source_cycle_id")):
        return True
    reason = str(row.get("reason") or "")
    return any(prefix in reason for prefix in SYNTHETIC_CYCLE_PREFIXES)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually deactivate (default: report only)")
    a = ap.parse_args()

    active = mongo_store.find_docs("ticker_watches", {"is_active": True})
    synthetic = [r for r in active if _is_synthetic_watch(r)]

    print(f"active watches ......... {len(active)}")
    print(f"armed by a test cycle .. {len(synthetic)}\n")

    if not synthetic:
        print("nothing to do.")
        return 0

    for r in sorted(synthetic, key=lambda r: str(r.get("ticker"))):
        print(f"  {str(r.get('ticker')):6s} {str(r.get('id')):20s} "
              f"source={str(r.get('source_cycle_id') or '-'):28s} "
              f"{str(r.get('reason'))[:60]}")

    if not a.apply:
        print(f"\nDRY RUN — nothing changed. Re-run with --apply to deactivate "
              f"these {len(synthetic)} watch(es).")
        return 0

    now = datetime.now(timezone.utc)
    changed = []
    for r in synthetic:
        wid = r.get("id")
        if not wid:
            continue
        mongo_store.update_docs(
            "ticker_watches", {"id": wid},
            {"$set": {"is_active": False, "updated_at": now,
                      "deactivated_reason": DEACTIVATION_NOTE}},
        )
        changed.append(wid)

    print(f"\ndeactivated {len(changed)} watch(es).")
    print("undo one with:")
    print("  mongo_store.update_docs('ticker_watches', {'id': '<id>'}, "
          "{'$set': {'is_active': True}})")
    print("ids: " + ", ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
