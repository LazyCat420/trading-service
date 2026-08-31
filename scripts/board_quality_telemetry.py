"""
Board Quality Telemetry (plan item B6).

A read-only, data-driven map of WHERE board decision quality is falling, so fixes
target real problems instead of guesses. Prints:

  1. Per-ticker mean quality        (decision_evaluations.final_quality_score)
  2. Per-regime mean quality        (joined to trade_results.regime)
  3. Failure-reason distribution    (evidence_gathering JSON 'failure_reason')
  4. Zero-score rate                (red-card / precheck hard-zeros)
  5. Regime → persona routing        (trade_results: is it collapsing to one?)
  6. H2H tournament persona win rate (debate_history) — SEE THE WARNING BELOW

Storage notes (re-verified against LIVE MongoDB, 2026-08-30):
  - decision_evaluations: final_quality_score is a top-level field; failure_reason
    is NOT — it lives inside `evidence_gathering`, which is a JSON **STRING**
    (`$type` says "string" for all 1131 documents), not a subdocument. A Mongo
    filter on `evidence_gathering.failure_reason` therefore matches NOTHING while
    the value sits right there in the text. Every read of it here goes through
    `json.loads`.
  - regime / persona_used live on trade_results, keyed (ticker, cycle_id).
    trade_results is UNIQUE on that pair (0 duplicate groups); decision_evaluations
    is NOT (121 duplicate groups), so the join is many-to-one and the LEFT JOIN
    cannot fan the left side out.
  - tournament debates land in debate_history with persona_name='tournament',
    winner in {'bull','bear'}, and pro/con_argument JSON strings carrying the
    persona.

SECTION 6 IS A RETIRED SUBSYSTEM — IT IS EXPECTED TO BE EMPTY.
    The tournament debate was retired on 2026-07-29
    (HANDOFF_tournament_retired_2026-07-29.md; DEBATE_ENGINE=3, no debate).
    Nothing in `app/` reads or writes `debate_history` any more — the only
    remaining references are the CREATE TABLE in scripts/migration/ and this
    file. The collection is frozen: 846 documents, newest 2026-07-29 20:05:55.
    So for any window shorter than the age of that last row, section 6 answers
    "(no rows)" and always will. That is not a broken port. Section 6 prints the
    age of the newest tournament debate alongside the empty result so the
    emptiness is read as "retired", not as "the tournament suddenly stopped
    winning".

Storage: MongoDB (`trading_bot`). This script used to read PostgreSQL, which
froze at the 2026-08-19 cutover — it kept answering, with July numbers, for a
window it labelled "last 14 days".

Usage:
    python scripts/board_quality_telemetry.py [DAYS]
    (DAYS = lookback window, default 14)
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

# `python scripts/board_quality_telemetry.py` puts `scripts/` on sys.path[0],
# not the repo root, and the venv carries no path entry for the repo — so the
# `app.db` imports below raise ModuleNotFoundError under the exact command the
# Usage line above documents, and the script dies with exit 1 before it prints
# a single panel. The archive version needed no bootstrap: it imported only the
# stdlib and a DB driver, both installed. Reading Mongo through `app/` is what
# makes the repo root a dependency, so this port has to add it, as every other
# script here that imports `app` at module scope already does.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_query, mongo_store  # noqa: E402
from app.db.collections import collection_for  # noqa: E402

DE = collection_for("decision_evaluations")
TR = collection_for("trade_results")
DH = collection_for("debate_history")


# ── formatting helpers ─────────────────────────────────────────────────────
def _round(value, places: int):
    """`ROUND(x::numeric, n)` — half-up, and it PRINTS its trailing zeros.

    Not Python's `round()`: that is banker's rounding (round(2.675, 2) -> 2.67
    where Postgres numeric gives 2.68) and it returns a float, so 2.50 renders
    as "2.5" and the column stops lining up with what the SQL used to print.
    Decimal(str(v)) and not Decimal(v): the shortest round-trip text is what
    Postgres' float8->numeric cast uses, so the two stores round the same value.
    """
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places),
                                        rounding=ROUND_HALF_UP)


def _pct(n: int, total: int, places: int = 1):
    """`ROUND(100.0 * n / NULLIF(total,0), places)` — NULL when the total is 0."""
    if not total:
        return None
    return _round(Decimal(100) * Decimal(n) / Decimal(total), places)


def _mean(values: list) -> float | None:
    """SQL AVG: NULLs are skipped, and an all-NULL group averages to NULL."""
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def q(title: str, columns, rows, note: str | None = None) -> None:
    """Print one panel in the shape the old DB-API cursor printed it."""
    print(f"\n--- {title} ---")
    print(" | ".join(columns))
    if not rows:
        print("(no rows)")
    for row in rows:
        print(" | ".join("" if v is None else str(v) for v in row))
    if note:
        print(note)


def _window(days: int) -> datetime:
    """The `NOW() - INTERVAL 'N days'` boundary, as naive UTC.

    Both stores agree on this only because the Postgres server ran at
    TimeZone='Etc/UTC' and pymongo hands back naive UTC datetimes; the archive's
    `decision_evaluations.timestamp` was `timestamp without time zone` holding
    UTC. Comparing a naive local `datetime.now()` here would silently shift the
    window by the box's offset (7 h on this host) and quietly drop or add rows.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)


