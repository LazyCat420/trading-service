"""create_watch and the resolution condition: refuse BAD, warn on ABSENT.

The asymmetry is the design. Refusing on ABSENCE would have stopped the
auto-derived baseline arming any watch at all the day this shipped — the
baseline descends from decisions that never recorded an open question — and a
desk that silently stops monitoring is a worse failure than one monitoring with
a weak record. The cost of absence is charged in the score instead, where it
stays visible.
"""

import json

import pytest

from app.services import watch_desk


@pytest.fixture
def captured(monkeypatch):
    docs = []
    monkeypatch.setattr(watch_desk.mongo_store, "find_one_and_update",
                        lambda *a, **k: None)
    monkeypatch.setattr(watch_desk.mongo_store, "insert_docs",
                        lambda coll, rows: docs.extend(rows))
    return docs


def _triggers():
    return [{"type": "price_below", "level": 61.0}, {"type": "staleness", "max_days": 10}]


def _rc():
    return {"open_question": "Does Q3 net interest margin hold above 3.4%?",
            "resolving_fact": "Q3 net interest margin on the October earnings call"}


class TestAbsentIsAllowedButLoud:
    def test_a_watch_with_no_condition_still_arms(self, captured):
        r = watch_desk.create_watch("C", _triggers())
        assert r["status"] == "armed"

    def test_it_is_recorded_as_schema_0(self, captured):
        watch_desk.create_watch("C", _triggers())
        assert captured[0]["schema_version"] == 0
        assert "decision_context" not in captured[0]

    def test_the_agent_is_told_what_it_left_out(self, captured):
        """A bare 'armed' lets an agent believe it did the job. The warning is
        the only place it learns the desk cannot screen anything for it."""
        r = watch_desk.create_watch("C", _triggers())
        assert "warning" in r
        assert "resolution_condition" in r["warning"]

    def test_a_schema0_watch_is_legacy_to_the_scorer(self, captured):
        from app.services import watch_schema as ws

        watch_desk.create_watch("C", _triggers())
        assert ws.is_legacy(captured[0])
        assert ws.get_resolution_condition(captured[0]) is None


class TestPresentIsStoredWhereTheScorerLooks:
    def test_a_valid_condition_arms_at_schema_1(self, captured):
        r = watch_desk.create_watch("C", _triggers(), resolution_condition=_rc(),
                                    decision_action="HOLD", decision_confidence=0.55)
        assert r["status"] == "armed" and r["schema_version"] == 1
        assert "warning" not in r

    def test_the_scorer_can_find_it(self, captured):
        """Stored under decision_context so `get_resolution_condition` reads it
        at the schema-1 location — a condition the scorer cannot find is a
        condition that does not exist."""
        from app.services import watch_schema as ws

        watch_desk.create_watch("C", _triggers(), resolution_condition=_rc(),
                                decision_action="HOLD", decision_confidence=0.55)
        rc = ws.get_resolution_condition(captured[0])
        assert rc is not None and "margin" in rc["resolving_terms_specific"]

    def test_the_prior_decision_travels_with_the_question(self, captured):
        """They are only meaningful together: 'HOLD' says nothing without what
        the HOLD was waiting on."""
        watch_desk.create_watch("C", _triggers(), resolution_condition=_rc(),
                                decision_action="hold", decision_confidence=0.55)
        dc = captured[0]["decision_context"]
        assert dc["action"] == "HOLD" and dc["confidence"] == 0.55


class TestBadIsRefused:
    def test_a_condition_with_no_open_question_is_refused(self, captured):
        r = watch_desk.create_watch("C", _triggers(), resolution_condition={
            "invalidates_if": {"type": "price_below", "level": 61.0}})
        assert r["status"] == "rejected" and "open_question" in r["reason"]
        assert captured == [], "a refused watch must not be written"

    def test_an_uncheckable_invalidation_is_refused(self, captured):
        r = watch_desk.create_watch("C", _triggers(), resolution_condition={
            **_rc(), "invalidates_if": {"type": "vibes"}})
        assert r["status"] == "rejected" and "not checkable" in r["reason"]
        assert captured == []


class TestTheAutoBaselineNeverInventsOne:
    def test_a_decision_that_stated_none_arms_a_legacy_watch(self, captured):
        """A fabricated condition would screen real headlines against words no
        agent chose, AND make the legacy-penalty telemetry — the only measure
        of how big this gap is — read as closed while nothing had changed."""
        watch_desk.derive_baseline_watch(
            "C", {"action": "HOLD", "rationale": "cheap on book",
                  "estimate": {"stop_loss": 61.0, "take_profit": 80.0}},
            {"price": 70.0}, "cycle-v3-1")
        assert captured and captured[0]["schema_version"] == 0

    def test_a_decision_that_stated_one_carries_it_through(self, captured):
        watch_desk.derive_baseline_watch(
            "C", {"action": "HOLD", "rationale": "cheap on book", "confidence": 0.6,
                  "resolution_condition": _rc(),
                  "estimate": {"stop_loss": 61.0, "take_profit": 80.0}},
            {"price": 70.0}, "cycle-v3-1")
        assert captured and captured[0]["schema_version"] == 1
        assert captured[0]["decision_context"]["action"] == "HOLD"


def test_the_tool_advertises_the_field_it_needs():
    """A parameter the model cannot see is a parameter it will never pass.

    Reads the SCHEMA the registry actually serves to the model, not the Python
    signature: a keyword-only argument that never reaches the tool description
    is unreachable in production while every unit test that calls the function
    directly still passes.

    THIS TEST CAN PASS FOR TWO DIFFERENT REASONS, AND ONE OF THEM IS WEAK.
    `app/tools/registry.py` prefers the generated flat catalog at
    `tool_schemas.json` (gitignored, COPYd into the image) and only falls back
    to the live `@registry.register` decorators when that file is absent. So in
    a fresh git worktree — where the generated file does not exist — this reads
    the decorator and passes trivially. On the primary checkout, and in the
    container, it reads the CATALOG.

    That is not a flaw to paper over; it is the point. The catalog is a
    generated cache with no invalidation, and it caught the real defect on
    2026-09-06: `resolution_condition` was added to the decorator and to
    `create_watch`, both green, while the shipped catalog still described the
    old six parameters — the model would never have seen the field. The fix is
    upstream, in `lazy-agent-service/tool_schemas/trading/watch_desk.json`,
    followed by `scripts/build_tool_schemas.py`.
    """
    import app.tools.watch_desk_tools  # noqa: F401 — registers the tool
    from app.tools.registry import registry

    schemas = registry.get_schemas_by_names(["watch_ticker"])
    assert schemas, "watch_ticker is not registered"
    params = json.dumps(schemas)
    assert "resolution_condition" in params
    assert "open_question" in params and "resolving_fact" in params


def test_the_tool_description_says_why_the_field_matters():
    """'Optional' in the description is how a field ends up never passed."""
    import app.tools.watch_desk_tools  # noqa: F401
    from app.tools.registry import registry

    blob = json.dumps(registry.get_schemas_by_names(["watch_ticker"])).lower()
    assert "always pass resolution_condition" in blob
