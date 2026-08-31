#!/usr/bin/env python3
"""Did a skill version trade better than the one before it?

The question SkillOpt's own score gate cannot answer. That gate measures whether
a doc is better *written* — specific, actionable, not bloated. This measures
whether decisions made under it were better, which is the only justification for
the loop's cost.

**Read this before quoting any number below.**

1. **Against the always-long baseline, never zero.** An agent long in a rising
   tape looks brilliant against zero. The baseline is printed on every row.
2. **n will be small for a long time.** At ~7 decisions/cycle and a 25-decision
   maturity threshold, a version governs ~25-60 decisions. Detecting a ~1%
   per-decision edge needs hundreds. Expect "not distinguishable from the prior
   version" to be the honest answer for months — and note that the repo's own
   residual-alpha work already found no detectable alpha in the pipeline at
   n=106 (t=-0.904). A confident-looking difference at n=30 is noise.
3. **Sequential comparison is confounded by regime.** Version 20 in a rising
   week beats version 19 in a falling one regardless of quality. The
   baseline-relative column is the only one worth reading, and even it does not
   fully control for this. A true answer needs an A/B: two bots, different
   versions, same tickers, same cycles.

Reads MongoDB (`decision_outcomes`). Postgres is a frozen archive whose last
`decision_outcomes` row was written 2026-08-19 22:56:58; a scorecard served from
it would answer for a fleet that stopped trading in August.

Usage:
    python scripts/skill_version_scorecard.py
    python scripts/skill_version_scorecard.py --agent v3_board_of_directors
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import mongo_query  # noqa: E402

# `resolved_at IS NOT NULL` / `skill_versions IS NOT NULL`. `{"$ne": None}`
# matches neither a null nor a MISSING field, so it is already SQL's IS NOT
# NULL — measured here: 501 of 2693 documents carry a stamp, and the 2192 nulls
# are excluded either way. `$exists` is belt-and-braces that states the intent,
# matching `app/autoresearch/skill_optimizer.py`.
NOT_NULL = {"$ne": None, "$exists": True}


def _wilson(hits: int, n: int) -> tuple[float, float]:
    """95% Wilson interval — honest about small n, unlike hits/n alone."""
    if n <= 0:
        return (0.0, 0.0)
    z = 1.96
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def skill_map(raw) -> dict[str, int]:
    """`LATERAL jsonb_each(skill_versions)` — the {agent: version} pairs.

    THE SHAPE IS NOT ONE SHAPE, and this is the whole reason the port is not a
    filter on `skill_versions.<agent>`. The column was `jsonb`, so the backfill
    (`table_spec._coerce`, which json-decodes every json/jsonb column) landed
    every migrated row as a Mongo SUBDOCUMENT. The live writer does not go
    through that path: `outcome_tracker.record_decisions` builds the snapshot
    with `json.dumps(...)` — a comment there still explains the dump as working
    around the old SQL driver adapting a dict to hstore — and
    `mongo_store.insert_docs` stores what it is given. So every document
    written since the cutover holds JSON **TEXT**.

    Measured on the live collection today: 445 subdocuments (all created on or
    before 2026-08-18, i.e. the archive) and 56 strings (all created on or
    after 2026-08-20, i.e. everything since). `{"skill_versions.v3_bear_agent":
    {"$exists": True}}` matches 445 of the 501 stamped documents — dot notation
    cannot see inside a string, so a Mongo-side `$objectToArray` or a nested
    field filter silently drops the only half that is still growing. Decoding
    here is what keeps both halves in the same scorecard.

    `(value #>> '{}')::int` is the version cast: `->>` yields TEXT in SQL, so
    "3" and 3 compared equal there. int() here does the same, and a value that
    will not cast is dropped rather than guessed at — `unreadable()` counts
    those so an unparseable payload cannot pass for an absent one.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for agent, version in raw.items():
        try:
            out[str(agent)] = int(version)
        except (TypeError, ValueError):
            continue
    return out


def _decodes_to_empty(raw) -> bool:
    """True for a stamp that parses and is empty — `{}`, `"{}"`, None.

    Distinguished from a stamp that will not parse at all, because the report
    says something different about each and only one of them is a defect.
    """
    if raw is None:
        return True
    if isinstance(raw, dict):
        return not raw
    if isinstance(raw, (str, bytes)):
        try:
            return not json.loads(raw)
        except (ValueError, TypeError):
            return False
    return False


