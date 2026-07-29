#!/usr/bin/env python3
"""Brier-score the probabilistic panel against the baselines that matter.

The old tournament could not be scored: it emitted ``bull``/``bear``/``split``
and a confidence of ``avg_jury_score * 10``. A 3-value label cannot be
Brier-scored or calibration-checked, which is why 31% of pipeline spend ran for
months without anyone being able to say whether it helped.

**The bar this must clear is NOT the old tournament.** The tournament is already
known to be noise (t=-0.17 over n=124), so beating it proves nothing. The
baselines, in ascending order of honesty:

  1. constant 0.5        Brier 0.25 by construction. Table stakes.
  2. base rate p̄         Brier p̄(1-p̄) on the same rows. **The real null** — a
                         forecaster that knows only "stocks usually drift up"
                         and nothing about this ticker.
  3. self-consistency    The same model, the FULL packet, k independent samples,
                         p = fraction bullish. No debate, no partition. The
                         literature's central finding is that most debate
                         systems lose to this at 2-3x the tokens. **This is the
                         one that decides whether the panel ships.**
  4. rho=1.0 control     The panel itself with shared_evidence=True. If the
                         panel beats self-consistency but NOT this, the gain is
                         from ensembling, not from information asymmetry.

Report Murphy's decomposition, not just the total:

    Brier = reliability - resolution + uncertainty

**Resolution is the number that matters.** This system's standing finding is
that it can identify its own bad decisions but cannot pick winners — i.e. it
has reliability and no resolution. A panel that only improves reliability has
bought nothing, and a headline Brier would hide that.

Usage:
    python scripts/score_panel.py --since 2026-07-01
    python scripts/score_panel.py --since 2026-07-01 --horizon 7 --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cognition.debate.panel_math import (  # noqa: E402
    brier_decomposition, brier_score, clamp_probability,
)

#: Matches outcome_tracker's deadband. A move inside +-1% is neither a win nor a
#: loss, and scoring flats as y=0 would reward permanent bearishness.
DEADBAND_PCT = 1.0

#: Below this, the noise band is wider than any effect worth acting on: the
#: repo's own bootstrap puts n=25 at +-0.207 and n=100 at +-0.104.
MIN_N = 100


def _outcome(move_pct: float) -> int | None:
    """+-1% deadband. None means EXCLUDE, not zero."""
    if move_pct > DEADBAND_PCT:
        return 1
    if move_pct < -DEADBAND_PCT:
        return 0
    return None


def _probability_from_artifact(art: dict) -> float | None:
    """Read a forecast from a debate artifact, new engine or old.

    The panel writes ``probability`` directly. The tournament never had one, so
    it is reconstructed from (winning_side, confidence) — deliberately, so the
    comparison is like-for-like rather than "the thing with a probability wins
    because it has one".
    """
    if not isinstance(art, dict):
        return None

    if isinstance(art.get("probability"), (int, float)):
        return clamp_probability(art["probability"])

    side = str(art.get("winning_side") or "").lower()
    conf = art.get("confidence")
    if not isinstance(conf, (int, float)):
        return None
    # confidence is distance-from-neutral in the old artifact too.
    delta = max(0.0, min(0.5, float(conf) / 200.0))
    if side == "bull":
        return clamp_probability(0.5 + delta)
    if side == "bear":
        return clamp_probability(0.5 - delta)
    if side in ("split", "veto", "skipped", "fallback"):
        return 0.5
    return None


def collect(since: str, horizon: int) -> dict[str, list[tuple[float, int]]]:
    """Gather (probability, outcome) pairs per engine.

    Reuses ``agent_scorecard.fetch_rows_from_prices`` rather than
    ``decision_outcomes``: it scores every desk straight off price_history
    (10-16x the sample, immediately, and it includes HOLD desks that never get
    an outcome row).
    """
    from scripts.agent_scorecard import fetch_rows_from_prices

    rows = fetch_rows_from_prices(since, horizon=horizon)
    buckets: dict[str, list[tuple[float, int]]] = defaultdict(list)
    voided = 0

    for row in rows:
        # fetch_rows_from_prices returns the parsed desk under "desk".
        desk = row.get("desk") or row.get("desk_data") or {}
        if isinstance(desk, str):
            try:
                desk = json.loads(desk)
            except ValueError:
                continue
        if not isinstance(desk, dict):
            continue

        move = row.get("move_pct")
        if move is None:
            continue
        y = _outcome(float(move))
        if y is None:      # inside the deadband
            continue

        art = desk.get("tournament_result")
        if not isinstance(art, dict):
            continue

        engine = art.get("engine") or "tournament"

        # A panel run whose evidence partition silently collapsed is N agents
        # reading one packet — the state that makes debate a martingale. Void
        # it rather than averaging it in and calling the result a panel.
        if engine == "probabilistic_panel" and art.get("partitioned") is False \
                and not art.get("shared_evidence_control"):
            voided += 1
            continue
        if art.get("degraded"):
            voided += 1
            continue

        if art.get("shared_evidence_control"):
            engine = "panel_rho1_control"

        p = _probability_from_artifact(art)
        if p is None:
            continue
        buckets[engine].append((p, y))

    if voided:
        print(f"  (voided {voided} runs: collapsed partition or degraded)")
    return buckets


def _report(label: str, pairs: list[tuple[float, int]]) -> dict:
    d = brier_decomposition(pairs)
    if not d["n"]:
        print(f"  {label:22} n=0")
        return d
    flag = "" if d["n"] >= MIN_N else f"  [n<{MIN_N}: inside the noise band]"
    print(f"  {label:22} n={d['n']:4}  Brier={d['brier']:.4f}  "
          f"rel={d['reliability']:.4f}  RES={d['resolution']:.4f}  "
          f"base={d['base_rate']:.3f}{flag}")
    return d


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="YYYY-MM-DD")
    ap.add_argument("--horizon", type=int, default=7, help="forward sessions")
    ap.add_argument("--json", help="write the full report here")
    args = ap.parse_args()

    print(f"\nPanel scoring — desks since {args.since}, +{args.horizon} sessions, "
          f"±{DEADBAND_PCT}% deadband\n")

    buckets = collect(args.since, args.horizon)
    if not buckets:
        print("  no scoreable rows. Has a panel or tournament run since --since?")
        return 1

    report: dict = {"since": args.since, "horizon": args.horizon, "engines": {}}

    print("\nENGINES (higher RES = actually discriminating; lower Brier = better)")
    for engine, pairs in sorted(buckets.items()):
        report["engines"][engine] = _report(engine, pairs)

    # Baselines computed on the union of scored rows so every comparison sits on
    # the same population.
    all_pairs = [pr for pairs in buckets.values() for pr in pairs]
    print("\nBASELINES (same rows)")
    n = len(all_pairs)
    base = sum(y for _, y in all_pairs) / n if n else 0.0
    report["baselines"] = {
        "constant_half": brier_score([(0.5, y) for _, y in all_pairs]),
        "base_rate": brier_score([(base, y) for _, y in all_pairs]),
        "base_rate_value": round(base, 4),
    }
    print(f"  {'constant 0.5':22} n={n:4}  Brier="
          f"{report['baselines']['constant_half']:.4f}   (table stakes)")
    print(f"  {'base rate p̄':22} n={n:4}  Brier="
          f"{report['baselines']['base_rate']:.4f}   p̄={base:.3f}  <- THE NULL")

    print("\nVERDICT")
    panel = report["engines"].get("probabilistic_panel")
    if not panel or not panel["n"]:
        print("  No panel rows yet — run a cycle with the panel enabled.")
    else:
        null = report["baselines"]["base_rate"]
        beats_null = panel["brier"] is not None and panel["brier"] < null
        print(f"  panel vs the base-rate null: "
              f"{'BEATS' if beats_null else 'does NOT beat'} it "
              f"({panel['brier']:.4f} vs {null:.4f})")
        if panel["resolution"] is not None and panel["resolution"] < 0.005:
            print("  WARNING: resolution ~0 — the panel is calibrated but not "
                  "discriminating. That is the failure this system already has; "
                  "a better Brier from reliability alone buys nothing.")
        if panel["n"] < MIN_N:
            print(f"  n={panel['n']} is below {MIN_N}. Not actionable yet — the "
                  "noise band is wider than any plausible effect.")
        ctrl = report["engines"].get("panel_rho1_control")
        if ctrl and ctrl["n"] and panel["brier"] is not None:
            better = panel["brier"] < ctrl["brier"]
            print(f"  vs the rho=1.0 (asymmetry-off) control: "
                  f"{'asymmetry helps' if better else 'NO evidence asymmetry helps'} "
                  f"({panel['brier']:.4f} vs {ctrl['brier']:.4f})")
        else:
            print("  rho=1.0 control has no rows — without it, a panel win could "
                  "be ensembling rather than information asymmetry.")
    print("\n  Self-consistency baseline is NOT in this report: it needs its own "
          "run (same model, full packet, k samples). It is the bar that decides "
          "whether the panel ships.\n")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
