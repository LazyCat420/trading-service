#!/usr/bin/env python3
"""What the skill loop believes about each agent, and why.

The loop's decisions were previously invisible: `agent_skills` records a version
and a score that measured prose, and nothing showed whether a version had earned
its place. This prints the measured state — sample count against the maturity
bar, the two outcome components, whether the window was contaminated by broken
tools, and the resulting verdict.

    python scripts/skill_scorecard.py                 # active version per agent
    python scripts/skill_scorecard.py --history       # every version, per agent
    python scripts/skill_scorecard.py --agent v3_bear_agent --history
    python scripts/skill_scorecard.py --json

A verdict of UNCOVERED or IMMATURE is information, not an error: it says the
loop cannot yet judge that agent. Saying so is the point — the previous gate
always had an opinion and none of them were grounded.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import mongo_query                                       # noqa: E402
from app.autoresearch.scorecard import (                            # noqa: E402
    MATURITY_N, REGRESSION_MARGIN, VERDICT_CONTAMINATED, VERDICT_IMMATURE,
    VERDICT_REGRESSED, build_scorecard, regression_verdict,
)
from app.autoresearch.skill_optimizer import TARGET_AGENTS          # noqa: E402

_MARK = {
    VERDICT_REGRESSED: "!!",
    VERDICT_CONTAMINATED: "~~",
    VERDICT_IMMATURE: "..",
}


def _versions(agent: str, history: bool) -> list[int]:
    """The version numbers to score, oldest first.

    Reads `agent_skills` in Mongo. `app.autoresearch.scorecard` — which this
    script is a viewer for — already reads the same collection through
    `mongo_query`, so both halves of the report now come from one store.

    ASCENDING, and `main()` depends on it: it applies `regression_verdict` to
    `versions[-1]` and `build_scorecard` to the rest, so a descending list
    would run the regression test against the OLDEST version and score the
    newest as history. `version` is a plain int on all 162 documents, so the
    sort is numeric — not the string ordering that would put v10 before v2.
    """
    if history:
        rows = mongo_query.find_rows(
            "agent_skills", {"agent_name": agent}, ["version"],
            sort=[("version", 1)])
        return [r[0] for r in rows]
    # `status = 'active'`. The SQL took whichever row the heap handed back
    # first; Mongo's natural order is a different arbitrary order, so
    # "faithful" here means picking deterministically rather than picking
    # blind. Highest version wins, which is what the two app-side readers of
    # this same question already do (`skill_loader._load` and
    # `skill_optimizer._load_skill`, both ported from
    # `... status = 'active' ORDER BY version DESC LIMIT 1`) — and
    # `skill_optimizer._save_skill` archives-then-inserts as two un-transacted
    # Mongo ops, so a second active row is a state this can land in. Verified a no-op on today's
    # data: all 7 agents have exactly one active row, so the output is
    # unchanged either way.
    row = mongo_query.find_row(
        "agent_skills", {"agent_name": agent, "status": "active"}, ["version"],
        sort=[("version", -1)])
    return [row[0]] if row else []


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--agent", help="limit to one agent name")
    p.add_argument("--history", action="store_true",
                   help="every version, not just the active one")
    p.add_argument("--json", action="store_true", dest="as_json")
    args = p.parse_args()

    agents = [args.agent] if args.agent else list(TARGET_AGENTS)
    out: list[dict] = []

    for agent in agents:
        versions = _versions(agent, args.history)
        if not versions:
            out.append({"agent_name": agent, "version": None,
                        "verdict": "UNCOVERED", "detail": "no skill doc yet"})
            continue
        for v in versions:
            card = (regression_verdict(agent, v) if v == versions[-1]
                    else build_scorecard(agent, v))
            out.append(card.to_dict())

    if args.as_json:
        print(json.dumps(out, indent=2, default=str))
        return 0

    print(f"maturity bar: n={MATURITY_N} resolved decisions   "
          f"regression margin: ±{REGRESSION_MARGIN:.3f} (95% noise band)\n")
    print(f"{'':2} {'agent':<26}{'ver':>4}{'n':>7}{'score':>8}"
          f"{'dir':>8}{'hold':>8}{'incompl':>9}  verdict")
    print("-" * 96)
    for c in out:
        mark = _MARK.get(c["verdict"], "  ")
        d, h = c.get("directional") or {}, c.get("hold") or {}
        fmt = lambda x: f"{x:.3f}" if isinstance(x, (int, float)) else "—"  # noqa: E731
        pct = (f"{c['incomplete_rate']:.0%}"
               if c.get("incomplete_rate") is not None else "—")
        print(f"{mark} {c['agent_name']:<26}{str(c.get('version') or '—'):>4}"
              f"{c.get('n_governed', 0):>7}{fmt(c.get('combined')):>8}"
              f"{fmt(d.get('score')):>8}{fmt(h.get('score')):>8}{pct:>9}  "
              f"{c['verdict']}")

    detailed = [c for c in out if c["verdict"] in
                (VERDICT_REGRESSED, VERDICT_CONTAMINATED)]
    if detailed:
        print()
        for c in detailed:
            print(f"  {c['agent_name']} v{c.get('version')}: {c.get('detail', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
