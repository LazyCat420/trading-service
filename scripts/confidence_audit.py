#!/usr/bin/env python3
"""Is `confidence` worth what the floor charges for it?

The confidence floor (ANALYSIS_CONFIDENCE_THRESHOLD, default 70) is the single
most active gate in the system, and 34% of all decisions land at 60-69 — just
underneath it. This script answers three questions that were being conflated.

────────────────────────────────────────────────────────────────────────────
Q1. DOES CONFIDENCE PREDICT RETURNS?  (--horizons)

Two measures of this disagreed, and the disagreement is a HORIZON EFFECT.

`decision_outcomes` resolves on a ~43-day average lag and shows a large
advantage for conf>=70:

    2026-05   conf<70 -5.17%  conf>=70 +1.10%   advantage +6.27  (n=1115)
    2026-06   conf<70 -0.69%  conf>=70 +4.07%   advantage +4.76  (n= 843)
    2026-07   conf<70 +0.76%  conf>=70 +0.05%   advantage -0.71  (n= 113)

July looks inverted, but its rows resolved in 6.8 days on average versus
~45 elsewhere — a fast-resolver subset, not a comparable population.

Scoring the SAME decisions directly off price_history, confidence gives
essentially nothing at every horizon short enough to measure:

    1d  -0.09   5d  +0.02   10d  -0.84   20d  -1.30   (advantage, pp)

CONCLUSION: confidence discriminates at ~6 weeks, not at ~2. An earlier read of
this repo's data concluded the 65-69 band had the BEST returns and the floor was
therefore costing money — that read was taken at a 5-day horizon, which is a
horizon at which nothing discriminates. **Do not lower the floor on short-horizon
evidence.** Both populations are ~98% counterfactual (2107 resolved outcomes vs
45 actual fills), so this is not an exit-management artifact either.

────────────────────────────────────────────────────────────────────────────
Q2. DOES THE SYNTHESIZER'S DOWNGRADE ADD INFORMATION?  (--synth)

The decision synthesizer lowers the board's confidence more often than it
raises it (269 vs 164, mean -1.80), and 95 of 600 decisions crossed from
board>=70 to synth<70 — blocked by the downgrade rather than by the board.

That looks like pure loss until you score it. Among decisions the BOARD rated
>=70 (h=10):

    synth KEPT >=70    n=99  mean -0.34%  win 52%
    synth CUT  to <70  n=40  mean -1.61%  win 32%

The trades it cut were materially worse. The downgrade is a real filter, not
noise — do NOT "fix" the synthesizer by removing it.

────────────────────────────────────────────────────────────────────────────
Q3. IS CONFIDENCE CALIBRATED?  (--calibrate)

Ranking and calibration are different properties (see
scripts/score_tournament_ranker.py — the tournament ranks at AUC 0.608 while
scoring Brier 0.3090, worse than a coin flip). Confidence is used as a
THRESHOLD, so its absolute value matters and miscalibration is expensive.

This fits an isotonic map from stated confidence to empirical win rate, fit on
the first half of the sample and scored OUT OF SAMPLE on the second. Isotonic is
monotone by construction, so it can only recalibrate the scale — it can never
reorder decisions, and a genuinely uninformative confidence produces a flat map
rather than a fake edge. Reported, never auto-applied: writing a calibration
map into the trading path is a separate, gated decision.

Usage:
    python scripts/confidence_audit.py --all
    python scripts/confidence_audit.py --calibrate --horizon 10
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Fewer than this on either side of a split and no verdict is issued.
MIN_PER_SIDE = 30

_PX: dict[tuple, float | None] = {}


def _prime(rows: list[tuple], horizon: int) -> None:
    """Resolve forward returns once via MongoDB."""
    from app.db import mongo_store
    from app.quant.returns import _one_vendor

    need = {(r[0], str(r[1])[:10]) for r in rows}
    for ticker, day in need:
        key = (ticker, day, horizon)
        if key in _PX:
            continue
        try:
            e_docs = mongo_store.find_docs(
                "price_history",
                _one_vendor(ticker, {"ticker": ticker, "date": {"$lte": day}}),
                sort=[("date", -1)],
                projection={"close": 1},
                limit=1,
            )
            x_docs = mongo_store.find_docs(
                "price_history",
                _one_vendor(ticker, {"ticker": ticker, "date": {"$gt": day}}),
                sort=[("date", 1)],
                projection={"close": 1},
                limit=max(horizon, 1),
            )
            if not e_docs or len(x_docs) < max(horizon, 1):
                _PX[key] = None
                continue
            e_close = float(e_docs[0].get("close") or 0)
            x_close = float(x_docs[-1].get("close") or 0)
            if e_close > 0:
                _PX[key] = 100.0 * (x_close - e_close) / e_close
            else:
                _PX[key] = None
        except Exception:
            _PX[key] = None


def _signed(ticker, day, horizon: int, action: str) -> float | None:
    """Forward return signed by trade direction — a SELL profits when price falls."""
    v = _PX.get((ticker, str(day)[:10], horizon))
    if v is None:
        return None
    return v if str(action).upper() == "BUY" else -v


def load_decisions(since: str) -> list[tuple]:
    from app.db import mongo_query

    return mongo_query.find_rows(
        "trade_results",
        {
            "created_at": {"$gte": since},
            "action": {"$in": ["BUY", "SELL"]},
            "confidence": {"$ne": None},
        },
        ["ticker", "created_at", "action", "confidence"],
    )


def q1_horizons(since: str) -> dict:
    rows = load_decisions(since)
    print(f"═══ Q1: does confidence predict returns? (n={len(rows)} decisions) ═══\n")
    print("  horizon   n(<70)  mean(<70)   n(>=70)  mean(>=70)   advantage")
    out = {}
    for h in (1, 5, 10, 20, 30):
        _prime(rows, h)
        lo = [s for t, d, a, c in rows if c < 70 and (s := _signed(t, d, h, a)) is not None]
        hi = [s for t, d, a, c in rows if c >= 70 and (s := _signed(t, d, h, a)) is not None]
        if not (lo and hi):
            continue
        adv = statistics.mean(hi) - statistics.mean(lo)
        flag = "" if min(len(lo), len(hi)) >= MIN_PER_SIDE else "  [n too small]"
        print(f"  {h:3d}d     {len(lo):5d}  {statistics.mean(lo):+8.2f}%  "
              f"{len(hi):5d}  {statistics.mean(hi):+8.2f}%   {adv:+6.2f}{flag}")
        out[h] = {"n_lo": len(lo), "n_hi": len(hi), "advantage": round(adv, 3)}
    print("\n  Confidence is a ~6-week signal, not a ~2-week one. See the module")
    print("  docstring: decision_outcomes resolves at ~43d and shows +4.8 to +6.3.")
    return out


def q2_synth(since: str, horizon: int) -> dict:
    import json as _json

    from app.db import mongo_query

    desks = mongo_query.find_rows(
        "shared_desk",
        {"created_at": {"$gte": since}},
        ["ticker", "created_at", "desk_data"],
    )

    parsed = []
    for tk, day, d in desks:
        if isinstance(d, str):
            try:
                d = _json.loads(d)
            except Exception:
                continue
        fd, td = d.get("final_decision") or {}, d.get("trade_decision") or {}
        bc, sc = fd.get("confidence"), td.get("confidence")
        act = str(td.get("action") or "").upper()
        if act in ("BUY", "SELL") and isinstance(bc, (int, float)) and isinstance(sc, (int, float)):
            parsed.append((tk, day, act, bc, sc))

    _prime([(p[0], p[1]) for p in parsed], horizon)
    kept, cut = [], []
    for tk, day, act, bc, sc in parsed:
        if bc < 70:
            continue  # only decisions the BOARD was confident about
        s = _signed(tk, day, horizon, act)
        if s is not None:
            (cut if sc < 70 else kept).append(s)

    print(f"\n═══ Q2: does the synthesizer's downgrade add information? (h={horizon}) ═══\n")
    print("  Among decisions the BOARD rated >= 70:")
    for lbl, v in (("synth KEPT >=70", kept), ("synth CUT to <70", cut)):
        if v:
            print(f"    {lbl:18} n={len(v):4d}  mean={statistics.mean(v):+6.2f}%  "
                  f"win={100 * sum(1 for x in v if x > 0) // len(v)}%")
    out = {"n_kept": len(kept), "n_cut": len(cut)}
    if kept and cut:
        delta = statistics.mean(cut) - statistics.mean(kept)
        out["delta"] = round(delta, 3)
        print(f"\n    downgraded minus kept: {delta:+.2f}pp "
              f"({'downgrade ADDS information' if delta < 0 else 'downgrade is NOT selective'})")
        try:
            from scipy.stats import fisher_exact, mannwhitneyu
            u, p = mannwhitneyu(kept, cut, alternative="greater")
            tab = [[sum(1 for x in kept if x > 0), sum(1 for x in kept if x <= 0)],
                   [sum(1 for x in cut if x > 0), sum(1 for x in cut if x <= 0)]]
            orr, pf = fisher_exact(tab)
            print(f"    Mann-Whitney p={p:.4f}   win-rate Fisher OR={orr:.2f} p={pf:.4f}")
            out |= {"mw_p": round(float(p), 5), "fisher_or": round(float(orr), 3),
                    "fisher_p": round(float(pf), 5)}
        except ImportError:
            pass
        print("\n    Do NOT remove the synthesizer downgrade to unblock trades —")
        print("    it is selecting against the worse half of the board's own calls.")
    return out


def q3_calibrate(since: str, horizon: int) -> dict:
    """Isotonic map: stated confidence -> empirical win rate, scored OUT OF SAMPLE."""
    rows = load_decisions(since)
    _prime(rows, horizon)
    data = sorted(
        ((c, 1 if s > 0 else 0, d)
         for t, d, a, c in rows if (s := _signed(t, d, horizon, a)) is not None),
        key=lambda z: z[2],
    )
    print(f"\n═══ Q3: is confidence calibrated? (h={horizon}, n={len(data)}) ═══\n")
    if len(data) < 2 * MIN_PER_SIDE:
        print(f"  n={len(data)} — need >= {2 * MIN_PER_SIDE} to fit and validate. Skipping.")
        return {"n": len(data), "fitted": False}

    split = len(data) // 2
    tr, te = data[:split], data[split:]
    try:
        import numpy as np
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        print("  sklearn/numpy unavailable — skipping.")
        return {"n": len(data), "fitted": False}

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(np.array([r[0] for r in tr], dtype=float),
            np.array([r[1] for r in tr], dtype=float))

    base = float(np.mean([r[1] for r in tr]))
    yte = np.array([r[1] for r in te], dtype=float)
    pred = iso.predict(np.array([r[0] for r in te], dtype=float))
    raw = np.clip(np.array([r[0] for r in te], dtype=float) / 100.0, 0, 1)

    brier = lambda p: float(np.mean((p - yte) ** 2))  # noqa: E731
    b_raw, b_cal, b_base = brier(raw), brier(pred), brier(np.full_like(yte, base))
    print(f"  train n={len(tr)}  test n={len(te)}  base win rate={base:.3f}\n")
    print(f"    Brier, confidence used raw (conf/100) : {b_raw:.4f}")
    print(f"    Brier, isotonic-recalibrated          : {b_cal:.4f}")
    print(f"    Brier, base rate (knows nothing)      : {b_base:.4f}")

    print("\n  Fitted map (stated confidence -> empirical win rate):")
    for c in (50, 55, 60, 65, 70, 75, 80, 85, 90):
        print(f"    {c:3d} -> {float(iso.predict([float(c)])[0]):.3f}")

    spread = float(iso.predict([90.0])[0] - iso.predict([50.0])[0])
    print(f"\n  Map spread across 50->90: {spread:+.3f}")
    if abs(spread) < 0.05:
        print("  FLAT MAP: confidence carries almost no win-rate information at this")
        print("  horizon. Recalibration cannot manufacture an edge that is not there —")
        print("  the fix is a better confidence signal, not a better scale.")
    elif b_cal < min(b_raw, b_base):
        print("  Recalibration beats BOTH the raw scale and the base rate out of sample.")
        print("  Worth wiring in — behind a parameter, measured against the live floor.")
    else:
        print("  Recalibration does not beat the base rate out of sample. Do not ship it.")
    return {"n": len(data), "fitted": True, "brier_raw": round(b_raw, 5),
            "brier_calibrated": round(b_cal, 5), "brier_base": round(b_base, 5),
            "spread": round(spread, 4)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-06-01")
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--horizons", action="store_true")
    ap.add_argument("--synth", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()
    if not (args.horizons or args.synth or args.calibrate):
        args.all = True

    out = {"since": args.since, "horizon": args.horizon}
    if args.all or args.horizons:
        out["q1_horizons"] = q1_horizons(args.since)
    if args.all or args.synth:
        out["q2_synthesizer"] = q2_synth(args.since, args.horizon)
    if args.all or args.calibrate:
        out["q3_calibration"] = q3_calibrate(args.since, args.horizon)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(out, f, indent=2, default=str)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