# ── the six questions ──────────────────────────────────────────────────────
def per_ticker_quality(since: datetime, until: datetime | None = None):
    """1. Per-ticker mean quality (0-5 scale). Lowest first — worst on top."""
    rows = mongo_query.group_rows(
        DE, _ts_query("timestamp", since, until),
        keys=["ticker"],
        aggs=[("count", None),
              ("avg", "final_quality_score"),
              ("min", "final_quality_score"),
              ("max", "final_quality_score")],
        select=[("key", "ticker"), ("agg", 0), ("agg", 1), ("agg", 2), ("agg", 3)],
    )
    out = [(t, n, _round(mean, 2), _round(lo, 2), _round(hi, 2))
           for t, n, mean, lo, hi in rows]
    # ORDER BY mean_q ASC NULLS FIRST, then ticker so the LIMIT 40 cut is
    # reproducible — Postgres left ties unordered, so the 40th row could change
    # between two runs over identical data.
    out.sort(key=lambda r: (r[2] is not None, r[2] if r[2] is not None else 0,
                            r[0] or ""))
    return ["ticker", "n", "mean_q", "min_q", "max_q"], out[:40]


def per_regime_quality(since: datetime, until: datetime | None = None):
    """2. Per-regime mean quality — LEFT JOIN de -> tr ON (ticker, cycle_id).

    Hand-stitched rather than `mongo_query.left_join_rows`, which joins on ONE
    equality; this join is on the PAIR. Joining on ticker alone would attach
    every cycle's regime to every evaluation of that ticker — a fan-out that
    still produces a plausible-looking table.
    """
    de_docs = mongo_store.find_docs(
        DE, _ts_query("timestamp", since, until),
        projection={"ticker": 1, "cycle_id": 1, "final_quality_score": 1, "_id": 0})
    tr_docs = mongo_store.find_docs(
        TR, {}, projection={"ticker": 1, "cycle_id": 1, "regime": 1, "_id": 0})

    index: dict[tuple, list] = {}
    for d in tr_docs:
        key = (d.get("ticker"), d.get("cycle_id"))
        if None in key:
            continue  # `NULL = NULL` is not true: a keyless row joins to nothing
        index.setdefault(key, []).append(d)

    groups: dict[object, list] = defaultdict(list)
    for d in de_docs:
        key = (d.get("ticker"), d.get("cycle_id"))
        matches = [] if None in key else index.get(key, [])
        for r in (matches or [None]):        # LEFT JOIN: no match -> one NULL row
            # `.get` and not `[...]`, the rule mongo_query._to_tuple states for
            # exactly this reason: "a document written before a column was
            # added simply lacks the field, and Postgres would have returned
            # NULL for it." One trade_results document with no `regime` key
            # would turn this whole panel into `Error: 'regime'` where the SQL
            # printed `(unknown)`. `regime` is present on all 1155 documents
            # today, so this is a guard against the next write, not a live bug.
            groups[r.get("regime") if r else None].append(
                d.get("final_quality_score"))

    out = [(regime if regime is not None else "(unknown)",
            len(scores), _round(_mean(scores), 2))
           for regime, scores in groups.items()]
    out.sort(key=lambda r: (r[2] is not None, r[2] if r[2] is not None else 0, r[0]))
    return ["regime", "n", "mean_q"], out


def failure_reasons(since: datetime, until: datetime | None = None):
    """3. Failure-reason distribution.

    `failure_reason` is a key inside the `evidence_gathering` JSON STRING, so
    this parses each document. `{"evidence_gathering.failure_reason": {...}}`
    as a Mongo filter matches 0 of 1131 documents — the field does not exist as
    a path, only as text.
    """
    docs = mongo_store.find_docs(
        DE, _ts_query("timestamp", since, until),
        projection={"evidence_gathering": 1, "final_quality_score": 1, "_id": 0})

    groups: dict[str, list] = defaultdict(list)
    for d in docs:
        groups[_failure_reason(d.get("evidence_gathering"))].append(
            d.get("final_quality_score"))

    total = len(docs)
    out = [(reason, len(scores), _pct(len(scores), total), _round(_mean(scores), 2))
           for reason, scores in groups.items()]
    out.sort(key=lambda r: (-r[1], r[0]))
    return ["failure_reason", "n", "pct", "mean_q"], out


def _failure_reason(evidence_gathering) -> str:
    """`COALESCE((evidence_gathering::jsonb) ->> 'failure_reason', 'none')`.

    A missing column, a missing key and a JSON null all collapse to 'none',
    which is what `->>` + COALESCE did. Text that will not parse is bucketed
    separately instead of raising: Postgres failed the WHOLE panel on one bad
    row, which is a worse answer than naming the rows it could not read.
    """
    if evidence_gathering is None:
        return "none"
    if isinstance(evidence_gathering, str):
        try:
            evidence_gathering = json.loads(evidence_gathering)
        except (ValueError, TypeError):
            return "(unparseable)"
    if not isinstance(evidence_gathering, dict):
        return "none"
    reason = evidence_gathering.get("failure_reason")
    return "none" if reason is None else str(reason)


