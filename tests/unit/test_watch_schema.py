"""The watch contract: what a watch may not omit, and what a planner may not say.

The load-bearing assertion in this file is that a watch record with no
RESOLUTION CONDITION is refused. Measured 2026-09-06: all 185 live watches
carried a thesis (prose) and 0 carried a stated open question, and the desk
spent its full daily budget on 21 of 21 days on headlines it had nothing to
screen against. A prompt asking for the field is not enforcement; this is.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import watch_schema as ws

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _rc(**over):
    base = {
        "open_question": "Does Q3 net interest margin hold above 3.4%?",
        "resolving_fact": "Q3 NIM disclosed on the October earnings call",
        "resolves_by": NOW + timedelta(days=38),
        "invalidates_if": {"type": "price_below", "level": 61.0},
        "becomes_if_true": "BUY", "becomes_if_false": "SELL",
    }
    base.update(over)
    return base


def _dc(**over):
    base = {"action": "HOLD", "confidence": 0.55, "thesis_summary": "Cheap on book value.",
            "resolution_condition": _rc(), "source_cycle_id": "cycle-v3-1", "decided_at": NOW}
    base.update(over)
    return base


class TestTheMandatoryField:
    def test_a_watch_without_a_resolution_condition_is_refused(self):
        doc, err = ws.normalize_watch_record(
            ticker="C", watch_id="w1", decision_context=_dc(resolution_condition=None),
            now=NOW)
        assert doc is None
        assert "resolution_condition" in err

    def test_a_thesis_alone_is_not_enough(self):
        """The exact shape of all 185 live watches: prose, no open question."""
        doc, err = ws.normalize_watch_record(
            ticker="C", watch_id="w1",
            decision_context={"action": "HOLD",
                              "thesis_summary": "Citi is cheap on tangible book."},
            now=NOW)
        assert doc is None, "a thesis-only watch must not arm"

    def test_an_invalidation_level_alone_is_not_enough(self):
        """The shape of the 102 watches that DID carry a price_below.

        A level with no open question cannot screen a headline — which is why
        27 of the last 30 trips were news that no level could refuse.
        """
        doc, err = ws.normalize_watch_record(
            ticker="C", watch_id="w1",
            decision_context=_dc(resolution_condition={
                "invalidates_if": {"type": "price_below", "level": 61.0}}),
            now=NOW)
        assert doc is None
        assert "open_question" in err

    def test_open_question_and_resolving_fact_are_both_required(self):
        _, e1 = ws.normalize_resolution_condition(_rc(open_question=""))
        _, e2 = ws.normalize_resolution_condition(_rc(resolving_fact=""))
        assert "open_question" in e1 and "resolving_fact" in e2

    def test_a_complete_condition_is_accepted_and_normalised(self):
        doc, err = ws.normalize_watch_record(
            ticker="c", watch_id="w1", decision_context=_dc(), now=NOW)
        assert err is None
        assert doc["ticker"] == "C"
        assert doc["schema_version"] == ws.WATCH_SCHEMA_VERSION
        rc = doc["decision_context"]["resolution_condition"]
        assert rc["resolves_by"].tzinfo is not None
        assert "margin" in rc["resolving_terms"]

    def test_an_uncheckable_invalidation_trigger_is_refused(self):
        _, err = ws.normalize_resolution_condition(
            _rc(invalidates_if={"type": "vibes", "level": 3}))
        assert "not checkable" in err


class TestResolutionTerms:
    def test_stopwords_are_excluded(self):
        """A term list made of stopwords matches every headline and fails OPEN.

        That is indistinguishable from having no screen, which is the failure
        this whole contract exists to prevent — so assert the absence, not just
        the presence.
        """
        terms = ws.resolution_terms("Will the company report more revenue this quarter?")
        for junk in ("the", "this", "company", "quarter", "more", "will"):
            assert junk not in terms
        assert "revenue" in terms

    def test_terms_are_stable_and_deduplicated(self):
        a = ws.resolution_terms("Margin margin MARGIN guidance")
        assert a == sorted(set(a))
        assert a.count("margin") == 1

    def test_two_callers_building_from_the_same_text_agree(self):
        """The derivation lives in ONE place on purpose — a second copy would
        drift and no test would notice, because both would still 'work'."""
        text = "Q3 net interest margin above 3.4%"
        assert ws.resolution_terms(text) == ws.resolution_terms(text)


class TestDecisionContext:
    @pytest.mark.parametrize("bad", ["DEGRADED", "", "ERROR", "UNKNOWN", None])
    def test_a_non_decision_may_not_arm_a_watch(self, bad):
        """9 of the last 30 trips descended from a prior action of DEGRADED —
        an error state that analysis_results records as if it were a decision,
        so 'the decision changed' compared a decision against a failure."""
        _, err = ws.normalize_decision_context(_dc(action=bad))
        assert err is not None and "not a decision" in err

    def test_confidence_on_a_0_100_scale_is_normalised(self):
        dc, _ = ws.normalize_decision_context(_dc(confidence=80))
        assert dc["confidence"] == pytest.approx(0.8)

    def test_confidence_on_a_0_1_scale_is_untouched(self):
        dc, _ = ws.normalize_decision_context(_dc(confidence=0.8))
        assert dc["confidence"] == pytest.approx(0.8)

    def test_a_garbage_confidence_becomes_unknown_not_zero(self):
        """Zero would read as 'certainly wrong'; None reads as 'not recorded'."""
        dc, _ = ws.normalize_decision_context(_dc(confidence="banana"))
        assert dc["confidence"] is None


class TestLegacyWatches:
    def test_a_legacy_watch_is_detected_not_backfilled(self):
        legacy = {"id": "w0", "ticker": "C", "thesis_summary": "prose"}
        assert ws.is_legacy(legacy)
        assert ws.get_resolution_condition(legacy) is None

    def test_get_resolution_condition_returns_none_never_an_empty_pass(self):
        """None must mean 'cannot screen'. A caller that reads it as an empty
        dict screens clean against nothing and certifies everything."""
        assert ws.get_resolution_condition({}) is None
        assert ws.get_resolution_condition(None) is None
        assert ws.get_resolution_condition({"decision_context": {}}) is None

    def test_a_schema1_watch_is_not_legacy(self):
        doc, _ = ws.normalize_watch_record(
            ticker="C", watch_id="w1", decision_context=_dc(), now=NOW)
        assert not ws.is_legacy(doc)
        assert ws.get_resolution_condition(doc) is not None


class TestCatalystCalendar:
    def test_looked_and_found_nothing_differs_from_never_looked(self):
        """Collapsing these makes a broken lookup read as 'no catalysts', which
        suppresses urgency exactly when the instrument is down."""
        looked = ws.normalize_catalyst_calendar({"sources": ["fundamentals"], "events": []})
        never = ws.normalize_catalyst_calendar({})
        assert looked["sources"] and not never["sources"]

    def test_events_are_sorted_and_undated_ones_dropped(self):
        cal = ws.normalize_catalyst_calendar({"events": [
            {"kind": "macro", "at": NOW + timedelta(days=3)},
            {"kind": "earnings", "at": NOW + timedelta(days=1)},
            {"kind": "junk"},                     # no date — unusable
        ]})
        assert [e["kind"] for e in cal["events"]] == ["earnings", "macro"]


class TestPlannerResult:
    def test_the_four_actions_are_exactly_these(self):
        assert set(ws.PLANNER_ACTIONS) == {
            "ANALYZE_NOW", "SCHEDULE_AT", "WATCH_FOR_CONDITION", "CLOSE_WATCH"}

    def test_an_invented_action_is_refused(self):
        _, err = ws.normalize_planner_result({"action": "BUY_THE_STOCK"})
        assert "invalid" in err

    def test_schedule_at_without_a_time_is_refused(self):
        _, err = ws.normalize_planner_result({"action": "SCHEDULE_AT"})
        assert "requires `at`" in err

    def test_watch_for_condition_requires_a_checkable_trigger(self):
        _, err = ws.normalize_planner_result(
            {"action": "WATCH_FOR_CONDITION", "condition": {"type": "gut_feel"}})
        assert "not checkable" in err

    def test_close_watch_requires_a_reason(self):
        """A watch is not closed silently — the close reason is the only record
        of why monitoring stopped."""
        _, err = ws.normalize_planner_result({"action": "CLOSE_WATCH"})
        assert "close_reason" in err

    def test_a_json_string_is_accepted(self):
        r, err = ws.normalize_planner_result(
            '{"action":"ANALYZE_NOW","rationale":"guidance cut"}')
        assert err is None and r["action"] == "ANALYZE_NOW"

    def test_normalisation_does_not_enforce_time_bounds(self):
        """Bounds belong to watch_policy, on ONE seam. If this file starts
        enforcing them too, the same rule lives in two places and they drift."""
        past = NOW - timedelta(days=400)
        r, err = ws.normalize_planner_result(
            {"action": "SCHEDULE_AT", "at": past.isoformat()}, now=NOW)
        assert err is None and r["at"] == past


def test_the_checkable_trigger_vocabulary_matches_the_desk():
    """Set equality in BOTH directions.

    A length check passes while one name drifts into another, and the schema
    would then accept a trigger the desk cannot evaluate (a watch that can
    never fire) or refuse one it can (a watch that cannot be armed).
    """
    from app.services.watch_desk import VALID_TRIGGER_TYPES

    assert ws.CHECKABLE_TRIGGER_TYPES - VALID_TRIGGER_TYPES == set()
    assert VALID_TRIGGER_TYPES - ws.CHECKABLE_TRIGGER_TYPES == set()


class TestTheGenericTermTier:
    """Two generic words is not evidence that a headline resolves a question.

    Without this tier the live trip "Adobe Stock: Citi Sees 'Achievable Set-Up'
    Ahead Of Q3 Earnings" matched a Citigroup net-interest-margin condition on
    "q3" + "earnings" alone — the same false pass the old `_title_names_ticker`
    guard gave it, arrived at by a different route.
    """

    def test_generic_equity_vocabulary_is_not_discriminating(self):
        terms = ws.resolution_terms("Will Q3 earnings revenue guidance beat estimates?")
        assert ws.specific_terms(terms) == [], (
            "a question stated entirely in generic words has no specific tier")

    def test_a_real_question_keeps_its_discriminating_terms(self):
        terms = ws.resolution_terms(
            "Does Q3 net interest margin hold above 3.4%?",
            "Q3 net interest margin on the October earnings call")
        specific = ws.specific_terms(terms)
        assert "margin" in specific and "interest" in specific
        # …and drops the words every equity thesis shares.
        assert "earnings" not in specific and "q3" not in specific

    def test_the_specific_tier_is_stored_on_the_condition(self):
        """Derived once at write time. A second derivation at screen time would
        drift from this one and nothing would notice, because both would still
        return a plausible list."""
        rc, err = ws.normalize_resolution_condition(_rc())
        assert err is None
        assert rc["resolving_terms_specific"] == ws.specific_terms(rc["resolving_terms"])
        assert set(rc["resolving_terms_specific"]) <= set(rc["resolving_terms"])

    def test_the_specific_tier_is_a_strict_subset_never_an_expansion(self):
        for text in ("Q3 net interest margin above 3.4%",
                     "Will they beat earnings estimates?",
                     "Does the DRC export ban keep copper above $12k/t?"):
            terms = ws.resolution_terms(text)
            assert set(ws.specific_terms(terms)) <= set(terms)
