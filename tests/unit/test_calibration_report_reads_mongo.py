"""`calibration_report.py` must read the store the decisions are written to.

It is where the confidence floor comes from. Pointed at the frozen archive it
does not fail visibly — it re-derives a LIVE policy gate from a population that
stopped on 2026-08-19 and prints the same confident table, which is the worst
shape a measurement instrument can take. Since 2026-08-28 it did not even do
that: the archive DSN field was deleted, so every run died with AttributeError
before the first line.

These tests pin the three things the port could have got wrong quietly.

1. WHERE IT READS. A source-level check, because there is no run-time symptom:
   a Postgres-backed run and a Mongo-backed run print the same headings.

2. WHICH ROWS COUNT. `decision_outcomes` contains pipeline crashes scored as
   trades. `confidence > 0` is the clause that removes them, and it is load
   bearing in a way the docstring's framing understates: measured 2026-08-30,
   dropping it alone re-creates the fake effect the report exists to avoid —
   the SELL 0-59 band goes from n=37 mean -3.27% to n=393 mean -5.53% (the
   report's own recorded "n=392 mean -5.55%"), and a floor "justified" by that
   band is gating on the parse-failure rate, not on confidence. The two
   `NOT LIKE` clauses remove only 8 further rows across both actions, because
   the lesson-marked rows are almost all confidence=0 as well.

3. WHICH ROWS SURVIVE. `COALESCE(lesson_stored,'') NOT LIKE '%x%'` KEEPS a
   decision that stored no lesson. Mongo's obvious negations do not: `$ne`
   and `$nin` are membership tests a missing field fails, and 185 clean
   decisions have no lesson (133 BUY, 52 SELL). Only `{"$not": ...}` is the
   complement. Both forms run, neither errors, and the wrong one just returns
   a smaller table.

The live half is pinned to the 2026-07-26 anchor the report exists to
preserve, restricted with `--as-of` so a growing collection cannot wash it out.

RED BEFORE THE PORT, and red for each way the port could have gone wrong.
Verified 2026-08-30 by injecting mutated copies of the module into this file
and running these same assertions against them, not by assertion:

    control (as shipped)                 17 pass /  0 fail
    the pre-port Postgres file            2 pass / 15 fail
    lesson clause -> `$ne` + `$not`      13 pass /  4 fail
    `confidence > 0` dropped             14 pass /  3 fail
    lesson clause dropped                12 pass /  5 fail
    `sort=[("created_at", 1)]` dropped   16 pass /  1 fail
    regex made case-insensitive          15 pass /  2 fail
"""
from __future__ import annotations

import datetime as dt
import re
import statistics
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import calibration_report as cr  # noqa: E402

SOURCE_PATH = REPO / "scripts" / "calibration_report.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")

