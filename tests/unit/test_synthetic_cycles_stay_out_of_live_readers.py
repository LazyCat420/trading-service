"""A test cycle's rows must not reach a production prompt, trigger or scorer.

`scripts/observe_cycle.py` runs a REAL cycle in the deployed container with
order placement disabled — by design, since 2026-07-25. What was never decided
is what the rest of the system does with the rows it leaves behind, and the
answer turned out to be "everything".

The 2026-08-31 ladder wrote 14 trade_results / 14 shared_desk / 20
decision_scores / 13 decision_outcomes / 12 episodic_memory / 159
whiteboard_entries rows under `cycle-observe-*` ids, and the run's own
contamination probe could not see any of it: `measurement_window_report.py`
counts only ids starting `cycle-v3-`, so its flat counter was evidence about
the FILTER, not about the system.

The four paths pinned here are the ones with live blast radius:

  1. previous-desk handoff -> injected into EVERY agent prompt, and Phase-0
     triage reads its age. Already chained in the wild: MP's "Age: 2h" prior
     desk was itself an observe desk, so MP took the cheap delta path instead
     of the full panel its 490-hour-old production desk would have forced.
  2. the data-report 48h fast path -> quotes the prior thesis into the report
     AND skips fundamentals / multi-API news / reddit / youtube.
  3. watch-desk baseline arming -> a trip enqueues START_V3_CYCLE with
     "trade": True. The ladder left ACTIVE NVDA/JPM/MP triggers citing
     cycle-observe-1788220872.
  4. decision_outcomes -> resolved ~7 days later into the hold-accuracy and
     calibration-ECE cohorts, and copied back into episodic memory.

`TestTheMintersAreAllClassified` is the guard that keeps the predicate honest:
it parses the tree for cycle-id minters instead of trusting a hand-kept list,
because a prefix list that drifts silently is how this class of bug returns.
"""

import ast
import pathlib
import re
from unittest.mock import patch

import pytest

from app.services.cycle_scope import (
    SYNTHETIC_CYCLE_PREFIXES,
    exclude_synthetic,
    is_production_cycle,
    is_synthetic_cycle,
)

REPO = pathlib.Path(__file__).resolve().parents[2]


class TestTheClassification:
    def test_a_production_id_is_production(self):
        assert is_production_cycle("cycle-v3-1788208223")
        assert not is_synthetic_cycle("cycle-v3-1788208223")

    @pytest.mark.parametrize("cid", [
        "cycle-observe-1788220872",
        "bench-nvda-1788204895",
        "canary_v3_ab12cd34",
        "challenger-cycle-observe-1788217529",
        "cycle-v3-audit-1",
    ])
    def test_known_harness_ids_are_synthetic(self, cid):
        assert is_synthetic_cycle(cid)
        assert not is_production_cycle(cid)

    def test_an_unknown_or_missing_id_is_left_visible(self):
        """The predicate fails toward SHOWING a row, never toward hiding one.

        A reader that drops rows it does not recognise would silently hide
        every legacy row written before cycle ids existed. The cost of the
        other direction is one stale test row in a prompt; the cost of this
        one is invisible data loss.
        """
        assert not is_synthetic_cycle(None)
        assert not is_synthetic_cycle("")
        assert not is_synthetic_cycle("some-future-scheme-42")


class TestTheMongoClause:
    def test_it_excludes_synthetic_and_keeps_everything_else(self):
        clause = exclude_synthetic()
        pattern = clause["cycle_id"]["$not"]["$regex"]

        assert re.match(pattern, "cycle-observe-1788220872")
        assert not re.match(pattern, "cycle-v3-1788208223")

    def test_it_can_be_pointed_at_another_field(self):
        assert "cycle" in exclude_synthetic("cycle")