def scorecard() -> tuple[list[tuple], int, int]:
    """`(rows, unreadable, empty_stamp)` — one row per (agent, version).

    The last two are counted SEPARATELY because the report says something
    different about each: an unreadable stamp is a defect in the writer, an
    empty one governs no agent and was silently uninteresting under Postgres
    too. Folding them together made the report accuse the writer of corruption
    for a row that is merely empty.

    Rows are `(agent, version, n, avg_pnl, wins, losses, first_seen,
    last_seen)`, matching the tuple the SELECT used to hand back, so the
    printing below is unchanged.

    `n` is `count(*)` and counts every governed decision; `avg_pnl` is SQL's
    `avg()`, which skips NULLs, so the two have different denominators when a
    resolved row has no pnl. Collapsing them would quietly change the number
    the report is built on.
    """
    rows = mongo_query.find_rows(
        "decision_outcomes",
        {"resolved_at": NOT_NULL,
         "skill_versions": NOT_NULL,
         "action": {"$in": ["BUY", "SELL"]}},
        ["skill_versions", "pnl_pct", "outcome", "created_at"],
    )

    buckets: dict[tuple[str, int], dict] = {}
    unreadable = 0
    empty_stamp = 0
    for raw, pnl, outcome, created in rows:
        pairs = skill_map(raw)
        if not pairs:
            # `not pairs` is TWO conditions and they are not the same finding.
            # An EMPTY stamp decodes perfectly and simply governs nobody —
            # `LATERAL jsonb_each('{}')` yielded zero rows and Postgres reported
            # nothing. A stamp that does not decode is a broken payload. Calling
            # both "does not decode" made the report accuse the writer of
            # corruption for a row that is merely uninteresting.
            if _decodes_to_empty(raw):
                empty_stamp += 1
            else:
                unreadable += 1
            continue
        for agent, version in pairs.items():
            b = buckets.setdefault((agent, version), {
                "n": 0, "pnl_sum": 0.0, "pnl_n": 0, "wins": 0, "losses": 0,
                "first": None, "last": None,
            })
            b["n"] += 1
            if pnl is not None:
                b["pnl_sum"] += float(pnl)
                b["pnl_n"] += 1
            if outcome == "WIN":
                b["wins"] += 1
            elif outcome == "LOSS":
                b["losses"] += 1
            if created is not None:
                if b["first"] is None or created < b["first"]:
                    b["first"] = created
                if b["last"] is None or created > b["last"]:
                    b["last"] = created

    def _date(v):
        # min(created_at)::date — the report prints a day, not a timestamp.
        return v.date() if hasattr(v, "date") else v

    out = []
    for (agent, version) in sorted(buckets, key=lambda k: (k[0], k[1])):
        b = buckets[(agent, version)]
        out.append((
            agent, version, b["n"],
            (b["pnl_sum"] / b["pnl_n"]) if b["pnl_n"] else None,
            b["wins"], b["losses"], _date(b["first"]), _date(b["last"]),
        ))
    return out, unreadable, empty_stamp


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", default=None, help="restrict to one agent")
    ap.add_argument("--min-n", type=int, default=5,
                    help="hide versions with fewer resolved decisions (default 5)")
    args = ap.parse_args()

    try:
        rows, unreadable, empty_stamp = scorecard()
    except Exception as e:  # noqa: BLE001
        print(f"query failed: {e}")
        print("\nThis reads the `decision_outcomes` collection in MongoDB. If it "
              "cannot be reached, check TRADING_MONGO_DB and the client config — "
              "there is no Postgres fallback, and the archive stopped on "
              "2026-08-19.")
        return 1

    # The null hypothesis: what staying long would have earned over the same
    # window. Without it a rising tape reads as skill.
    base_row = mongo_query.agg_row(
        "decision_outcomes",
        {"resolved_at": NOT_NULL, "action": "BUY"},
        [("avg", "pnl_pct")],
    )
    baseline = float(base_row[0]) if base_row and base_row[0] is not None else None

    if not rows:
        # The warning below used to sit AFTER this return, so the one state it
        # exists to describe — every stamped decision unreadable — printed
        # "no skill version yet" and exited 0, which is what a healthy empty
        # store prints. A fault must not be reportable as an absence.
        if unreadable:
            print(f"WARNING — {unreadable} stamped decision(s) carry a "
                  "skill_versions payload that does not decode to "
                  "{agent: version}. There are no scoreable rows AT ALL, and "
                  "this is why — do not read the line below as 'nothing has "
                  "accrued yet'.\n")
            return 1
        print("No resolved decisions carry a skill version yet.\n")
        print("Expected until the stamp accrues: rows written before the "
              "2026-07-25 migration carry NULL, deliberately not backfilled.")
        print("A decision needs ~7 days to resolve, so the first usable rows "
              "land about a week after deploy.")
        return 0

    print(f"{'agent':26} {'ver':>4} {'n':>5} {'avg%':>7} {'vs base':>8} "
          f"{'win%':>6} {'95% CI':>14}  window")
    print("-" * 100)
    prev_agent = None
    for agent, version, n, avg_pnl, wins, losses, first_seen, last_seen in rows:
        if args.agent and agent != args.agent:
            continue
        if n < args.min_n:
            continue
        if prev_agent and prev_agent != agent:
            print()
        prev_agent = agent
        avg = float(avg_pnl or 0.0)
        directional = int(wins) + int(losses)
        lo, hi = _wilson(int(wins), directional)
        vs = f"{avg - baseline:+.2f}" if baseline is not None else "n/a"
        print(f"{agent:26} {version:>4} {n:>5} {avg:>+7.2f} {vs:>8} "
              f"{(100.0 * wins / directional if directional else 0):>5.0f}% "
              f"{100 * lo:>5.0f}-{100 * hi:<5.0f}%  {first_seen}..{last_seen}")

    print()
    if unreadable:
        print(f"WARNING — {unreadable} stamped decision(s) carry a skill_versions "
              "payload that does not decode to {agent: version} and are in NO "
              "row above. Investigate before quoting these numbers.")
    if empty_stamp:
        print(f"note — {empty_stamp} decision(s) carry an EMPTY skill_versions "
              "stamp. That is not a broken payload: it governs no agent, and "
              "`jsonb_each('{}')` returned nothing for it under Postgres too.")
    if baseline is not None:
        print(f"BASELINE — always-long over all resolved BUYs: {baseline:+.2f}%")
    print("A version is only interesting if its interval clears the baseline AND "
          "does not overlap the prior version's. At the n values above, expect "
          "neither. Read the module docstring before drawing a conclusion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
