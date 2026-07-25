#!/usr/bin/env python3
"""Is the V3 pipeline producing alpha, or re-buying beta?

Measured 2026-07-25 over 856 desks, the board earned +1.13% edge while staying
long every ticker it looked at earned +2.16%. Positive returns, worse than
doing nothing. This script decomposes that: it regresses realized per-decision
returns on the cross-sectional factor exposures of the names the pipeline
picked, and reports what is left over.

    python scripts/residual_alpha_report.py --since 2026-05-01 --horizon 7

Exposures are computed AS OF each decision date from that date's cross-section,
so nothing here can see the future. Reports only — it never gates a trade.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def fetch_decisions(since: str, horizon: int) -> list[dict]:
    """Every desk with a resolvable forward return, scored from price_history.

    Mirrors agent_scorecard's `--source price` path: `decision_outcomes` is
    bookkeeping-limited (n=40 and it ignores --since), while scoring desks
    straight off prices gives ~10-20x the sample and includes HOLDs.
    """
    from app.db.connection import get_db

    sessions = horizon + 1
    out: list[dict] = []
    with get_db() as db:
        rows = db.execute(
            """
            SELECT s.cycle_id, s.ticker, s.created_at, s.desk_data
            FROM shared_desk s
            WHERE s.created_at >= %s
            ORDER BY s.created_at ASC
            """,
            [since],
        ).fetchall()

        for cycle_id, ticker, created_at, desk_data in rows:
            desk = desk_data if isinstance(desk_data, dict) else json.loads(desk_data or "{}")
            as_of = created_at.date() if hasattr(created_at, "date") else created_at
            prices = db.execute(
                """
                SELECT close FROM price_history
                WHERE ticker = %s AND close IS NOT NULL AND date >= %s
                ORDER BY date ASC LIMIT %s
                """,
                [ticker, as_of, sessions],
            ).fetchall()
            if len(prices) < sessions:
                continue          # forward window hasn't closed yet
            try:
                entry, exit_ = float(prices[0][0]), float(prices[-1][0])
            except (TypeError, ValueError):
                continue
            if not entry or entry != entry or exit_ != exit_:
                continue

            decision = desk.get("trade_decision") or desk.get("final_decision") or {}
            action = str(decision.get("action") or "").upper()
            if action not in ("BUY", "SELL", "HOLD"):
                continue

            # Skip decisions no agent actually made — a degraded fallback
            # scores as a real opinion and is exactly the laundering the
            # provenance field exists to stop.
            prov = decision.get("decision_provenance") or desk.get("decision_provenance")
            if prov and str(prov).lower() in ("degraded_fallback", "coerced", "timeout_fallback"):
                continue

            held = bool((desk.get("cycle_metadata") or {}).get("held"))
            out.append({
                "cycle_id": cycle_id,
                "ticker": ticker,
                "as_of": as_of,
                "action": action,
                "held": held,
                "move_pct": (exit_ - entry) / entry * 100.0,
            })
    return out


def classify_executability(action: str, held: bool) -> str:
    if action == "BUY":
        return "consequential"
    if action == "SELL":
        return "consequential" if held else "blocked"
    if action == "HOLD":
        return "consequential" if held else "noop"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-05-01")
    ap.add_argument("--horizon", type=int, default=7,
                    help="Forward trading sessions to score over (default 7)")
    ap.add_argument("--executable-only", action="store_true",
                    help="Score ONLY decisions that can change the book. 69%% of "
                         "desks are policy-blocked SELLs or no-op HOLDs.")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    from app.quant import residual_alpha

    decisions = fetch_decisions(args.since, args.horizon)
    if not decisions:
        print(f"No scoreable desks since {args.since}.")
        return 1

    total = len(decisions)
    for d in decisions:
        d["executability"] = classify_executability(d["action"], d["held"])
    if args.executable_only:
        decisions = [d for d in decisions if d["executability"] == "consequential"]

    print("=" * 92)
    print(f"RESIDUAL ALPHA — {len(decisions)} of {total} desks since {args.since} "
          f"(+{args.horizon}-session horizon)"
          + ("  [CONSEQUENTIAL ONLY]" if args.executable_only else ""))
    print("=" * 92)

    if not decisions:
        print("Nothing left after filtering.")
        return 1

    # THE NULL. Always-long over the same names is what "doing nothing clever"
    # earns; the pipeline has to clear THIS, not zero.
    naive = sum(d["move_pct"] for d in decisions) / len(decisions)
    signed = [(-d["move_pct"] if d["action"] == "SELL" else d["move_pct"]) for d in decisions]
    taken = sum(signed) / len(signed)
    print(f"BASELINE  always-long over the same desks : {naive:+.2f}%")
    print(f"PIPELINE  return signed to actions taken  : {taken:+.2f}%")
    print(f"          difference vs the null          : {taken - naive:+.2f}%\n")

    report = residual_alpha.attribute_returns(decisions, horizon=args.horizon)
    print(residual_alpha.summarize(report))

    if report.get("ok"):
        print()
        if report["alpha_is_significant"]:
            print("VERDICT: residual alpha is statistically distinguishable from zero.")
        else:
            print("VERDICT: NO residual alpha. The pipeline's return is explained by its "
                  "factor exposure —\n         it is selling beta as alpha. This is the "
                  "measurement the wave exists to make;\n         it is a null result, not "
                  "a broken script.")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"generated_at": datetime.utcnow().isoformat(),
                       "since": args.since, "horizon": args.horizon,
                       "n_total": total, "report": report}, fh, indent=2, default=str)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