class TestTheMintersAreAllClassified:
    """Parse the source; never trust a hand-kept prefix list.

    An allowlist can drift in both directions and keep its count — this repo
    has been bitten by exactly that (FOREIGN_OWNERS: 14 vs 14, two missing and
    two invented, and the quiescence check could never have said quiet). So
    this derives the minted prefixes from the tree instead of listing them.

    Read by AST rather than regex: the production minter is
    `cycle_id = kwargs.get("cycle_id") or f"cycle-v3-{...}"`, which no
    reasonable naive `cycle_id = f"..."` regex catches, and a non-greedy one
    silently returns "cycle-" for every id and classifies nothing.
    """

    @staticmethod
    def _literal_prefix(node: ast.AST) -> str | None:
        """The constant head of an f-string, e.g. f"cycle-v3-{x}" -> cycle-v3-."""
        if isinstance(node, ast.JoinedStr) and node.values:
            head = node.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                return head.value
        if isinstance(node, ast.BoolOp):  # kwargs.get(...) or f"cycle-v3-{...}"
            for v in node.values:
                got = TestTheMintersAreAllClassified._literal_prefix(v)
                if got:
                    return got
        return None

    def _minted_prefixes(self) -> set[str]:
        found: set[str] = set()
        for d in ("app", "scripts"):
            for f in (REPO / d).rglob("*.py"):
                if f.name == "cycle_scope.py":
                    continue
                try:
                    tree = ast.parse(f.read_text(encoding="utf-8"))
                except SyntaxError:  # pragma: no cover
                    continue
                for node in ast.walk(tree):
                    value = None
                    if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "cycle_id" for t in node.targets
                    ):
                        value = node.value
                    elif isinstance(node, ast.keyword) and node.arg == "cycle_id":
                        value = node.value
                    if value is None:
                        continue
                    prefix = self._literal_prefix(value)
                    if prefix and ("-" in prefix or "_" in prefix):
                        found.add(prefix)
        return found

    def test_the_scan_finds_the_known_minters(self):
        """A guard that finds nothing passes for the wrong reason."""
        found = self._minted_prefixes()

        assert "cycle-observe-" in found, f"scan found only {sorted(found)}"
        assert "cycle-v3-" in found, f"scan found only {sorted(found)}"

    def test_every_minted_prefix_is_classified_one_way_or_the_other(self):
        unclassified = []
        for prefix in sorted(self._minted_prefixes()):
            probe = prefix + "0"
            if not (is_production_cycle(probe) or is_synthetic_cycle(probe)):
                unclassified.append(prefix)

        assert not unclassified, (
            "these cycle-id prefixes are minted somewhere in app/ or scripts/ but "
            "match neither is_production_cycle nor SYNTHETIC_CYCLE_PREFIXES, so "
            "their rows are treated as production by every live reader: "
            + ", ".join(unclassified)
        )

    def test_production_stays_a_single_prefix(self):
        """If a second production minter appears, the allowlist must learn it."""
        producers = {p for p in self._minted_prefixes() if is_production_cycle(p + "0")}

        assert producers == {"cycle-v3-"}, f"unexpected production minters: {producers}"


# ── 1. previous-desk handoff ──────────────────────────────────────────────
class TestThePreviousDeskHandoff:
    def test_the_query_excludes_synthetic_desks(self):
        from app.v3 import desk_persistence

        seen = {}

        def _capture(collection, query, columns, **kw):
            seen["collection"] = collection
            seen["query"] = query
            return None

        with patch.object(desk_persistence.mongo_query, "find_row", _capture):
            desk_persistence.load_latest_desk_for_ticker("nvda")

        assert seen["collection"] == "shared_desk"
        assert seen["query"]["ticker"] == "NVDA"
        assert "$not" in seen["query"]["cycle_id"], (
            "an observe desk here becomes the Manila Envelope in every agent "
            "prompt and makes triage think the ticker was just analysed"
        )


# ── 2. the data-report fast path ──────────────────────────────────────────
class TestTheFastPath:
    def test_the_analysis_results_read_excludes_synthetic(self):
        src = (REPO / "app" / "v3" / "data_report.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        reads = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "find_docs" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and first.value == "analysis_results":
                    reads.append(node)

        assert reads, "the fast-path read of analysis_results is gone — retarget this test"
        for node in reads:
            rendered = ast.unparse(node)
            assert "exclude_synthetic" in rendered, (
                "a fast-path hit does not just quote a prior thesis, it SKIPS the "
                "heavy collectors: " + rendered[:200]
            )


# ── 3. watch-desk arming ──────────────────────────────────────────────────
class TestWatchDeskArming:
    def test_arming_is_guarded_by_the_synthetic_check(self):
        src = (REPO / "app" / "services" / "pipeline_service.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        guarded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test_src = ast.unparse(node.test)
            if "is_synthetic_cycle" not in test_src:
                continue
            if "derive_baseline_watch" in ast.unparse(node):
                guarded = True

        assert guarded, (
            "derive_baseline_watch must not run for a synthetic cycle — a watch "
            "trip enqueues START_V3_CYCLE with trade=True"
        )


# ── 4. the outcome / calibration cohort ───────────────────────────────────
class TestTheOutcomeCohort:
    def test_the_recorder_refuses_a_synthetic_cycle(self):
        from app.autoresearch import outcome_tracker

        def _explode(*a, **kw):  # pragma: no cover - must never run
            raise AssertionError("the recorder touched the DB for a synthetic cycle")

        with patch.object(outcome_tracker.mongo_query, "find_rows", _explode):
            recorded = outcome_tracker.record_cycle_decisions(
                "cycle-observe-1788220872", {"tickers": ["NVDA"]}
            )

        assert recorded == 0

    def test_the_resolver_skips_rows_already_in_the_table(self):
        """The 13 observe HOLDs from 2026-08-31 are still stored on purpose.

        Leaving them unresolved keeps the evidence of what happened while the
        30-day resolved cohort behind hold-accuracy and calibration ECE stays
        clean. Deleting them would have destroyed the audit trail instead.
        """
        from app.autoresearch import outcome_tracker

        seen = {}

        def _capture(collection, query, columns, **kw):
            seen["query"] = query
            return []

        with patch.object(outcome_tracker.mongo_query, "find_rows", _capture):
            outcome_tracker.resolve_pending_outcomes()

        assert "$not" in seen["query"]["cycle_id"]
