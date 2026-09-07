"""The Stage-2 planner: off by default, parseable, and never authoritative.

It ships disabled. The delivery order is deterministic triage in shadow FIRST,
planner only once the shadow data shows the triage inputs are reliable — a model
asked to judge unvalidated numbers produces judgements that look reasonable
either way, which is how a green check lies.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services import watch_planner as wpl
from app.services import watch_schema as ws
from app.services.watch_triage import TriageVerdict

NOW = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)


def _watch():
    dc, err = ws.normalize_decision_context({
        "action": "HOLD", "confidence": 0.5,
        "thesis_summary": "Cheap on tangible book.",
        "resolution_condition": {
            "open_question": "Does Q3 net interest margin hold above 3.4%?",
            "resolving_fact": "Q3 net interest margin on the October earnings call",
            "resolves_by": NOW + timedelta(days=38)},
    })
    assert err is None, err
    return {"id": "w1", "ticker": "C", "schema_version": 1, "decision_context": dc}


def _verdict():
    return TriageVerdict(
        eligible=True, reject_reason=None, score=2.4,
        components={"materiality": 0.5, "catalyst_urgency": 0.3},
        detail={"evidence": {"kind": "news", "trigger_type": "news",
                             "source": "detected", "text": "Citi Q3 NIM beats",
                             "observed_at": NOW},
                "resolution_overlap": 2},
        event_key="k1")


def _cal():
    at = NOW + timedelta(days=38)
    return {"events": [{"kind": "earnings", "at": at, "confidence": "date_known_hour_assumed",
                        "label": "C earnings", "source": "finviz"}],
            "earnings_at": at, "earnings_confidence": "date_known_hour_assumed",
            "sources": ["fundamentals"]}


class TestItShipsDisabled:
    def test_the_planner_returns_nothing_when_disabled(self, monkeypatch):
        monkeypatch.setattr("app.services.parameter_store.get_param",
                            lambda name, *a, **k: 0)
        called = []

        async def spy(**kw):
            called.append(kw)
            return {"response": '{"action":"ANALYZE_NOW"}'}

        r, err = asyncio.run(wpl.plan(ticker="C", watch=_watch(), verdict=_verdict(),
                                      calendar=_cal(), now=NOW, call_model=spy))
        assert r is None and "disabled" in err
        assert called == [], "a disabled planner must not spend a model call"

    def test_the_registered_default_is_off(self):
        from app.services.parameter_store import PARAMETER_REGISTRY

        assert PARAMETER_REGISTRY["WATCH_PLANNER_ENABLED"].default == 0

    def test_an_unreadable_switch_is_treated_as_disabled(self, monkeypatch):
        """An unreadable switch must not ENABLE a feature. Failing open here
        would turn a parameter-store hiccup into a live model in the loop."""
        def boom(*a, **k):
            raise RuntimeError("param store down")

        monkeypatch.setattr("app.services.parameter_store.get_param", boom)
        r, err = asyncio.run(wpl.plan(ticker="C", watch=_watch(), verdict=_verdict(),
                                      calendar=_cal(), now=NOW))
        assert r is None and "disabled" in err


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr("app.services.parameter_store.get_param",
                        lambda name, *a, **k: 1)


class TestParsing:
    def _run(self, payload):
        async def fake(**kw):
            return {"response": payload, "tokens_used": 120}

        return asyncio.run(wpl.plan(ticker="C", watch=_watch(), verdict=_verdict(),
                                    calendar=_cal(), now=NOW, call_model=fake))

    def test_a_bare_object_parses(self, enabled):
        r, err = self._run('{"action":"ANALYZE_NOW","rationale":"guidance cut"}')
        assert err is None and r["action"] == "ANALYZE_NOW"

    def test_a_fenced_object_parses(self, enabled):
        """A model that fences its answer has still answered. Scoring that as a
        refusal makes the planner look like it never recommends anything."""
        r, err = self._run('Here you go:\n```json\n{"action":"CLOSE_WATCH",'
                           '"close_reason":"settled"}\n```\nHope that helps!')
        assert err is None and r["action"] == "CLOSE_WATCH"

    def test_prose_with_no_object_is_an_error_not_a_crash(self, enabled):
        r, err = self._run("I think you should probably look at it again soon.")
        assert r is None and "no parsable JSON" in err

    def test_an_invalid_action_is_refused_by_the_schema(self, enabled):
        r, err = self._run('{"action":"SELL_EVERYTHING"}')
        assert r is None and "invalid" in err

    def test_a_model_failure_degrades_to_no_opinion(self, enabled):
        """apply_policy with no planner result falls back to the deterministic
        path — so an outage degrades to the behaviour that shipped, never to
        no behaviour."""
        async def boom(**kw):
            raise TimeoutError("model unreachable")

        r, err = asyncio.run(wpl.plan(ticker="C", watch=_watch(), verdict=_verdict(),
                                      calendar=_cal(), now=NOW, call_model=boom))
        assert r is None and "failed" in err

    def test_token_usage_is_carried_through(self, enabled):
        r, _ = self._run('{"action":"ANALYZE_NOW"}')
        assert r["tokens_used"] == 120


class TestThePrompt:
    def test_it_states_the_open_question(self):
        p = wpl.build_prompt(ticker="C", watch=_watch(), verdict=_verdict(),
                             calendar=_cal(), now=NOW)
        assert "net interest margin" in p
        assert "OPEN QUESTION" in p

    def test_a_legacy_watch_says_so_rather_than_leaving_a_blank(self):
        """A blank invites the model to invent one; an explicit 'NONE RECORDED'
        tells it what it is actually working without."""
        p = wpl.build_prompt(ticker="C", watch={"id": "w0", "ticker": "C"},
                             verdict=_verdict(), calendar=_cal(), now=NOW)
        assert "NONE RECORDED" in p

    def test_the_score_components_are_shown_not_just_the_total(self):
        """The planner's useful contribution is disagreeing with a SPECIFIC
        term. It cannot disagree with a number it cannot see."""
        p = wpl.build_prompt(ticker="C", watch=_watch(), verdict=_verdict(),
                             calendar=_cal(), now=NOW)
        assert "catalyst_urgency" in p and "COMPONENTS" in p

    def test_the_calendar_is_rendered(self):
        p = wpl.build_prompt(ticker="C", watch=_watch(), verdict=_verdict(),
                             calendar=_cal(), now=NOW)
        assert "earnings" in p

    def test_an_empty_calendar_says_so_explicitly(self):
        p = wpl.build_prompt(ticker="C", watch=_watch(), verdict=_verdict(),
                             calendar={}, now=NOW)
        assert "no scheduled events found" in p

    def test_the_system_prompt_names_all_four_actions_and_only_those(self):
        for a in ws.PLANNER_ACTIONS:
            assert a in wpl.SYSTEM_PROMPT
        assert "do nothing" in wpl.SYSTEM_PROMPT.lower(), \
            "the prompt must make 'do nothing' an explicitly good answer"

    def test_the_prompt_tells_it_to_schedule_AFTER_a_release_not_before(self):
        """The single most expensive mistake available to it: spending a cycle
        hours before a known release that is about to rewrite the answer."""
        low = wpl.SYSTEM_PROMPT.lower()
        assert "after the fact lands" in low and "never just before" in low
