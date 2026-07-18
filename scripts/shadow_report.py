#!/usr/bin/env python3
"""
Contradiction-Shadow aggregate report.

Reads the observation-only contradiction-shadow telemetry that
app/v3/contradiction_shadow.py records onto every finished desk
(shared_desk.desk_data.agent_telemetry, agent="contradiction_shadow") and
summarizes how often cross-agent dissent actually fires — the empirical
input for deciding whether to promote the shadow into a real gate.

Usage:
    python scripts/shadow_report.py                 # all shadow-era desks
    python scripts/shadow_report.py --hours 24      # last 24h only
    python scripts/shadow_report.py --recent 15     # show N recent flagged
"""

import argparse
import json
import os
from collections import Counter

import psycopg
from dotenv import load_dotenv

load_dotenv()


def _iter_shadow_desks(hours=None):
    conn = psycopg.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    if hours:
        cur.execute(
            "SELECT ticker, phase, desk_data, updated_at FROM shared_desk "
            "WHERE updated_at > NOW() - (%s || ' hours')::interval "
            "ORDER BY updated_at DESC",
            (str(hours),),
        )
    else:
        cur.execute(
            "SELECT ticker, phase, desk_data, updated_at FROM shared_desk "
            "ORDER BY updated_at DESC"
        )
    for ticker, phase, dd, upd in cur.fetchall():
        data = json.loads(dd) if isinstance(dd, str) else dd
        tele = (data or {}).get("agent_telemetry") or []
        shadow = next(
            (t for t in tele if t.get("agent") == "contradiction_shadow"), None
        )
        if shadow is not None:
            yield ticker, phase, upd, shadow, data
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=None)
    ap.add_argument("--recent", type=int, default=10)
    args = ap.parse_args()

    n = 0
    n_contra = 0
    n_downgrade = 0
    claims_total = 0
    pair_counter = Counter()
    downgrade_action = Counter()
    flagged = []

    for ticker, phase, upd, shadow, data in _iter_shadow_desks(args.hours):
        n += 1
        claims_total += shadow.get("claims_extracted", 0) or 0
        cc = shadow.get("contradiction_count", 0) or 0
        if cc:
            n_contra += 1
        if shadow.get("would_downgrade_to_hold"):
            n_downgrade += 1
            downgrade_action[str(shadow.get("final_action", "?"))] += 1
        for c in shadow.get("contradictions", []):
            pair = " vs ".join(sorted([c.get("source_ref_1", "?"), c.get("source_ref_2", "?")]))
            pair_counter[pair] += 1
        if cc:
            flagged.append((upd, ticker, shadow))

    scope = f"last {args.hours}h" if args.hours else "all shadow-era desks"
    print(f"══ Contradiction-Shadow report ({scope}) ══")
    if n == 0:
        print("No desks carry shadow telemetry yet.")
        return
    pct = lambda x: f"{100*x/n:.0f}%"
    print(f"desks analyzed:            {n}")
    print(f"  ≥1 contradiction:        {n_contra}  ({pct(n_contra)})")
    print(f"  would_downgrade_to_hold: {n_downgrade}  ({pct(n_downgrade)})   ← live BUY/SELL a gate would flip to HOLD")
    print(f"avg claims / desk:         {claims_total/n:.1f}")

    if pair_counter:
        print("\ncontradiction by source pair:")
        for pair, cnt in pair_counter.most_common():
            print(f"  {cnt:3}  {pair}")

    if downgrade_action:
        print("\nwould-downgrade cases by final action:")
        for act, cnt in downgrade_action.most_common():
            print(f"  {cnt:3}  {act}")

    if flagged:
        print(f"\nrecent flagged desks (≤{args.recent}):")
        for upd, ticker, shadow in flagged[: args.recent]:
            sm = shadow.get("sentiment_by_source", {})
            print(
                f"  {upd:%m-%d %H:%M} {ticker:6} "
                f"{shadow.get('final_action','?')}@{shadow.get('final_confidence','?')} "
                f"downgrade={shadow.get('would_downgrade_to_hold')}  {sm}"
            )


if __name__ == "__main__":
    main()
