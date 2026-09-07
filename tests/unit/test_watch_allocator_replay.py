"""Replay: the seven scenarios, end to end, plus the frozen live trips.

Each scenario drives triage -> policy the way `watch_allocator.assess` does, and
asserts the OUTCOME rather than an intermediate number, so a rescoring that
preserves behaviour passes and one that changes behaviour fails.

The frozen half reads `tests/fixtures/watch_desk/live_trips.json`, captured once
from the live `watch_events` collection. Nothing in the allocator writes that
collection, so it stays an independent oracle. It must never be regenerated to
make a red go green — a fixture the code under test can rewrite turns the gate
into a tautology forever.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.services import watch_policy as wp
from app.services import watch_schema as ws
from app.services import watch_triage as wt

NOW = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures",
                       "watch_desk", "live_trips.json")


# ─── Harness ────────────────────────────────────────────────────────────────
def _watch(question, fact, *, confidence=0.5, action="HOLD", invalidates=None,
           expires_in_days=20, legacy=False):
    if legacy:
        return {"id": "w0", "ticker": "C", "thesis_summary": "prose only",
                "expiry_at": NOW + timedelta(days=expires_in_days)}
    rc = {"open_question": question, "resolving_fact": fact}
    if invalidates:
        rc["invalidates_if"] = invalidates
    dc, err = ws.normalize_decision_context(
        {"action": action, "confidence": confidence,
         "thesis_summary": "…", "resolution_condition": rc})
    assert err is None, err
    return {"id": "w1", "ticker": "C", "schema_version": 1, "decision_context": dc,
            "expiry_at": NOW + timedelta(days=expires_in_days)}


def _earnings_cal(hours_from_now):
    at = NOW + timedelta(hours=hours_from_now)
    return {"earnings_at": at, "earnings_confidence": "date_known_hour_assumed",
            "events": [{"kind": "earnings", "at": at, "confidence": "x",
                        "label": "C earnings", "source": "finviz"}],
            "sources": ["fundamentals"]}


def run(**over):
    """triage -> policy, exactly as watch_allocator.assess wires them."""
    defaults = dict(
        ticker="C",
        watch=_watch("Does Q3 net interest margin hold above 3.4%?",
                     "Q3 net interest margin on the October earnings call"),
        evidence=wt.Evidence(kind="news", text="Citigroup Q3 net interest margin beats",
                             observed_at=NOW - timedelta(hours=1),
                             source="detected", trigger_type="news"),
        now=NOW, market_open=True, calendar={}, budget_left=6, budget_total=6,
    )
    defaults.update({k: v for k, v in over.items()
                     if k in wt.TriageInputs.__dataclass_fields__})
    inp = wt.TriageInputs(**defaults)
    verdict = wt.triage(inp)
    decision = wp.apply_policy(wp.PolicyInputs(
        now=inp.now, verdict=verdict, planner_result=over.get("planner_result"),
        budget_left=inp.budget_left, budget_total=inp.budget_total,
        min_reanalysis_h=inp.min_reanalysis_h,
        analyses_this_week=inp.analyses_this_week,
        max_analyses_per_week=inp.max_analyses_per_week,
        last_analysis_at=inp.last_analysis_at,
        watch_expires_at=inp.watch.get("expiry_at"),
        market_open=inp.market_open, seen_event_keys=inp.seen_event_keys))
    return verdict, decision


# ─── 1. Earnings ────────────────────────────────────────────────────────────
class TestEarnings:
    def test_the_hours_after_a_release_are_the_moment_to_look(self):
        v, d = run(calendar=_earnings_cal(-6), held_qty=50, position_weight=0.05,
                   last_analysis_at=NOW - timedelta(days=10))
        assert d.action == wp.ACT_ANALYZE_NOW, (d.reason, v.components)

    def test_the_hours_before_a_release_are_not(self):
        """Analysing 6h before earnings spends a cycle on a thesis a known,
        imminent, unknowable fact is about to rewrite. The desk had no concept
        of this at all — the measured AGX wake fired on a TRANSCRIPT headline
        27 days after the prior look, i.e. after the numbers were public."""
        before = run(calendar=_earnings_cal(+6), budget_left=3)[0]
        after = run(calendar=_earnings_cal(-6), budget_left=3)[0]
        assert before.score < after.score

    def test_a_stale_earnings_date_does_not_read_as_no_catalyst(self):
        """EXLS's newest fundamentals row on 2026-09-06 still said 2026-07-28.
        A stale reading must raise a question, not suppress urgency."""
        stale = run(calendar={"earnings_at": None, "earnings_confidence": "stale",
                              "events": [], "sources": ["fundamentals"]})[0]
        absent = run(calendar={"earnings_at": None, "earnings_confidence": "absent",
                               "events": [], "sources": ["fundamentals"]})[0]
        assert stale.components["catalyst_urgency"] > absent.components["catalyst_urgency"]


# ─── 2. HOLD resolution ─────────────────────────────────────────────────────
class TestHoldResolution:
    def test_the_fact_that_settles_the_question_gets_the_wake(self):
        v, d = run(evidence=wt.Evidence(
            kind="news", text="Citigroup reports Q3 net interest margin of 3.55%",
            observed_at=NOW, source="detected", trigger_type="news"),
            last_analysis_at=NOW - timedelta(days=6))
        assert d.action == wp.ACT_ANALYZE_NOW
        assert v.detail["resolution_overlap"] > 0

    def test_a_headline_about_the_company_but_not_the_question_is_refused(self):
        """THE measured defect. `_title_names_ticker` passes this — it asks only
        'is this about the company'. It flipped a live HOLD to BUY."""
        v, d = run(evidence=wt.Evidence(
            kind="news", text="Citigroup delays Fed rate cut forecast to June 2027",
            observed_at=NOW, source="detected", trigger_type="news"))
        assert v.reject_reason == wt.REJECT_OFF_THESIS
        assert d.action == wp.ACT_DEFER

    def test_a_conference_sponsorship_notice_is_refused(self):
        """Live trip: 'The New York Times Company's CFO to Participate in the
        Citi 2026 Global TMT' woke a trade-enabled cycle for C."""
        v, _ = run(evidence=wt.Evidence(
            kind="news",
            text="The New York Times Company's CFO to Participate in the Citi 2026 Global TMT",
            observed_at=NOW, source="detected", trigger_type="news"))
        assert v.reject_reason == wt.REJECT_OFF_THESIS

    def test_an_analyst_action_naming_the_bank_as_actor_is_refused(self):
        """Live trip: 'Adobe Stock: Citi Sees Achievable Set-Up…' — right company
        name, wrong company's news."""
        v, _ = run(evidence=wt.Evidence(
            kind="news",
            text="Adobe Stock: Citi Sees 'Achievable Set-Up' Ahead Of Q3 Earnings, Raises Price Target",
            observed_at=NOW, source="detected", trigger_type="news"))
        assert v.reject_reason == wt.REJECT_OFF_THESIS


