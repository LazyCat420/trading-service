#!/usr/bin/env python3
"""Where in the debate chain do the numbers decay? One grounding rate per hop.

Phase A (2026-08-23, ch.91) stamps a measure-only `_grounding_shadow` onto
every bull_argument / bear_rebuttal / bull_defense / debate_judge artifact:
which of the artifact's claimed metric values match the desk's verified data.
Until this report, NOTHING read that field — it accumulated in production for
three days with no aggregator (found 2026-08-26; first read measured
bull 96.4% → bear 81.0% → defense 76.2% → judge 86.0% grounded, with a
sign-flipped operating margin propagating uncorrected through three hops).

    python3 scripts/grounding_decay_report.py --since 2026-08-23
    python3 scripts/grounding_decay_report.py --since 2026-08-23 --json out.json

Read-only. Safe against a live cycle.

BASELINE: 85.6% grounded (ch.90 attack2, pre-fix, replay-derived and
baseline-drift-inflated — treat as a floor, not a target). A hop below the
floor, or a curve that worsens hop-to-hop, names the next fix's target.
Cross-check: scratch/decision-decay-audit-2026-08-23/attack2-grounding.py is
an independent implementation of the same question from raw desks.
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

#: Consecutive hops, in speaking order. The decay curve is read left to right.
HOPS = ("bull_argument", "bear_rebuttal", "bull_defense", "debate_judge")
_SHORT = {"bull_argument": "bull", "bear_rebuttal": "bear",
          "bull_defense": "defense", "debate_judge": "judge"}
BASELINE_GROUNDED_PCT = 85.6


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


def collect(since: str, until: str | None, include_synthetic: bool) -> dict:
    win: dict = {"created_at": {"$gte": since}}
    if until:
        win["created_at"]["$lt"] = until

    per_hop = {h: Counter() for h in HOPS}          # checked/mismatched/unverifiable/artifacts
    per_day = defaultdict(lambda: {h: Counter() for h in HOPS})
    worst_metrics: Counter = Counter()
    worst_examples: list[dict] = []
    desks = 0

    for d in mongo_query.find_dicts(
        "shared_desk", win,
        ["cycle_id", "ticker", "created_at", "desk_data"],
    ):
        if not include_synthetic and not _is_production_cycle(d.get("cycle_id")):
            continue
        dd = _as_dict(d.get("desk_data"))
        if not dd:
            continue
        desks += 1
        day = str(d.get("created_at"))[:10]
        for hop in HOPS:
            art = dd.get(hop)
            if not isinstance(art, dict):
                continue
            g = art.get("_grounding_shadow")
            if not isinstance(g, dict):
                continue
            for agg in (per_hop[hop], per_day[day][hop]):
                agg["checked"] += int(g.get("checked") or 0)
                agg["mismatched"] += int(g.get("mismatched") or 0)
                agg["unverifiable"] += int(g.get("unverifiable") or 0)
                agg["artifacts"] += 1
            for w in g.get("worst") or []:
                if isinstance(w, dict) and w.get("metric"):
                    worst_metrics[str(w["metric"])] += 1
                    if len(worst_examples) < 20:
                        worst_examples.append(
                            {"ticker": d.get("ticker"), "hop": hop, **{
                                k: w.get(k) for k in ("metric", "claimed", "verified")}}
                        )

    return {
        "desks": desks,
        "per_hop": {h: dict(c) for h, c in per_hop.items()},
        "per_day": {day: {h: dict(c) for h, c in hops.items() if c}
                    for day, hops in sorted(per_day.items())},
        "worst_metrics": worst_metrics.most_common(10),
        "worst_examples": worst_examples,
    }


def grounded_pct(c: dict) -> float | None:
    checked = c.get("checked", 0)
    if not checked:
        return None
    return (checked - c.get("mismatched", 0)) / checked * 100


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="2026-08-23", help="ISO date, inclusive (default: Phase A boundary)")
    ap.add_argument("--until", default=None, help="ISO date, exclusive")
    ap.add_argument("--include-synthetic", action="store_true")
    ap.add_argument("--json", dest="json_out", default=None)
    a = ap.parse_args()

    data = collect(a.since, a.until, a.include_synthetic)

    # Vacuity guard: an empty result is the absence of evidence, not health.
    total_artifacts = sum(c.get("artifacts", 0) for c in data["per_hop"].values())
    if not total_artifacts:
        print(f"NO SHADOWED ARTIFACTS in [{a.since}, {a.until or 'now'}) — this "
              f"report proved nothing. Either the window predates Phase A "
              f"(2026-08-23), no debates ran, or attach_grounding_shadow "
              f"regressed. Check the newest desks' desk_data before trusting "
              f"any other number here.")
        return 1

    print(f"Grounding decay, [{a.since} .. {a.until or 'now'}), "
          f"{data['desks']} production desks"
          f"{' (+synthetic)' if a.include_synthetic else ''}\n")
    print(f"{'hop':16} {'artifacts':>9} {'checked':>8} {'mismatch':>8} "
          f"{'unverif':>8} {'grounded':>9}  vs {BASELINE_GROUNDED_PCT}% floor")
    verdicts = {}
    for hop in HOPS:
        c = data["per_hop"][hop]
        pct = grounded_pct(c)
        if pct is None:
            print(f"{hop:16} {c.get('artifacts', 0):>9} — no checked claims")
            continue
        verdict = "OK" if pct >= BASELINE_GROUNDED_PCT else "BELOW FLOOR"
        verdicts[hop] = verdict
        print(f"{hop:16} {c['artifacts']:>9} {c['checked']:>8} "
              f"{c['mismatched']:>8} {c['unverifiable']:>8} {pct:>8.1f}%  {verdict}")

    print("\nper day (grounded % per hop):")
    for day, hops in data["per_day"].items():
        cells = "  ".join(
            f"{_SHORT[h]}={grounded_pct(c):.0f}%" if grounded_pct(c) is not None else f"{_SHORT[h]}=—"
            for h, c in hops.items()
        )
        print(f"  {day}: {cells}")

    print("\nmost-mismatched metrics:", ", ".join(f"{m}×{n}" for m, n in data["worst_metrics"]))
    print("sample mismatches (claimed vs verified):")
    for e in data["worst_examples"][:8]:
        print(f"  {e['ticker']:6} {e['hop']:14} {e['metric']}: {e['claimed']} vs {e['verified']}")

    if a.json_out:
        Path(a.json_out).write_text(json.dumps(data, indent=2, default=str))
        print(f"\nwrote {a.json_out}")

    return 0 if all(v == "OK" for v in verdicts.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