def zero_score_rate(since: datetime, until: datetime | None = None):
    """4. Zero-score rate — how often the final score is hard-zeroed."""
    window = _ts_query("timestamp", since, until)
    total = mongo_query.count(DE, window)
    zeroed = mongo_query.count(DE, dict(window, final_quality_score=0))
    return ["total", "zeroed", "zero_pct"], [(total, zeroed, _pct(zeroed, total))]


def regime_persona_routing(since: datetime, until: datetime | None = None):
    """5. Regime → persona routing distribution. B3 predicted a collapse onto
    Jane Street/CONTRADICTORY — confirm it with data."""
    rows = mongo_query.group_rows(
        TR, _ts_query("created_at", since, until),
        keys=["regime", "persona_used"],
        aggs=[("count", None)],
        select=[("key", "regime"), ("key", "persona_used"), ("agg", 0)],
    )
    out = [(r if r is not None else "(none)", p if p is not None else "(none)", n)
           for r, p, n in rows]
    out.sort(key=lambda x: (-x[2], x[0], x[1]))
    return ["regime", "persona_used", "n"], out


def tournament_win_rate(since: datetime, until: datetime | None = None):
    """6. H2H tournament persona win rate — A RETIRED SUBSYSTEM (see module docstring).

    The winning persona is pro_argument.persona (bull) or con_argument.persona
    (bear); both are JSON STRINGS, like evidence_gathering.
    """
    query = dict(_ts_query("created_at", since, until),
                 persona_name="tournament", winner={"$in": ["bull", "bear"]})
    docs = mongo_store.find_docs(
        DH, query,
        projection={"winner": 1, "pro_argument": 1, "con_argument": 1, "_id": 0})

    counts: dict[str, int] = defaultdict(int)
    for d in docs:
        side = "pro_argument" if d.get("winner") == "bull" else "con_argument"
        counts[_json_key(d.get(side), "persona") or "(unparsed)"] += 1

    total = sum(counts.values())
    out = [(persona, wins, _pct(wins, total)) for persona, wins in counts.items()]
    out.sort(key=lambda r: (-r[1], r[0]))
    return ["winning_persona", "wins", "pct"], out


def _json_key(raw, key: str):
    """`(col::jsonb) ->> key` where `col` is a JSON **STRING** in Mongo.

    `pro_argument` and `con_argument` are `$type` "string" for all 846
    documents, exactly like `evidence_gathering`. Returning None for anything
    that will not parse reproduces `->>`'s NULL, which the caller COALESCEs to
    '(unparsed)'.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    value = raw.get(key)
    return None if value is None else str(value)


def tournament_retirement_note() -> str | None:
    """Why section 6 is empty, taken from the data rather than asserted.

    Trap 7 of this migration: "a script that compiles, runs and returns [] is
    the exact failure this effort exists to catch." Section 6 legitimately
    returns nothing, so it has to SHOW that — by naming the age of the newest
    tournament debate in the collection, whatever the window was.
    """
    newest = mongo_query.scalar(DH, {"persona_name": "tournament"}, "created_at",
                                sort=[("created_at", -1)])
    if newest is None:
        return ("(note: no tournament debate has ever been recorded in "
                f"{DH} — the subsystem was retired 2026-07-29)")
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - newest).days
    return (f"(note: the newest tournament debate in {DH} is {newest} — "
            f"{age} days old. The tournament was RETIRED on 2026-07-29 and "
            "nothing writes this collection any more, so an empty result here "
            "is expected, not a failed read.)")


def _ts_query(field: str, since: datetime, until: datetime | None) -> dict:
    """`WHERE field > since [AND field <= until]`.

    `until` has no CLI flag and defaults to open-ended, exactly as the SQL was;
    it exists so a caller (the parity probe, the unit test) can pin a window
    that ended before the 2026-08-19 cutover, where Mongo and the Postgres
    archive must agree row for row.
    """
    bounds: dict = {"$gt": since}
    if until is not None:
        bounds["$lte"] = until
    return {field: bounds}


def main(argv: list[str]) -> int:
    days = int(argv[1]) if len(argv) > 1 else 14
    since = _window(days)

    print(f"=== Board Quality Telemetry — last {days} days ===")

    panels = [
        ("1. Per-ticker mean quality", per_ticker_quality, None),
        ("2. Per-regime mean quality", per_regime_quality, None),
        ("3. Failure-reason distribution", failure_reasons, None),
        ("4. Zero-score rate", zero_score_rate, None),
        ("5. Regime -> persona routing", regime_persona_routing, None),
        ("6. H2H tournament persona win rate", tournament_win_rate,
         tournament_retirement_note),
    ]
    for title, fn, empty_note in panels:
        try:
            columns, rows = fn(since)
            note = empty_note() if (empty_note and not rows) else None
            q(title, columns, rows, note)
        except Exception as e:  # noqa: BLE001 — one dead panel must not hide the rest
            print(f"\n--- {title} ---")
            print(f"Error: {e}")

    print("\n=== done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