# ─── 3. Price invalidation ──────────────────────────────────────────────────
class TestPriceInvalidation:
    def test_a_breached_stop_on_a_held_position_wins_the_last_wake(self):
        """Zero of the last 30 trips were price triggers. A breached
        invalidation level must be able to take the day's final wake from a
        headline."""
        v, d = run(budget_left=1, budget_total=6, held_qty=100,
                   position_weight=0.09, unrealized_pct=-0.15,
                   last_analysis_at=NOW - timedelta(days=20),
                   watch=_watch("Does the copper thesis hold?", "Copper below $12k/t",
                                confidence=0.35,
                                invalidates={"type": "price_below", "level": 24.81}),
                   calendar=_earnings_cal(-8),
                   evidence=wt.Evidence(kind="price", text="C price $59.10 <= $61.00",
                                        observed_at=NOW, source="price",
                                        trigger_type="price_below"))
        assert d.action == wp.ACT_ANALYZE_NOW, (d.reason, v.score, v.components)

    def test_a_headline_cannot_take_that_same_last_wake(self):
        v, d = run(budget_left=1, budget_total=6,
                   last_analysis_at=NOW - timedelta(days=9))
        assert d.action == wp.ACT_DEFER
        assert "below_score_floor" in (v.reject_reason or d.reason)


