#!/usr/bin/env python3
"""WHY does the board overriding the fundamental desk destroy value?

The override matrix (2026-07-25) found the one handoff that survives a
permutation test: when the board contradicts the fundamental desk's
`near_term_read`, it earns **-0.97%** vs **+0.39%** when it agrees — a -1.36%
gap at p=0.005 (n=377), widening to **-2.82% at p=0.0002** on the 124 desks
with a trustworthy `held` flag.

Before gating anything, establish WHICH override is the costly one. A gate on
the wrong subset is worse than none: it would suppress the board's legitimate
disagreements and leave the damaging ones untouched.

Hypotheses tested here:
  H1 DIRECTION   — is one direction of override worse (board bullish over a
                   bearish desk, vs the reverse)?
  H2 CONVICTION  — does it depend on the fundamental desk's confidence, or on
                   `matters_this_week`? Overriding a desk that says "this does
                   not bear on the next 2 weeks" should be CHEAP and correct.
  H3 CONFIDENCE  — is the board's own confidence informative, i.e. does a
                   high-confidence override do better than a low-confidence one?
  H4 ACTION      — is the damage concentrated in BUYs, SELLs, or HOLDs?

Whichever slice carries the damage is the slice a gate should target.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PERMUTATIONS = 20_000


def perm_p(a: np.ndarray, b: np.ndarray, seed: int = 20260725) -> float | None:
    if a.size < 2 or b.size < 2:
        return None
    rng = np.random.default_rng(seed)
    pooled = np.concatenate([a, b])
    obs = abs(a.mean() - b.mean())
    n_a = a.size
    hits = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(pooled)
        if abs(pooled[:n_a].mean() - pooled[n_a:].mean()) >= obs:
            hits += 1
    return (hits + 1) / (PERMUTATIONS + 1)


def _cell(label: str, vals: list[float], indent: str = "   ") -> None:
    a = np.array(vals, dtype=float)
    if a.size == 0:
        print(f"{indent}{label:<44} n=   0")
        return
    print(f"{indent}{label:<44} n={a.size:4d}  mean={a.mean():+.2f}%  "
          f"sd={a.std(ddof=1) if a.size > 1 else float('nan'):.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-05-01")
    ap.add_argument("--horizon", type=int, default=7)
    args = ap.parse_args()

    from scripts.agent_scorecard import fetch_rows_from_prices, _stance

    rows = fetch_rows_from_prices(args.since, args.horizon)
    print("=" * 96)
    print(f"OVERRIDE DIAGNOSIS — board vs fundamental desk, {len(rows)} desks "
          f"since {args.since} (+{args.horizon}d)")
    print("=" * 96)

    # Build the override population once.
    recs = []
    for row in rows:
        fund = row["desk"].get("fundamental_report") or {}
        board = row["desk"].get("final_decision") or {}
        up, down = _stance(fund), _stance(board)
        if up is None or down is None:
            continue
        nt = fund.get("near_term_read") if isinstance(fund.get("near_term_read"), dict) else {}
        recs.append({
            "overrode": up != down,
            "up": up, "down": down,
            "signed": down * row["move_pct"],
            "board_conf": board.get("confidence"),
            "fund_conf": fund.get("confidence"),
            "matters": nt.get("matters_this_week"),
            "action": str(board.get("action", "")).upper(),
        })

    ov = [r for r in recs if r["overrode"]]
    ag = [r for r in recs if not r["overrode"]]
    print(f"\npopulation: {len(ag)} agreed / {len(ov)} overrode\n")
    _cell("ALL agreed", [r["signed"] for r in ag])
    _cell("ALL overrode", [r["signed"] for r in ov])
    p = perm_p(np.array([r["signed"] for r in ov]), np.array([r["signed"] for r in ag]))
    print(f"   -> permutation p = {p}\n")

    # ── H1 direction of the override ──
    print("H1  DIRECTION OF OVERRIDE")
    buckets = defaultdict(list)
    for r in ov:
        name = f"desk {'BULL' if r['up']>0 else 'BEAR' if r['up']<0 else 'NEUT'}" \
               f" -> board {'BULL' if r['down']>0 else 'BEAR' if r['down']<0 else 'NEUT'}"
        buckets[name].append(r["signed"])
    for k in sorted(buckets, key=lambda k: np.mean(buckets[k])):
        _cell(k, buckets[k])

    # ── H2 does the desk say it matters this week? ──
    print("\nH2  FUNDAMENTAL DESK'S OWN near_term_read.matters_this_week")
    print("    (overriding a desk that says 'does not matter this week' SHOULD be cheap)")
    for flag in (True, False, None):
        sub = [r["signed"] for r in ov if r["matters"] is flag]
        _cell(f"overrode when matters_this_week={flag}", sub)
    for flag in (True, False, None):
        sub = [r["signed"] for r in ag if r["matters"] is flag]
        _cell(f"agreed   when matters_this_week={flag}", sub)
    a_t = np.array([r["signed"] for r in ov if r["matters"] is True])
    a_f = np.array([r["signed"] for r in ov if r["matters"] is False])
    if a_t.size > 1 and a_f.size > 1:
        print(f"    -> overrides when it MATTERS vs when it does not: p = {perm_p(a_t, a_f)}")

    # ── H3 board confidence ──
    print("\nH3  BOARD CONFIDENCE ON THE OVERRIDE")
    conf = [(r["board_conf"], r["signed"]) for r in ov
            if isinstance(r["board_conf"], (int, float))]
    if len(conf) >= 20:
        vals = sorted(conf)
        mid = len(vals) // 2
        lo = np.array([v for _, v in vals[:mid]])
        hi = np.array([v for _, v in vals[mid:]])
        print(f"    low-confidence half  n={lo.size:3d} mean={lo.mean():+.2f}%")
        print(f"    high-confidence half n={hi.size:3d} mean={hi.mean():+.2f}%")
        print(f"    -> p = {perm_p(hi, lo)}  "
              f"({'confidence is informative' if (p := perm_p(hi, lo)) and p < 0.05 else 'confidence is NOT informative'})")
    else:
        print(f"    insufficient confidence values (n={len(conf)})")

    # ── H4 which action ──
    print("\nH4  BY BOARD ACTION")
    for act in ("BUY", "SELL", "HOLD"):
        _cell(f"overrode -> {act}", [r["signed"] for r in ov if r["action"] == act])
        _cell(f"agreed   -> {act}", [r["signed"] for r in ag if r["action"] == act])

    print("\n" + "=" * 96)
    print("Read the slice with the most negative mean AND a usable n — that is where")
    print("a gate belongs. A gate on the whole override population would also suppress")
    print("the board's legitimate disagreements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
