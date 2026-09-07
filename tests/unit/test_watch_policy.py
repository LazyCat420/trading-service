"""The policy layer: the model recommends, code decides.

The standing risk this file guards is a research agent that creates unlimited
immediate work for itself. Every assertion below is about a bound the LLM cannot
talk its way past — and about CLAMPING rather than refusing, because refusing an
out-of-bounds recommendation throws away the judgement along with the overreach.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import watch_policy as wp
from app.services.watch_triage import TriageVerdict

NOW = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)


def _verdict(eligible=True, score=3.5, reason=None, key="k1"):
    return TriageVerdict(eligible=eligible, reject_reason=reason, score=score,
                         components={"materiality": score}, detail={}, event_key=key)


def _inp(**over):
    base = dict(now=NOW, verdict=_verdict(), planner_result=None,
                budget_left=6, budget_total=6, min_reanalysis_h=12,
                analyses_this_week=0, max_analyses_per_week=3,
                last_analysis_at=None,
                watch_expires_at=NOW + timedelta(days=20), market_open=True)
    base.update(over)
    return wp.PolicyInputs(**base)


class TestTheDeterministicPath:
    def test_an_eligible_affordable_trip_analyses_now(self):
        d = wp.apply_policy(_inp())
        assert d.action == wp.ACT_ANALYZE_NOW and d.source == "deterministic"

    def test_an_ineligible_trip_defers_with_the_triage_reason(self):
        d = wp.apply_policy(_inp(verdict=_verdict(eligible=False, reason="evidence_off_thesis")))
        assert d.action == wp.ACT_DEFER and d.reason == "evidence_off_thesis"

    def test_an_exhausted_global_budget_defers(self):
        d = wp.apply_policy(_inp(budget_left=0))
        assert d.action == wp.ACT_DEFER and d.reason == "global_daily_budget_exhausted"

    def test_an_exhausted_ticker_budget_defers(self):
        d = wp.apply_policy(_inp(analyses_this_week=3, max_analyses_per_week=3))
        assert d.action == wp.ACT_DEFER and d.reason == "ticker_weekly_budget_exhausted"

    def test_the_cadence_floor_defers(self):
        d = wp.apply_policy(_inp(last_analysis_at=NOW - timedelta(hours=3)))
        assert d.action == wp.ACT_DEFER and d.reason == "below_cadence_floor"

    def test_a_duplicate_event_defers(self):
        d = wp.apply_policy(_inp(seen_event_keys=frozenset({"k1"})))
        assert d.action == wp.ACT_DEFER and d.reason == "duplicate_event"

    def test_the_score_floor_applies_to_the_deterministic_path_too(self):
        """One seam. If the floor lived only in triage, a caller that built a
        verdict another way would slip past it."""
        d = wp.apply_policy(_inp(budget_left=1, verdict=_verdict(score=1.0)))
        assert d.action == wp.ACT_DEFER and "below_score_floor" in d.reason


class TestThePlannerIsAdviceNotAuthority:
    def test_a_schedule_sooner_than_the_cadence_floor_is_clamped_not_obeyed(self):
        """The model asks for 1h; the floor is 12h from the last look."""
        d = wp.apply_policy(_inp(
            last_analysis_at=NOW - timedelta(hours=1),
            planner_result={"action": "SCHEDULE_AT", "at": NOW + timedelta(hours=1)}))
        assert d.action == wp.ACT_SCHEDULE_AT
        assert d.at == NOW - timedelta(hours=1) + timedelta(hours=12)
        assert any(c["why"] == "sooner than the cadence floor" for c in d.clamps)

    def test_a_schedule_past_the_watch_expiry_is_clamped_to_it(self):
        """A timer for a record that will not exist fires, finds nothing, and
        the reason the research existed is gone."""
        expiry = NOW + timedelta(days=5)
        d = wp.apply_policy(_inp(
            watch_expires_at=expiry,
            planner_result={"action": "SCHEDULE_AT", "at": NOW + timedelta(days=90)}))
        assert d.at == expiry
        assert any("expiry" in c["why"] for c in d.clamps)

    def test_a_schedule_beyond_the_horizon_cap_is_clamped_even_with_no_expiry(self):
        d = wp.apply_policy(_inp(
            watch_expires_at=None,
            planner_result={"action": "SCHEDULE_AT", "at": NOW + timedelta(days=900)}))
        assert d.at <= NOW + timedelta(days=wp.MAX_SCHEDULE_HORIZON_DAYS)

    def test_analyze_now_is_DOWNGRADED_not_refused_when_unaffordable(self):
        """The planner judged the SUBJECT worth looking at; only its timing was
        unaffordable. Keeping the judgement and dropping the overreach is the
        whole design — and the result is bounded, so it cannot loop."""
        d = wp.apply_policy(_inp(
            budget_left=0, planner_result={"action": "ANALYZE_NOW"}))
        assert d.action == wp.ACT_SCHEDULE_AT
        assert d.reason.startswith("downgraded:")
        assert any(c["field"] == "action" for c in d.clamps)

    def test_analyze_now_is_honoured_when_it_is_affordable(self):
        d = wp.apply_policy(_inp(planner_result={"action": "ANALYZE_NOW"}))
        assert d.action == wp.ACT_ANALYZE_NOW and d.source == "planner"

    def test_an_unknown_action_defers_rather_than_raising(self):
        """A contract violation is not a decision, and the safe direction is
        always 'do less'. Raising here would let a malformed model response
        stop the whole sweep."""
        d = wp.apply_policy(_inp(planner_result={"action": "LIQUIDATE_EVERYTHING"}))
        assert d.action == wp.ACT_DEFER and "unknown_planner_action" in d.reason

    def test_there_is_no_path_that_schedules_sooner_than_the_cadence_floor(self):
        """The guardrail, stated exhaustively rather than by example."""
        floor_from = NOW - timedelta(hours=2)
        for pr in (
            {"action": "ANALYZE_NOW"},
            {"action": "SCHEDULE_AT", "at": NOW},
            {"action": "SCHEDULE_AT", "at": NOW - timedelta(days=30)},
            {"action": "SCHEDULE_AT"},                     # no time at all
        ):
            d = wp.apply_policy(_inp(last_analysis_at=floor_from, planner_result=pr,
                                     budget_left=0))
            assert d.action in (wp.ACT_SCHEDULE_AT, wp.ACT_DEFER)
            if d.at is not None:
                assert d.at >= floor_from + timedelta(hours=12), (pr, d.at)


class TestTheCheapActionsAreNotBudgeted:
    def test_close_watch_needs_no_budget(self):
        """The only action that REDUCES work is the one we least want to
        obstruct."""
        d = wp.apply_policy(_inp(
            budget_left=0, analyses_this_week=99,
            planner_result={"action": "CLOSE_WATCH", "close_reason": "question settled"}))
        assert d.action == wp.ACT_CLOSE_WATCH
        assert d.close_reason == "question settled"

    def test_watch_for_condition_is_allowed_with_no_budget(self):
        """'Keep watching instead of analysing' is exactly the right answer
        when the budget is gone — refusing it there would be perverse."""
        d = wp.apply_policy(_inp(
            budget_left=0,
            planner_result={"action": "WATCH_FOR_CONDITION",
                            "condition": {"type": "price_below", "level": 61.0},
                            "until": NOW + timedelta(days=3)}))
        assert d.action == wp.ACT_WATCH_FOR_CONDITION
        assert d.condition["level"] == 61.0

    def test_watch_for_condition_until_is_still_clamped_to_the_expiry(self):
        expiry = NOW + timedelta(days=2)
        d = wp.apply_policy(_inp(
            watch_expires_at=expiry,
            planner_result={"action": "WATCH_FOR_CONDITION",
                            "condition": {"type": "price_below", "level": 61.0},
                            "until": NOW + timedelta(days=60)}))
        assert d.at == expiry


class TestImpossibleWindows:
    def test_a_cadence_floor_past_the_expiry_defers_rather_than_scheduling(self):
        """There is no legal moment. Emitting a schedule anyway would produce a
        timer that can never legally run — which reads as 'handled'."""
        d = wp.apply_policy(_inp(
            last_analysis_at=NOW, min_reanalysis_h=48,
            watch_expires_at=NOW + timedelta(hours=6),
            planner_result={"action": "SCHEDULE_AT", "at": NOW + timedelta(hours=1)}))
        assert d.action == wp.ACT_DEFER and d.reason == "no_legal_schedule_window"

    def test_the_same_holds_for_a_downgraded_analyze_now(self):
        d = wp.apply_policy(_inp(
            budget_left=0, last_analysis_at=NOW, min_reanalysis_h=48,
            watch_expires_at=NOW + timedelta(hours=6),
            planner_result={"action": "ANALYZE_NOW"}))
        assert d.action == wp.ACT_DEFER and "no_legal_schedule_window" in d.reason


def test_the_decision_carries_the_score_components_through():
    """The dashboard's 'why it ran or deferred' reads these off the decision.
    A decision that dropped them would leave the column permanently blank."""
    v = _verdict(score=2.75)
    d = wp.apply_policy(_inp(verdict=v))
    assert d.score == 2.75 and d.components == v.components
    assert "components" in d.as_doc()
