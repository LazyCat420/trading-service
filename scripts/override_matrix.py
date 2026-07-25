#!/usr/bin/env python3
"""Does overriding an upstream agent PAY? With error bars this time.

The 2026-07-25 scorecard reported that the board overriding the fundamental
desk earned -2.38% edge at 18% hit — and that every handoff except the
synthesizer's was negative. That was n=34, no confidence interval, and it is
exactly the shape of finding that has already been retracted once on this
codebase. This script re-runs it at ~10x the sample and attaches a test.

    python scripts/override_matrix.py --min-held-known

## Why `--min-held-known` matters

Executability depends on whether the ticker was HELD at decision time. Only
**357 of 1192** desks carry an explicit `cycle_metadata.held` flag, and it is
NOT reconstructible: `trade_fills` has 44 rows and `positions` holds only the
8 current rows, with no history table covering the gap.

Guessing `held` for the other 835 is precisely the error that produced the
retracted "decision layer destroys value" headline — a policy-blocked SELL on
an unheld name scored as a real loss. So `--min-held-known` restricts to desks
where the flag is present and honest. 357 still beats 34 by 10x.

Without the flag, a SELL cannot be classified, so the default is to report
BOTH populations and let the difference speak.

## The test

Overrides vs agreements is a two-sample comparison of mean signed return.
Welch's t (unequal variances, unequal n) plus a permutation test, because the
return distribution is fat-tailed and n per cell can be small. A difference
that survives neither is a lead, not a finding.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEADBAND_PCT = 1.0
PERMUTATIONS = 20_000

PAIRS = [
    ("tournament_result", "final_decision", "board overrides debate"),
    ("quant_report", "final_decision", "board overrides quant"),
    ("fundamental_report", "final_decision", "board overrides fundamental"),
    ("final_decision", "trade_decision", "synthesizer overrides board"),
]


def welch_t(a: np.ndarray, b: np.ndarray) -> tuple[float | None, float | None]:
    """Welch's t and approximate two-sided p (normal tail; n here is large
    enough that the t/normal difference is immaterial next to the fat tails)."""
    if a.size < 2 or b.size < 2:
        return None, None
    va, vb = a.var(ddof=1), b.var(ddof=1)
    se = np.sqrt(va / a.size + vb / b.size)
    if se <= 0:
        return None, None
    t = (a.mean() - b.mean()) / se
    from math import erfc, sqrt
    return float(t), float(erfc(abs(t) / sqrt(2)))


def permutation_p(a: np.ndarray, b: np.ndarray, seed: int = 20260725) -> float | None:
    """Distribution-free two-sided p for the difference in means."""
    if a.size < 2 or b.size < 2:
        return None
    rng = np.random.default_rng(seed)
    pooled = np.concatenate([a, b])
    observed = abs(a.mean() - b.mean())
    n_a = a.size
    count = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(pooled)
        if abs(pooled[:n_a].mean() - pooled[n_a:].mean()) >= observed:
            count += 1
    return (count + 1) / (PERMUTATIONS + 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-05-01")
    ap.add_argument("--horizon", type=int, default=7)
    ap.add_argument("--min-held-known", action="store_true",
                    help="Only desks with an explicit cycle_metadata.held flag "
                         "(357 of 1192). Prevents the mis-bucketing that caused "
                         "the retracted 'decision layer destroys value' headline.")
    ap.add_argument("--executable-only", action="store_true")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    from scripts.agent_scorecard import fetch_rows_from_prices, _stance, classify_executability

    rows = fetch_rows_from_prices(args.since, args.horizon)
    total = len(rows)

    if args.min_held_known:
        rows = [r for r in rows
                if isinstance(r["desk"].get("cycle_metadata"), dict)
                and "held" in r["desk"]["cycle_metadata"]
                and r["desk"]["cycle_metadata"]["held"] is not None]
    if args.executable_only:
        rows = [r for r in rows
                if classify_executability(r["desk"], r.get("action")) == "consequential"]

    print("=" * 100)
    print(f"OVERRIDE MATRIX — {len(rows)} of {total} desks since {args.since} "
          f"(+{args.horizon}d)"
          + ("  [held-flag known]" if args.min_held_known else "")
          + ("  [consequential only]" if args.executable_only else ""))
    print("=" * 100)
    if not rows:
        print("nothing to score"); return 1

    naive = float(np.mean([r["move_pct"] for r in rows]))
    print(f"BASELINE always-long over these desks: {naive:+.2f}%   (beat THIS, not zero)\n")

    out = {}
    for up_key, down_key, label in PAIRS:
        agreed, overrode = [], []
        for row in rows:
            up = _stance(row["desk"].get(up_key) or {})
            down = _stance(row["desk"].get(down_key) or {})
            if up is None or down is None:
                continue
            # Signed to the DOWNSTREAM agent's call: what following it earned.
            signed = down * row["move_pct"]
            (agreed if up == down else overrode).append(signed)

        a, o = np.array(agreed, dtype=float), np.array(overrode, dtype=float)
        if a.size == 0 and o.size == 0:
            continue
        t, p_t = welch_t(o, a) if (a.size > 1 and o.size > 1) else (None, None)
        p_perm = permutation_p(o, a) if (a.size > 1 and o.size > 1) else None

        def fmt(x, spec="+.2f"):
            return "—" if x is None else format(x, spec)

        print(f"{label}")
        print(f"   agreed  n={a.size:4d}  mean={fmt(a.mean() if a.size else None)}%"
              f"  sd={fmt(a.std(ddof=1) if a.size > 1 else None, '.2f')}")
        print(f"   overrode n={o.size:4d}  mean={fmt(o.mean() if o.size else None)}%"
              f"  sd={fmt(o.std(ddof=1) if o.size > 1 else None, '.2f')}")
        if t is not None:
            diff = o.mean() - a.mean()
            verdict = ("SIGNIFICANT" if (p_perm is not None and p_perm < 0.05)
                       else "not distinguishable from noise")
            print(f"   difference {diff:+.2f}%  Welch t={t:+.2f} p={p_t:.3f}"
                  f"  permutation p={p_perm:.4f}  -> {verdict}")
        else:
            print("   difference: insufficient n in one cell")
        print()

        out[label] = {
            "agreed_n": int(a.size),
            "agreed_mean": float(a.mean()) if a.size else None,
            "overrode_n": int(o.size),
            "overrode_mean": float(o.mean()) if o.size else None,
            "welch_t": t, "p_welch": p_t, "p_permutation": p_perm,
        }

    print("=" * 100)
    sig = [k for k, v in out.items()
           if v.get("p_permutation") is not None and v["p_permutation"] < 0.05]
    if sig:
        print("Statistically distinguishable handoffs:", ", ".join(sig))
        print("  These are candidates for a gate. Check the SIGN before acting:")
        print("  a negative override mean means the downstream agent should defer.")
    else:
        print("NO handoff difference survives a permutation test at this sample.")
        print("  The 2026-07-25 override figures (-2.38%, 18% hit, n=34) do NOT")
        print("  replicate as significant. Treat them as a lead, not a finding —")
        print("  and do NOT tune a gate on them.")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