#: The residual-coupling grep from the porting runbook, as a test.
PG_COUPLING = re.compile(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres")

#: The recorded 2026-07-26 measurement, and the day it was taken.
ANCHOR_DAY = "2026-07-26"
ANCHOR_HIGH_N = 693          # recorded, and reproduced exactly
ANCHOR_LOW_N = 135           # recorded; 136 today, see test docstring


class TestItReadsMongo:
    """No path back to the archive, and the read goes through the one seam."""

    def test_no_postgres_coupling_is_left_in_the_source(self):
        hits = [(i, ln) for i, ln in enumerate(SOURCE.splitlines(), 1)
                if PG_COUPLING.search(ln)]
        assert not hits, (
            "the report still names a Postgres symbol:\n"
            + "\n".join(f"  {i}: {ln.strip()}" for i, ln in hits)
        )

    def test_the_read_goes_through_the_row_shaped_mongo_seam(self):
        assert "from app.db import mongo_query" in SOURCE
        assert cr.COLLECTION == "decision_outcomes"

    def test_it_passes_the_table_name_not_a_resolved_collection(self):
        """`collection_for()` runs exactly once, inside `mongo_query`.

        Resolving it at the call site too would read one name and, the day
        renames are switched on, write another — the defect
        `tests/unit/test_no_double_collection_resolution.py` exists for.
        """
        assert "collection_for" not in SOURCE

    def test_it_bootstraps_the_repo_root_onto_sys_path(self):
        """`python scripts/calibration_report.py` puts `scripts/` on the path,
        not the repo root, so a module-scope `from app.db import ...` exits 1
        with no output unless the bootstrap runs FIRST."""
        body = SOURCE.split('"""', 2)[-1]
        assert body.index("sys.path.insert") < body.index("from app.db")


class TestTheExclusionFilterIsIntact:
    """Structure only — these run with no database at all."""

    def test_the_lesson_clause_is_a_complement_not_a_membership_test(self):
        clause = cr.clean_query("BUY")["lesson_stored"]
        assert set(clause) == {"$not"}, (
            f"lesson_stored is filtered with {sorted(clause)}. `$ne`/`$nin` are "
            "membership tests that a missing or null field FAILS, so they "
            "silently delete the 185 decisions that stored no lesson — which "
            "`COALESCE(lesson_stored,'') NOT LIKE` kept."
        )

    def test_both_failure_texts_are_still_excluded(self):
        rx = cr._FAILED_LESSON["$regex"]
        assert "PIPELINE FAILURE" in rx
        assert "Failed to parse" in rx

    def test_the_match_stays_case_sensitive_like_sql_LIKE(self):
        """`LIKE` is case-sensitive and `$regex` without `$options` is too.
        Adding `"i"` would widen the excluded set the first time a lesson is
        written in another case — a change to the calibration population."""
        assert "$options" not in cr._FAILED_LESSON

    def test_zero_confidence_rows_are_excluded(self):
        assert cr.clean_query("BUY")["confidence"] == {"$ne": None, "$gt": 0}

    def test_the_null_checks_also_exclude_a_missing_field(self):
        """Postgres column defaults are gone, so post-cutover documents can
        lack `pnl_pct`/`resolved_at` entirely. `$ne: None` excludes a missing
        field exactly as `IS NOT NULL` excluded a NULL."""
        q = cr.clean_query("BUY")
        assert q["resolved_at"] == {"$ne": None}
        assert q["pnl_pct"] == {"$ne": None}

    def test_as_of_is_inclusive_of_the_day_it_names(self):
        assert cr._as_of(ANCHOR_DAY) == dt.datetime(2026, 7, 27,
                                                    tzinfo=dt.timezone.utc)
        q = cr.clean_query("BUY", cr._as_of(ANCHOR_DAY))
        assert q["resolved_at"] == {"$ne": None,
                                    "$lt": dt.datetime(2026, 7, 27,
                                                       tzinfo=dt.timezone.utc)}

    def test_rows_are_read_in_chronological_order(self):
        """The chronological split is the check the whole finding rests on."""
        assert "sort=[(\"created_at\", 1)]" in SOURCE


class TestAgainstTheLiveCollection:
    """Read-only, against production Mongo — `TRADING_BOT_LIVE_AUDIT=1`.

    An offline test cannot tell a filter that runs from a filter that is
    right: every variant here parses, runs and returns a plausible table.
    """

    def test_created_at_is_a_date_everywhere(self, live_mongo):
        """A string timestamp sorts above every BSON Date, so one would deal
        the chronological halves wrong without raising."""
        assert cr.mongo_query.count(
            cr.COLLECTION, {"created_at": {"$type": "string"}}) == 0

    def test_the_anchor_measurement_still_reproduces(self, live_mongo):
        """The 2026-07-26 finding, re-derived from Mongo.

        Recorded: <72 n=135 mean -1.77% (-4.64% vs the null), >=72 n=693 mean
        +3.77% (+0.90%). Reproduced 2026-08-30: 136 / -1.84 / -4.72 and
        693 / +3.80 / +0.93. The high band matches to the row. The low band is
        one row wider and cannot be made exact: outcomes are re-resolved after
        the fact and `pnl_pct` rewritten when they are, so the population "as
        of 2026-07-26" is not a thing any later store can rebuild row for row.
        Both signs, both magnitudes and the ordering survive, which is the
        claim the report makes.
        """
        rows = cr.fetch("BUY", cr._as_of(ANCHOR_DAY))
        null = statistics.fmean(p for _, p in rows)
        low = [p for c, p in rows if c < 72]
        high = [p for c, p in rows if c >= 72]

        assert len(high) == ANCHOR_HIGH_N, (
            f"the >=72 band is {len(high)}, and the 2026-07-26 measurement "
            f"recorded {ANCHOR_HIGH_N}. That band reproduces to the row, so a "
            "change here is the population moving, not noise: a lesson clause "
            "turned into a membership test reads 663 and a dropped one 696."
        )
        assert abs(len(low) - ANCHOR_LOW_N) <= 2, (
            f"the low band is {len(low)}, recorded {ANCHOR_LOW_N}. More than a "
            "row or two of drift means the population changed, not the "
            "arithmetic — check the exclusion filter before relaxing this."
        )
        # The finding: the low band loses ~4.6pp against the always-long null,
        # and the high band beats it by under a point.
        assert statistics.fmean(low) - null < -4.0
        assert 0.5 < statistics.fmean(high) - null < 1.5

    def test_the_zero_confidence_rows_are_what_the_filter_removes(self, live_mongo):
        """Dropping `confidence > 0` re-creates the fake effect.

        Measured 2026-08-30: SELL 0-59 is n=37 mean -3.27% with the clause and
        n=393 mean -5.53% without it — the report's recorded "n=392 mean
        -5.55%", a band that would dominate the sweep and justify a floor on
        the pipeline's crash rate.
        """
        rows = cr.fetch("SELL")
        assert rows, "empty SELL population — the filter cannot be judged"
        assert all(c > 0 for c, _ in rows)

        band = [p for c, p in rows if c < 60]
        assert len(band) < 100, (
            f"the SELL 0-59 band holds {len(band)} rows; with confidence=0 "
            "excluded it holds 37. This is the parse-failure population "
            "back in the calibration."
        )
        assert statistics.fmean(band) > -5.0

    def test_no_pipeline_failure_lesson_survives_the_filter(self, live_mongo):
        """`$and`, not a merged dict: a merged dict would OVERWRITE the
        `lesson_stored` clause under test and count the corrupt rows instead
        of proving they are gone. Returns 4 per action if the clause is
        dropped, 0 while it is there."""
        for action in ("BUY", "SELL"):
            leaked = cr.mongo_query.count(cr.COLLECTION, {"$and": [
                cr.clean_query(action),
                {"lesson_stored": cr._FAILED_LESSON},
            ]})
            assert leaked == 0, (
                f"{leaked} {action} rows whose own lesson says the pipeline "
                "failed are being scored as decisions")

    def test_decisions_that_stored_no_lesson_are_kept(self, live_mongo):
        """The trap that costs 185 rows and raises nothing.

        The foil is built here rather than described: the same query with a
        membership test bolted on is what a careful-looking port produces, and
        it must return strictly fewer rows than the complement form.
        """
        for action, expected in (("BUY", 133), ("SELL", 52)):
            kept = cr.clean_query(action)
            membership = dict(kept, lesson_stored={"$ne": None,
                                                   "$not": cr._FAILED_LESSON})
            lessonless = cr.mongo_query.count(
                cr.COLLECTION, dict(kept, lesson_stored=None))
            assert lessonless == pytest.approx(expected, abs=15), (
                f"{action}: {lessonless} kept decisions have no lesson, "
                f"expected about {expected}")
            assert (cr.mongo_query.count(cr.COLLECTION, kept)
                    - cr.mongo_query.count(cr.COLLECTION, membership)
                    == lessonless), (
                "the membership form drops exactly the lesson-less decisions; "
                "if these are equal the filter has become a membership test")

    def test_the_population_partitions_into_clean_and_corrupt(self, live_mongo):
        """Nothing is dropped that is neither a decision nor a known failure —
        an exclusion that quietly loses a third category is how a calibration
        turns into a cherry-pick."""
        for action in ("BUY", "SELL"):
            base = {"resolved_at": {"$ne": None}, "action": action,
                    "pnl_pct": {"$ne": None}}
            clean = cr.mongo_query.count(cr.COLLECTION, cr.clean_query(action))
            corrupt = cr.mongo_query.count(cr.COLLECTION, dict(
                base, **{"$or": [{"confidence": 0},
                                 {"lesson_stored": cr._FAILED_LESSON}]}))
            assert clean + corrupt == cr.mongo_query.count(cr.COLLECTION, base)