# ─── 4. Duplicated events ───────────────────────────────────────────────────
class TestDuplicatedEvents:
    def test_the_same_headline_cannot_wake_twice(self):
        ev = wt.Evidence(kind="news", text="Citigroup Q3 net interest margin beats",
                         observed_at=NOW - timedelta(hours=1), source="detected",
                         trigger_type="news")
        first_v, first_d = run(evidence=ev)
        assert first_d.action == wp.ACT_ANALYZE_NOW
        _, second_d = run(evidence=ev, seen_event_keys=frozenset({first_v.event_key}))
        assert second_d.action == wp.ACT_DEFER and second_d.reason == "duplicate_event"

    def test_the_key_survives_a_reworded_wrapper(self):
        """The same story republished with different punctuation is the same
        world-event. A key that missed it would let one story wake four cycles
        — which is what happened to NVDA in an hour."""
        a = wt.Evidence(kind="news", text="Citigroup Q3 net interest margin beats",
                        observed_at=NOW, source="detected", trigger_type="news")
        b = wt.Evidence(kind="news", text="CITIGROUP  Q3, NET INTEREST MARGIN BEATS!!",
                        observed_at=NOW, source="detected", trigger_type="news")
        assert a.event_key("C") == b.event_key("C")


# ─── 5. Stale data ──────────────────────────────────────────────────────────
class TestStaleData:
    def test_an_old_headline_cannot_wake_a_cycle(self):
        _, d = run(evidence=wt.Evidence(
            kind="news", text="Citigroup Q3 net interest margin beats",
            observed_at=NOW - timedelta(days=9), source="detected",
            trigger_type="news"))
        assert d.action == wp.ACT_DEFER and d.reason == wt.REJECT_STALE_EVIDENCE

    def test_freshness_is_measured_from_the_world_not_from_when_we_looked(self):
        """If `observed_at` fell back to now, everything would be permanently
        fresh and this gate would be a decoration."""
        old = wt.Evidence(kind="news", text="x", observed_at=NOW - timedelta(days=9),
                          source="detected", trigger_type="news")
        assert old.observed_at < NOW
        _, d = run(evidence=old)
        assert d.reason == wt.REJECT_STALE_EVIDENCE


# ─── 6. Market closure ──────────────────────────────────────────────────────
class TestMarketClosure:
    def test_a_price_trigger_cannot_fire_out_of_session(self):
        """Off-hours fast_info returns a stale last close, which would fire an
        already-breached level overnight and wake a cycle that cannot trade."""
        _, d = run(market_open=False, evidence=wt.Evidence(
            kind="price", text="stop hit", observed_at=NOW, source="price",
            trigger_type="price_below"))
        assert d.action == wp.ACT_DEFER and d.reason == wt.REJECT_MARKET_CLOSED

    def test_news_still_evaluates_out_of_session(self):
        """Most material news breaks outside the session. Blocking it would
        silence the desk for two thirds of every day."""
        _, d = run(market_open=False, last_analysis_at=NOW - timedelta(days=6))
        assert d.action == wp.ACT_ANALYZE_NOW


