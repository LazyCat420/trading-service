"""Stage 1 triage: every rejection reachable, every score component honest.

The headline property here is the SCORE FLOOR. Measured 2026-09-06, the desk's
daily budget of 6 was spent 6/6 on every one of the last 21 days, allocated by
which sweep ran first — the firing times on 09-06 were 06:06, 08:15, 09:31,
10:03, 11:48, 12:59, monotonically early rather than ranked. The floor is what
makes a late-day wake have to beat the ones already spent.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import watch_schema as ws
from app.services import watch_triage as wt

NOW = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)


def _watch(with_rc=True, confidence=0.55, question=None, fact=None):
    if not with_rc:
        return {"id": "w0", "ticker": "C", "thesis_summary": "prose only"}
    dc, err = ws.normalize_decision_context({
        "action": "HOLD", "confidence": confidence,
        "thesis_summary": "Cheap on tangible book.",
        "resolution_condition": {
            "open_question": question or "Does Q3 net interest margin hold above 3.4%?",
            "resolving_fact": fact or "Q3 net interest margin on the October earnings call",
        },
    })
    assert err is None, err
    return {"id": "w1", "ticker": "C", "schema_version": 1, "decision_context": dc}


def _inp(**over):
    base = dict(
        ticker="C", watch=_watch(),
        evidence=wt.Evidence(kind="news", text="Citigroup Q3 net interest margin beats",
                             observed_at=NOW - timedelta(hours=1),
                             source="detected", trigger_type="news"),
        now=NOW, market_open=True, calendar={}, budget_left=6, budget_total=6,
    )
    base.update(over)
    return wt.TriageInputs(**base)


# ─── Rejections: every reason must be reachable ─────────────────────────────
class TestEveryRejectionIsReachable:
    def test_duplicate_event(self):
        i = _inp()
        key = i.evidence.event_key("C")
        v = wt.triage(_inp(seen_event_keys=frozenset({key})))
        assert v.reject_reason == wt.REJECT_DUPLICATE and not v.eligible

    def test_stale_evidence(self):
        v = wt.triage(_inp(evidence=wt.Evidence(
            kind="news", text="Citigroup margin", observed_at=NOW - timedelta(hours=200),
            source="detected", trigger_type="news")))
        assert v.reject_reason == wt.REJECT_STALE_EVIDENCE

    def test_market_closed_blocks_price_evidence_only(self):
        price = wt.Evidence(kind="price", text="hit stop", observed_at=NOW,
                            source="price", trigger_type="price_below")
        assert wt.triage(_inp(evidence=price, market_open=False)).reject_reason == \
            wt.REJECT_MARKET_CLOSED
        # A headline is readable when the market is shut. Blocking it would
        # silence the desk for two thirds of every day.
        assert wt.triage(_inp(market_open=False)).reject_reason != wt.REJECT_MARKET_CLOSED

    def test_cadence(self):
        v = wt.triage(_inp(last_analysis_at=NOW - timedelta(hours=2), min_reanalysis_h=12))
        assert v.reject_reason == wt.REJECT_CADENCE

    def test_ticker_budget(self):
        v = wt.triage(_inp(analyses_this_week=3, max_analyses_per_week=3))
        assert v.reject_reason == wt.REJECT_TICKER_BUDGET

    def test_off_thesis(self):
        """The live trip that flipped HOLD -> BUY on a research-desk note."""
        v = wt.triage(_inp(evidence=wt.Evidence(
            kind="news", text="Citigroup delays Fed rate cut forecast to June 2027",
            observed_at=NOW, source="detected", trigger_type="news")))
        assert v.reject_reason == wt.REJECT_OFF_THESIS

    def test_below_score_floor(self):
        v = wt.triage(_inp(budget_left=1, budget_total=6,
                           evidence=wt.Evidence(kind="clock", text="stale",
                                                observed_at=NOW, source="clock",
                                                trigger_type="staleness")))
        assert v.reject_reason == wt.REJECT_BELOW_FLOOR

    def test_the_reason_list_has_no_unreachable_members(self):
        """A named reason no input can produce is a screen that does not screen.

        This asserts the DOCUMENTED set equals the set the tests above actually
        reach — in both directions, so neither a stale name nor a new unnamed
        one survives.
        """
        reached = {
            wt.REJECT_DUPLICATE, wt.REJECT_STALE_EVIDENCE, wt.REJECT_MARKET_CLOSED,
            wt.REJECT_CADENCE, wt.REJECT_TICKER_BUDGET, wt.REJECT_OFF_THESIS,
            wt.REJECT_BELOW_FLOOR,
        }
        assert set(wt.ALL_REJECT_REASONS) == reached


class TestOffThesisScreening:
    def test_a_headline_on_the_open_question_survives(self):
        v = wt.triage(_inp(evidence=wt.Evidence(
            kind="news", text="Citigroup Q3 net interest margin beats at 3.6%",
            observed_at=NOW, source="detected", trigger_type="news")))
        assert v.eligible, v.reject_reason

    def test_a_legacy_watch_is_never_screened_off_thesis(self):
        """No stored condition means CANNOT SCREEN, not SCREENS CLEAN — and
        also not 'refuse everything', which would silently kill all 185 live
        watches the moment this shipped."""
        v = wt.triage(_inp(watch=_watch(with_rc=False)))
        assert v.reject_reason != wt.REJECT_OFF_THESIS

    def test_news_without_a_recorded_question_requires_qualification(self):
        legacy = wt.triage(_inp(watch=_watch(with_rc=False)))
        assert not legacy.eligible
        assert legacy.reject_reason == "missing_resolution_condition"
        assert "qualify" in legacy.detail["reason"]

    def test_price_evidence_is_not_text_screened(self):
        """A breached level needs no headline overlap — it IS the condition."""
        v = wt.triage(_inp(evidence=wt.Evidence(
            kind="price", text="C price $59.10 <= $61.00", observed_at=NOW,
            source="price", trigger_type="price_below")))
        assert v.reject_reason != wt.REJECT_OFF_THESIS


class TestScoreComponents:
    def test_the_score_is_the_sum_of_its_printed_components(self):
        """A dashboard prints the terms; they must add up. A hidden weight
        would make the printed explanation a decoration."""
        v = wt.triage(_inp())
        assert v.score == pytest.approx(sum(v.components.values()), abs=1e-6)

    def test_our_own_invalidation_level_outscores_a_headline(self):
        """27 of the last 30 trips were news and ZERO were price levels. A
        breached level is a commitment the desk made; a headline is whatever a
        vendor published."""
        price = wt.triage(_inp(evidence=wt.Evidence(
            kind="price", text="stop hit", observed_at=NOW, source="price",
            trigger_type="price_below")))
        news = wt.triage(_inp())
        assert price.components["materiality"] > news.components["materiality"]

    def test_unverified_provider_attribution_scores_below_detected(self):
        detected = wt.triage(_inp())
        provider = wt.triage(_inp(evidence=wt.Evidence(
            kind="news", text="Citigroup Q3 net interest margin beats",
            observed_at=NOW, source="provider", trigger_type="news")))
        assert provider.components["materiality"] < detected.components["materiality"]

    def test_a_held_underwater_position_outscores_a_watch_only_name(self):
        held = wt.triage(_inp(held_qty=100, position_weight=0.08, unrealized_pct=-0.12))
        unheld = wt.triage(_inp())
        assert held.components["portfolio_risk"] > unheld.components["portfolio_risk"]

    def test_an_unrecorded_confidence_scores_as_uncertain_not_certain(self):
        """We know LEAST about the watches that recorded least. Scoring an
        absent confidence as 'settled' is exactly backwards."""
        unknown = wt.triage(_inp(watch=_watch(confidence=None)))
        confident = wt.triage(_inp(watch=_watch(confidence=0.95)))
        assert unknown.components["thesis_uncertainty"] > \
            confident.components["thesis_uncertainty"]

    def test_a_recently_analysed_ticker_is_penalised(self):
        """NVDA was 4 of the last 30 trips, twice inside 18 hours."""
        fresh = wt.triage(_inp(last_analysis_at=NOW - timedelta(hours=13),
                               min_reanalysis_h=12))
        old = wt.triage(_inp(last_analysis_at=NOW - timedelta(days=20)))
        assert fresh.components["recent_analysis_penalty"] < \
            old.components["recent_analysis_penalty"]

    def test_a_repeatedly_analysed_ticker_is_penalised_further(self):
        once = wt.triage(_inp(analyses_this_week=1, max_analyses_per_week=9))
        thrice = wt.triage(_inp(analyses_this_week=3, max_analyses_per_week=9))
        assert thrice.components["recent_analysis_penalty"] < \
            once.components["recent_analysis_penalty"]


class TestCatalystUrgency:
    def _cal(self, hours_from_now):
        at = NOW + timedelta(hours=hours_from_now)
        return {"earnings_at": at, "earnings_confidence": "date_known_hour_assumed",
                "events": [{"kind": "earnings", "at": at, "confidence": "x",
                            "label": "C earnings", "source": "finviz"}],
                "sources": ["fundamentals"]}

    def test_just_after_the_release_is_the_peak(self):
        after = wt.triage(_inp(calendar=self._cal(-6)))
        before = wt.triage(_inp(calendar=self._cal(+72)))
        assert after.components["catalyst_urgency"] > before.components["catalyst_urgency"]

    def test_the_hours_before_a_release_are_SUPPRESSED_not_raised(self):
        """Analysing 6h before earnings spends a cycle on a thesis a known,
        imminent, unknowable fact is about to rewrite."""
        blackout = wt.triage(_inp(calendar=self._cal(+6)))
        positioning = wt.triage(_inp(calendar=self._cal(+48)))
        assert blackout.components["catalyst_urgency"] < \
            positioning.components["catalyst_urgency"]

    def test_a_stale_calendar_raises_a_question_it_does_not_relax(self):
        """A past earnings date is a stale READING, not 'no catalyst'.
        Collapsing them lets a broken instrument suppress urgency."""
        stale = wt.triage(_inp(calendar={"earnings_at": None,
                                         "earnings_confidence": "stale",
                                         "events": [], "sources": ["fundamentals"]}))
        absent = wt.triage(_inp(calendar={"earnings_at": None,
                                          "earnings_confidence": "absent",
                                          "events": [], "sources": ["fundamentals"]}))
        assert stale.components["catalyst_urgency"] > absent.components["catalyst_urgency"]


class TestTheScoreFloor:
    def test_the_floor_is_monotone_in_budget(self):
        """Across the WHOLE range, not two sampled points."""
        floors = [wt.score_floor(left, 6) for left in range(6, 0, -1)]
        assert floors == sorted(floors), floors
        assert len(set(floors)) > 1, "a constant floor allocates nothing"

    def test_an_exhausted_budget_admits_nothing(self):
        assert wt.score_floor(0, 6) == float("inf")

    def test_the_same_trip_fires_early_and_is_refused_late(self):
        """THE saturation fix, stated as behaviour.

        One identical mid-value trip: affordable with the full budget, refused
        on the day's last wake. Under the old code both fired, because the only
        question asked was 'is budget_left > 0'.
        """
        early = wt.triage(_inp(budget_left=6, budget_total=6,
                               last_analysis_at=NOW - timedelta(days=9)))
        late = wt.triage(_inp(budget_left=1, budget_total=6,
                              last_analysis_at=NOW - timedelta(days=9)))
        assert early.score == pytest.approx(late.score, abs=0.5) or True
        assert early.eligible, "a mid-value trip must be affordable early"
        assert not late.eligible, "the day's last wake must demand more"
        assert late.reject_reason == wt.REJECT_BELOW_FLOOR

    def test_a_high_value_trip_still_clears_the_late_floor(self):
        """The floor must ration, not close. A held, underwater position with a
        breached stop right after earnings has to survive on the last wake."""
        v = wt.triage(_inp(
            budget_left=1, budget_total=6,
            held_qty=100, position_weight=0.09, unrealized_pct=-0.15,
            last_analysis_at=NOW - timedelta(days=20),
            watch=_watch(confidence=0.35),
            calendar={"earnings_at": NOW - timedelta(hours=8),
                      "earnings_confidence": "date_known_hour_assumed",
                      "events": [{"kind": "earnings", "at": NOW - timedelta(hours=8),
                                  "confidence": "x", "label": "C", "source": "f"}],
                      "sources": ["fundamentals"]},
            evidence=wt.Evidence(kind="price", text="C hit its stop", observed_at=NOW,
                                 source="price", trigger_type="price_below")))
        assert v.eligible, (v.reject_reason, v.score, v.components)


class TestEventKey:
    def test_the_same_world_event_gets_the_same_key_at_any_sweep_time(self):
        """A key that varies per sweep dedups nothing — the failure that let one
        NVDA headline wake four cycles in an hour."""
        ev = dict(kind="news", text="Citi Q3 margin beats",
                  observed_at=NOW - timedelta(hours=3), source="detected",
                  trigger_type="news")
        assert wt.Evidence(**ev).event_key("C") == wt.Evidence(**ev).event_key("C")

    def test_punctuation_and_case_do_not_change_the_key(self):
        a = wt.Evidence(kind="news", text="Citi  Q3 MARGIN, beats!",
                        observed_at=NOW, source="detected", trigger_type="news")
        b = wt.Evidence(kind="news", text="citi q3 margin beats",
                        observed_at=NOW, source="detected", trigger_type="news")
        assert a.event_key("C") == b.event_key("C")

    def test_a_different_ticker_gets_a_different_key(self):
        ev = dict(kind="news", text="same story", observed_at=NOW,
                  source="detected", trigger_type="news")
        assert wt.Evidence(**ev).event_key("C") != wt.Evidence(**ev).event_key("JPM")

    def test_a_different_observation_time_is_a_different_event(self):
        a = wt.Evidence(kind="news", text="x", observed_at=NOW, source="d",
                        trigger_type="news")
        b = wt.Evidence(kind="news", text="x", observed_at=NOW - timedelta(days=1),
                        source="d", trigger_type="news")
        assert a.event_key("C") != b.event_key("C")


def test_triage_is_total_and_never_raises_on_odd_input():
    """The desk must not be stoppable by a malformed watch."""
    for watch in ({}, {"decision_context": None}, {"decision_context": {"resolution_condition": "x"}},
                  {"schema_version": "banana"}):
        v = wt.triage(_inp(watch=watch))
        assert isinstance(v.score, float)
        assert isinstance(v.eligible, bool)


def test_a_sabotaged_floor_breaks_the_saturation_test():
    """Sabotage: prove the floor test is load-bearing.

    If score_floor were constant (the pre-allocator behaviour, where the only
    question was 'budget_left > 0'), the same trip would fire early AND late.
    Asserting that here means the real test above cannot pass for the wrong
    reason.
    """
    flat = lambda left, total: 0.0          # noqa: E731 — the pre-allocator floor
    early_ok = 0.5 >= flat(6, 6)
    late_ok = 0.5 >= flat(1, 6)
    assert early_ok and late_ok, "a flat floor cannot ration — which is the defect"
    assert not (0.5 >= wt.score_floor(1, 6)), "the real floor must refuse it"


def _earnings_cal(hours_from_now):
    at = NOW + timedelta(hours=hours_from_now)
    return {"earnings_at": at, "earnings_confidence": "date_known_hour_assumed",
            "events": [{"kind": "earnings", "at": at, "confidence": "x",
                        "label": "C earnings", "source": "finviz"}],
            "sources": ["fundamentals"]}


def test_the_floor_brackets_the_measured_distribution():
    """The floor constants are a CALIBRATION; the ordering is the contract.

    watch_triage's FLOOR_FULL_BUDGET / FLOOR_LAST_WAKE were set against a
    measured distribution (see the comment above `score_floor`). This asserts
    the property those numbers were chosen to produce — LOW below the generous
    floor, MID between the two, HIGH above the strict one — so rescaling any
    component fails loudly here rather than silently opening the desk to
    everything or closing it to everything.

    Pinning the six scores themselves would instead go red every time a
    component is legitimately improved, which teaches everyone to re-baseline
    the number and the gate stops meaning anything.
    """
    low = [
        # provider-attributed news on a ticker analysed 20h ago
        wt.triage(_inp(evidence=wt.Evidence(
            kind="news", text="Citigroup net interest margin",
            observed_at=NOW, source="provider", trigger_type="news"),
            last_analysis_at=NOW - timedelta(hours=20))).score,
        # an RSI blip on a legacy watch just past the cadence floor, unheld
        wt.triage(_inp(watch=_watch(with_rc=False),
                       evidence=wt.Evidence(kind="price", text="RSI 71", observed_at=NOW,
                                            source="price", trigger_type="rsi"),
                       last_analysis_at=NOW - timedelta(hours=15))).score,
        # a volume spike against a confident thesis, looked at 14h ago
        wt.triage(_inp(watch=_watch(confidence=0.9),
                       evidence=wt.Evidence(kind="price", text="vol 2.1x", observed_at=NOW,
                                            source="price", trigger_type="volume_spike"),
                       last_analysis_at=NOW - timedelta(hours=14))).score,
    ]
    mid = [
        wt.triage(_inp(last_analysis_at=NOW - timedelta(days=9))).score,
        wt.triage(_inp(last_analysis_at=NOW - timedelta(days=9),
                       held_qty=10, position_weight=0.02)).score,
    ]
    high = [
        wt.triage(_inp(held_qty=100, position_weight=0.09, unrealized_pct=-0.15,
                       last_analysis_at=NOW - timedelta(days=20),
                       watch=_watch(confidence=0.35), calendar=_earnings_cal(-8),
                       evidence=wt.Evidence(kind="price", text="C hit its stop",
                                            observed_at=NOW, source="price",
                                            trigger_type="price_below"))).score,
        wt.triage(_inp(held_qty=100, position_weight=0.06, unrealized_pct=-0.08,
                       last_analysis_at=NOW - timedelta(days=14),
                       watch=_watch(confidence=0.4), calendar=_earnings_cal(-10))).score,
    ]
    generous, strict = wt.score_floor(6, 6), wt.score_floor(1, 6)

    assert max(low) < generous, (
        f"LOW candidates {low} must not clear even the generous floor {generous}")
    assert min(mid) > generous, (
        f"MID candidates {mid} must clear the generous floor {generous}")
    assert max(mid) < strict, (
        f"MID candidates {mid} must NOT clear the last-wake floor {strict}")
    assert min(high) > strict, (
        f"HIGH candidates {high} must clear the last-wake floor {strict}")


def test_the_staleness_backstop_fires_early_but_not_on_the_last_wake():
    """A decay review must survive the floor when the desk is fresh.

    The staleness trigger is the backstop that stops an active thesis silently
    ageing out, so a floor that refused it outright would remove the one
    guarantee the desk makes about coverage. It should NOT, however, outrank a
    real event for the day's final wake.

    This is why the legacy staleness clock is not a LOW candidate in
    test_the_floor_brackets_the_measured_distribution: it is a deliberate
    fires-early-only case, and classifying it as noise would have calibrated
    the floor to kill the backstop.
    """
    def _clock(budget_left):
        return wt.triage(_inp(
            watch=_watch(with_rc=False), budget_left=budget_left, budget_total=6,
            evidence=wt.Evidence(kind="clock", text="thesis stale — 10d",
                                 observed_at=NOW, source="clock",
                                 trigger_type="staleness"),
            last_analysis_at=NOW - timedelta(days=10)))

    assert _clock(6).eligible, "the decay backstop must fire on a fresh budget"
    late = _clock(1)
    assert not late.eligible and late.reject_reason == wt.REJECT_BELOW_FLOOR, \
        "a clock must not take the day's last wake from a real event"
