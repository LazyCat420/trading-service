#!/usr/bin/env python3
"""How far along is the post-Phase-A measurement window, and what died on the way?

Ch.91's rule: no decision-logic change ships until ~100 clean production
decisions accumulate after the Phase A boundary (2026-08-23), then attack1 /
attack2 / hold_wall re-run and their verdict picks the next fix. This report
is the go/no-go counter for that rule — and the visibility fix for the desks
that never become decisions: 45 of 74 desks (08-23..26) died as
`board_degraded_fallback` with NO trade_results row, so every
trade_results-based report silently skipped them.

    python3 scripts/measurement_window_report.py
    python3 scripts/measurement_window_report.py --boundary 2026-08-23 --target 100

Read-only. Safe against a live cycle.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import mongo_query  # noqa: E402
from scripts.hold_wall_report import _is_production_cycle  # noqa: E402

PHASE_A_BOUNDARY = "2026-08-23"


def _as_dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, (str, bytes)):
        try:
            out = json.loads(v)
            return out if isinstance(out, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--boundary", default=PHASE_A_BOUNDARY,
                    help="window start, ISO date (default: Phase A deploy day)")
    ap.add_argument("--target", type=int, default=100,
                    help="clean decisions needed before attack1 re-runs")
    ap.add_argument("--json", dest="json_out", default=None)
    a = ap.parse_args()

    win = {"created_at": {"$gte": a.boundary}}

    # ── decisions (trade_results) ──
    decisions = []
    for d in mongo_query.find_dicts(
        "trade_results", win,
        ["cycle_id", "ticker", "action", "policy_action",
         "decision_provenance", "confidence", "created_at"],
    ):
        if _is_production_cycle(d.get("cycle_id")):
            decisions.append(d)

    clean = [d for d in decisions
             if str(d.get("decision_provenance") or "") not in
             ("board_degraded_fallback", "timeout_abort")]

    # ── desks, including the ones that never became a decision ──
    desk_days: dict[str, Counter] = defaultdict(Counter)
    degraded_examples: list[tuple] = []
    for d in mongo_query.find_dicts(
        "shared_desk", win, ["cycle_id", "ticker", "created_at", "desk_data"],
    ):
        if not _is_production_cycle(d.get("cycle_id")):
            continue
        day = str(d.get("created_at"))[:10]
        fd = _as_dict(d.get("desk_data")).get("final_decision") or {}
        prov = str(fd.get("decision_provenance") or "none")
        if prov == "board_degraded_fallback":
            desk_days[day]["degraded"] += 1
            if len(degraded_examples) < 8:
                degraded_examples.append((day, d.get("ticker"), d.get("cycle_id")))
        else:
            desk_days[day]["ok"] += 1

    total_desks = sum(sum(c.values()) for c in desk_days.values())
    total_degraded = sum(c["degraded"] for c in desk_days.values())

    # Vacuity guard: zero desks means the query window or store is wrong,
    # not that the pipeline is healthy.
    if not total_desks:
        print(f"NO PRODUCTION DESKS since {a.boundary} — this report proved "
              f"nothing. Check the boundary date and the Mongo connection "
              f"before reading anything into the zeros.")
        return 1

    n = len(clean)
    days_elapsed = max(1, len(desk_days))
    rate = n / days_elapsed
    remaining = max(0, a.target - n)
    eta_days = (remaining / rate) if rate > 0 else float("inf")

    actions = Counter(str(d.get("action")) for d in clean)
    gates = Counter(str(d.get("policy_action"))[:44] for d in clean)
    confs = [d.get("confidence") for d in clean
             if isinstance(d.get("confidence"), (int, float))]

    print(f"Measurement window since {a.boundary} (target {a.target} clean decisions)\n")
    print(f"  clean decisions ........ {n}  ({rate:.1f}/day over {days_elapsed} active days)")
    print(f"  degraded/aborted rows .. {len(decisions) - n}")
    print(f"  actions ................ {dict(actions)}")
    print(f"  act rate ............... "
          f"{sum(v for k, v in actions.items() if k in ('BUY', 'SELL')) / n:.1%}"
          if n else "  act rate ............... n/a")
    if confs:
        print(f"  confidence ............. min {min(confs)} / max {max(confs)} / "
              f">=70: {sum(1 for c in confs if c >= 70)} / >=80: {sum(1 for c in confs if c >= 80)}")
    print(f"  gate mix ............... {dict(gates)}")
    print(f"\n  desk mortality (the decisions that never happened):")
    print(f"  desks .................. {total_desks}, degraded {total_degraded} "
          f"({total_degraded / total_desks:.0%})")
    for day in sorted(desk_days):
        c = desk_days[day]
        print(f"    {day}: ok={c['ok']:3} degraded={c['degraded']:3}")
    if degraded_examples:
        print(f"  degraded examples: "
              + ", ".join(f"{t}@{d}" for d, t, _ in degraded_examples[:6]))

    if remaining:
        print(f"\n  GO/NO-GO: NO-GO — {remaining} clean decisions short; "
              f"~{eta_days:.0f} more day(s) at the current rate. "
              f"(Degraded desks slow this clock: every one is a decision "
              f"that never accrued.)")
    else:
        print(f"\n  GO/NO-GO: GO — window closed. Re-run attack1 (shuffle/"
              f"stickiness), attack2 vs grounding_decay_report, and "
              f"hold_wall_report (LEAK must be 0), judged against "
              f"power_report's detectable-effect floor.")

    if a.json_out:
        Path(a.json_out).write_text(json.dumps({
            "boundary": a.boundary, "target": a.target, "clean": n,
            "per_day": {d: dict(c) for d, c in sorted(desk_days.items())},
            "actions": dict(actions), "gates": dict(gates),
            "degraded_desks": total_degraded, "desks": total_desks,
        }, indent=2, default=str))
        print(f"\nwrote {a.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
