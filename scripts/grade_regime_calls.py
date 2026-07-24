#!/usr/bin/env python3
"""Grade the Regime Engine's forward calls against the realized tape.

The regime engine emits a label, seven factor scores and a board directive —
none of which the pipeline could ever be wrong about, because none of them
predicted anything. Its artifacts were 53/53 unscoreable in the agent
scorecard. `forward_call` (added 2026-07-24) fixes that: a 5-trading-day SPX
direction and VIX direction, with a conviction.

This grades those calls once the window has passed, using the same
`asset_prices` closes the engine was shown.

    UP/DOWN   correct when the realized 5d move clears ±1%; FLAT is the
              deadband, matching outcome_tracker's WIN/LOSS thresholds.
    RISING/   VIX direction on the same ±5% deadband — volatility moves in
    FALLING   percentage terms, so a 1% band would call everything a hit.

Read-only. Usage:
    python scripts/grade_regime_calls.py [--since 2026-07-24]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

HORIZON_DAYS = 5
SPX_DEADBAND_PCT = 1.0
VIX_DEADBAND_PCT = 5.0


def _load_closes(symbol: str) -> list[tuple]:
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            "SELECT date, close FROM asset_prices WHERE symbol = %s "
            "AND close IS NOT NULL ORDER BY date ASC",
            [symbol],
        ).fetchall()
    # NaN survives a NOT NULL check; it is not a price.
    return [(d, float(c)) for d, c in rows if c == c]


def _move_after(closes: list[tuple], start_date, days: int) -> float | None:
    """Percent move over `days` trading days starting from the first close on
    or after `start_date`. None when the window hasn't closed yet."""
    idx = next((i for i, (d, _) in enumerate(closes) if d >= start_date), None)
    if idx is None or idx + days >= len(closes):
        return None
    start, end = closes[idx][1], closes[idx + days][1]
    if not start:
        return None
    return (end - start) / start * 100.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-07-24", help="Only grade calls made on/after this date")
    ap.add_argument("--json", dest="json_out", help="Write the raw report here")
    args = ap.parse_args()

    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            """
            SELECT cycle_id, ticker, created_at, desk_data
            FROM shared_desk
            WHERE created_at >= %s
            ORDER BY created_at ASC
            """,
            [args.since],
        ).fetchall()

    spx = _load_closes("GSPC")
    vix = _load_closes("VIX")
    if not spx:
        print("No GSPC history in asset_prices — cannot grade.")
        return 1

    # One regime call per cycle (the per-ticker duplication was removed on
    # 2026-07-24); dedupe defensively so older cycles don't count 6 times.
    seen: set[str] = set()
    graded: list[dict] = []
    pending = 0
    missing = 0

    for cycle_id, _ticker, created_at, desk_data in rows:
        if cycle_id in seen:
            continue
        desk = desk_data if isinstance(desk_data, dict) else json.loads(desk_data or "{}")
        regime = desk.get("regime_classification") or {}
        call = regime.get("forward_call")
        if not isinstance(call, dict) or not call:
            missing += 1
            continue
        seen.add(cycle_id)

        made_on = created_at.date() if hasattr(created_at, "date") else created_at
        spx_move = _move_after(spx, made_on, HORIZON_DAYS)
        vix_move = _move_after(vix, made_on, HORIZON_DAYS) if vix else None
        if spx_move is None:
            pending += 1
            continue

        realized_spx = "UP" if spx_move > SPX_DEADBAND_PCT else ("DOWN" if spx_move < -SPX_DEADBAND_PCT else "FLAT")
        realized_vol = None
        if vix_move is not None:
            realized_vol = ("RISING" if vix_move > VIX_DEADBAND_PCT
                            else ("FALLING" if vix_move < -VIX_DEADBAND_PCT else "STABLE"))

        graded.append({
            "cycle_id": cycle_id,
            "date": str(made_on),
            "regime": regime.get("regime"),
            "spx_call": call.get("spx_direction"),
            "spx_realized": realized_spx,
            "spx_move_pct": round(spx_move, 2),
            "vol_call": call.get("vol_direction"),
            "vol_realized": realized_vol,
            "vix_move_pct": round(vix_move, 2) if vix_move is not None else None,
            "conviction": call.get("conviction"),
        })

    if not graded:
        print(f"No gradeable regime calls since {args.since} "
              f"({pending} still inside the {HORIZON_DAYS}-day window, "
              f"{missing} cycles with no forward_call).")
        return 0

    spx_hits = sum(1 for g in graded if g["spx_call"] and g["spx_call"] == g["spx_realized"])
    spx_n = sum(1 for g in graded if g["spx_call"])
    vol_hits = sum(1 for g in graded if g["vol_call"] and g["vol_call"] == g["vol_realized"])
    vol_n = sum(1 for g in graded if g["vol_call"] and g["vol_realized"])

    print(f"\nREGIME FORWARD CALLS — {len(graded)} graded since {args.since} "
          f"({pending} pending, {missing} without a call)")
    print("-" * 92)
    print(f"{'date':<12} {'regime':<16} {'SPX call':<9} {'real':<6} {'move':>7}   "
          f"{'vol call':<9} {'real':<8} {'conv':>4}")
    for g in graded:
        mark = "✓" if g["spx_call"] == g["spx_realized"] else "✗"
        print(f"{g['date']:<12} {str(g['regime'])[:15]:<16} {str(g['spx_call']):<9} "
              f"{g['spx_realized']:<6} {g['spx_move_pct']:>6.2f}% {mark} "
              f"{str(g['vol_call']):<9} {str(g['vol_realized']):<8} {str(g['conviction']):>4}")

    print("-" * 92)
    if spx_n:
        print(f"SPX direction: {spx_hits}/{spx_n} = {spx_hits/spx_n*100:.0f}%")
    if vol_n:
        print(f"VIX direction: {vol_hits}/{vol_n} = {vol_hits/vol_n*100:.0f}%")

    # Calibration: high-conviction calls should beat low-conviction ones. If
    # they don't, the conviction number is decoration.
    buckets: dict[str, list[bool]] = defaultdict(list)
    for g in graded:
        if not g["spx_call"]:
            continue
        conv = g.get("conviction")
        label = "unknown" if conv is None else ("high (>=70)" if conv >= 70 else
                                               "mid (40-69)" if conv >= 40 else "low (<40)")
        buckets[label].append(g["spx_call"] == g["spx_realized"])
    if len(buckets) > 1:
        print("\ncalibration by conviction:")
        for label, hits in sorted(buckets.items()):
            print(f"  {label:<14} {sum(hits)}/{len(hits)} = {sum(hits)/len(hits)*100:.0f}%")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"since": args.since, "graded": graded,
                       "spx_hit_rate": (spx_hits / spx_n * 100) if spx_n else None,
                       "vol_hit_rate": (vol_hits / vol_n * 100) if vol_n else None}, f,
                      indent=2, default=str)
        print(f"\nwrote {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    raise SystemExit(main())