# ─── 7. The trip whose best answer is "do nothing" ──────────────────────────
class TestDoNothing:
    def test_a_low_value_trip_on_a_busy_day_defers_and_says_why(self):
        v, d = run(budget_left=2, budget_total=6,
                   last_analysis_at=NOW - timedelta(hours=14),
                   evidence=wt.Evidence(kind="price", text="RSI 71", observed_at=NOW,
                                        source="price", trigger_type="rsi"))
        assert d.action == wp.ACT_DEFER
        assert d.reason, "a deferral with no reason cannot be audited"
        assert v.components, "a deferral must still carry its score components"

    def test_doing_nothing_costs_no_budget(self):
        """Charging a deferral would reproduce the measured defect from the
        other side: on 2026-09-02 six wakes were charged and six decisions were
        not made."""
        _, d = run(budget_left=2, last_analysis_at=NOW - timedelta(hours=2))
        assert d.action == wp.ACT_DEFER
        assert d.at is None and d.close_reason is None

    def test_the_planner_may_answer_keep_watching_with_no_budget_left(self):
        _, d = run(budget_left=0, planner_result={
            "action": "WATCH_FOR_CONDITION",
            "condition": {"type": "price_below", "level": 61.0},
            "until": NOW + timedelta(days=3)})
        assert d.action == wp.ACT_WATCH_FOR_CONDITION


# ─── The frozen live trips ──────────────────────────────────────────────────
@pytest.fixture(scope="module")
def live():
    if not os.path.exists(FIXTURE):
        pytest.skip(f"fixture not captured: {FIXTURE}")
    with open(FIXTURE) as f:
        return json.load(f)


class TestTheFrozenBaseline:
    """These pin the MEASURED starting point the plan's success gates are
    written against. They are not assertions about the allocator — they are the
    evidence it exists to improve, frozen so a later 'we fixed it' can be
    checked against something the fix cannot rewrite.
    """

    def test_the_fixture_is_the_captured_window_not_a_regeneration(self, live):
        assert live["trips"], "an empty fixture proves nothing"
        assert "captured_at" in live
        assert "Do not regenerate" in live["note"]

    def test_the_measured_composition_was_news_and_clocks_only(self, live):
        types = live["summary"]["trigger_types"]
        assert types.get("news", 0) + types.get("staleness", 0) == live["summary"]["n"]
        assert "price_below" not in types and "price_above" not in types, (
            "zero price triggers fired in the captured window — the levels the "
            "desk chose for itself never tripped")

    def test_the_measured_no_decision_rate_was_over_a_quarter(self, live):
        assert live["summary"]["no_decision_rate"] >= 0.25

    def test_no_captured_watch_had_a_resolution_condition(self, live):
        """0/30. This is the gap the contract closes, and the number the
        coverage gate in the plan is measured against."""
        assert live["summary"]["resolution_condition_coverage"] == 0.0
        assert all(not t["watch_has_resolution_condition"] for t in live["trips"])

    def test_the_budget_was_saturated_every_captured_day(self, live):
        """The finding that reframed the task: the desk was not idle with
        occasional trips, it was full every single day and allocating by
        arrival time."""
        counts = list(live["daily_wake_counts"].values())
        assert len(counts) >= 14, "too few days to claim saturation"
        assert len(set(counts)) == 1, f"expected a flat, saturated budget: {counts}"
        assert counts[0] == 6

    def test_every_captured_trip_would_now_be_screenable_or_explicitly_not(self, live):
        """Each legacy trip must land in exactly one of two buckets: it carries
        no resolution condition (so it is scored with the legacy penalty and
        never screened off-thesis), or it carries one (so it is screened).
        A third state — screened against an empty condition — is the fail-open
        bug this asserts cannot exist.
        """
        for t in live["trips"]:
            watch = {"id": t["watch_id"], "ticker": t["ticker"]}
            if t["watch_has_resolution_condition"]:
                continue
            assert ws.get_resolution_condition(watch) is None
            v = wt.triage(wt.TriageInputs(
                ticker=t["ticker"], watch=watch,
                evidence=wt.Evidence(kind="news", text=t["detail"] or "",
                                     observed_at=NOW, source="detected",
                                     trigger_type=t["trigger_type"] or "news"),
                now=NOW, budget_left=6, budget_total=6))
            assert v.reject_reason != wt.REJECT_OFF_THESIS, (
                f"{t['ticker']}: a watch with no stored condition must not be "
                f"screened against one")
            assert v.components["legacy_schema_penalty"] < 0
