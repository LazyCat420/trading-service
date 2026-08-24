"""The Pipeline panel's history surface: per-cycle events + trigger provenance.

Two separate defects are covered here.

1. `/run-cycle/status` only ever carried the ONE cycle named in the
   `pipeline_state` singleton, so the dashboard's pipeline grid dropped every
   asset row when the next cycle started. `GET /cycles/{id}/events` serves a
   past cycle's events — but only helps if the dict shape is IDENTICAL to what
   `PipelineStateDB.get_state()` builds. A mismatched key does not raise; the
   client's parseEvents silently yields ZERO rows, which looks exactly like a
   cycle that processed nothing. Hence the shape assertions.

2. Nothing linked a `cycle-v3-<epoch>` back to the `wd-`/`sch-*`/`job_` command
   that started it. The trigger event fixes that, and must NOT be mistaken for
   a per-ticker event by the client's ticker heuristic.

This file was written when the readers were dual-store; the service is pure
Mongo now, so every test pins the Mongo reader directly.
"""

import json
from unittest.mock import MagicMock

import pytest


# ── The wire shape the reader must produce ────────────────────────────────────
# Keys copied from app/db/mongo_store.read_pipeline_events. If that reader
# changes, this list must change with it — that is the point of pinning it.
EVENT_KEYS = {"ts", "phase", "step", "detail", "status", "data", "elapsed_ms"}


def _router():
    from app.routers import cycle_replay_router
    return cycle_replay_router


def test_cycle_events_reader_passes_the_wire_shape_through(monkeypatch):
    """The endpoint and /run-cycle/status MUST agree key-for-key.

    They do now because both go through this one reader — that is the point of
    the extraction. A drifted key would not raise; the client's parseEvents
    would return zero rows, which reads as "this cycle processed nothing".
    """
    import app.services.pipeline_state as ps

    canonical = {"ts": "2026-08-19T21:00:19", "phase": "collecting", "step": "s",
                 "detail": "d", "status": "ok", "data": {}, "elapsed_ms": 1}
    monkeypatch.setattr(ps.mongo_store, "read_pipeline_events",
                        lambda cid: [dict(canonical) for _ in range(3)])

    events = ps.PipelineStateDB.get_cycle_events("cycle-x", limit=2)

    assert len(events) == 2, "the limit must apply to the Mongo reader"
    assert set(events[0].keys()) == EVENT_KEYS

    # No limit -> the full stream, so get_state keeps everything.
    events = ps.PipelineStateDB.get_cycle_events("cycle-x")
    assert len(events) == 3


def test_get_state_still_uses_the_extracted_readers(monkeypatch):
    """The extraction must not have orphaned get_state — a regression here
    would blank the LIVE grid, not just history."""
    import inspect
    import app.services.pipeline_state as ps

    src = inspect.getsource(ps.PipelineStateDB.get_state)
    assert "cls.get_cycle_events(cycle_id)" in src
    assert "cls.get_cycle_results(cycle_id)" in src


def test_cycle_results_carry_the_action_the_events_lack(monkeypatch):
    """The OUTPUT column's real source.

    The only phase='trading' event is emit_trade, whose data is
    {kind, ticker, side, qty, price} — no action, no confidence. Without these
    results a history row falls through to the client's `|| 'HOLD'` / `|| 0`
    and renders a confident HOLD for a decision nobody made.
    """
    import app.services.pipeline_state as ps

    monkeypatch.setattr(ps.mongo_store, "find_docs", lambda *a, **k: [
        # result_json as a JSON string, ticker only in the column
        {"ticker": "TRMB",
         "result_json": json.dumps({"action": "HOLD", "confidence": 70,
                                    "trade_executed": False})},
        # result_json already a dict, ticker present inside it
        {"ticker": "NVDA",
         "result_json": {"ticker": "NVDA", "action": "BUY", "confidence": 82,
                         "trade_executed": True}},
    ])

    results = ps.PipelineStateDB.get_cycle_results("cycle-x")

    assert {r["ticker"] for r in results} == {"TRMB", "NVDA"}
    trmb = next(r for r in results if r["ticker"] == "TRMB")
    # ticker is backfilled from the column when the JSON omits it
    assert trmb["action"] == "HOLD" and trmb["confidence"] == 70
    nvda = next(r for r in results if r["ticker"] == "NVDA")
    assert nvda["trade_executed"] is True


def test_cycle_readers_short_circuit_on_a_missing_cycle_id(monkeypatch):
    import app.services.pipeline_state as ps

    events_reader = MagicMock()
    docs_reader = MagicMock()
    monkeypatch.setattr(ps.mongo_store, "read_pipeline_events", events_reader)
    monkeypatch.setattr(ps.mongo_store, "find_docs", docs_reader)

    assert ps.PipelineStateDB.get_cycle_events("") == []
    assert ps.PipelineStateDB.get_cycle_results("") == []
    events_reader.assert_not_called()
    docs_reader.assert_not_called()


def test_cycle_triggers_is_one_query_for_the_whole_page(monkeypatch):
    """Per-cycle this would be a sixth fan-out on an endpoint already at ~1.4s."""
    mod = _router()

    finder = MagicMock(return_value=[
        {"cycle_id": "cycle-a",
         "data": {"source": "watch_desk", "trigger_type": "price_below"}},
        {"cycle_id": "cycle-b", "data": {"source": "manual"}},
    ])
    monkeypatch.setattr(mod.mongo_store, "find_docs", finder)

    out = mod._cycle_triggers(["cycle-a", "cycle-b", "cycle-c"])

    assert finder.call_count == 1
    assert out["cycle-a"]["source"] == "watch_desk"
    assert out["cycle-b"]["source"] == "manual"
    # A cycle predating provenance must be ABSENT, not defaulted — a fabricated
    # "manual" would read as a fact the database never recorded.
    assert "cycle-c" not in out


def test_cycle_triggers_short_circuits_on_empty_page(monkeypatch):
    mod = _router()
    finder = MagicMock()
    monkeypatch.setattr(mod.mongo_store, "find_docs", finder)
    assert mod._cycle_triggers([]) == {}
    finder.assert_not_called()


# ── Trigger provenance ───────────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs,expected", [
    ({"watch_wake": True}, "watch_desk"),
    ({"research_request": True}, "research_governor"),
    ({"dynamic_selection_mode": True}, "schedule"),
    ({}, "manual"),
    # A Watch Desk wake also rides with dynamic_selection_mode=False, but if a
    # producer ever set both, the tripwire is the more specific fact and must win.
    ({"watch_wake": True, "dynamic_selection_mode": True}, "watch_desk"),
])
def test_trigger_source(kwargs, expected):
    from app.services.pipeline_service import _trigger_source
    assert _trigger_source(kwargs) == expected


def test_trigger_payload_carries_the_tripwire():
    from app.services.pipeline_service import _trigger_payload
    p = _trigger_payload(
        {"watch_wake": True,
         "watch_trigger": {"type": "price_below", "detail": "TRMB fell to $54.00"}},
        ["TRMB"],
    )
    # Exact equality on purpose: this pins the WHOLE payload shape, so a key
    # added to _trigger_payload has to be acknowledged here rather than
    # arriving unnoticed. The schedule-provenance fields (ch.92) are present
    # and empty — a Watch Desk wake has no schedule behind it, and asserting
    # that they are None is what proves a wake cannot inherit a stale
    # catalyst from whatever ran before it.
    assert p == {
        "source": "watch_desk",
        "trigger_type": "price_below",
        "reason": "TRMB fell to $54.00",
        "tickers": ["TRMB"],
        "schedule_id": None,
        "reason_codes": [],
        "review_intent": None,
        "urgency": None,
    }


def test_trigger_payload_leaves_unknowns_as_none_not_empty_string():
    from app.services.pipeline_service import _trigger_payload
    p = _trigger_payload({}, [])
    assert p["trigger_type"] is None
    assert p["reason"] is None


def test_trigger_payload_survives_a_non_dict_watch_trigger():
    from app.services.pipeline_service import _trigger_payload
    p = _trigger_payload({"watch_wake": True, "watch_trigger": "price_below"}, ["X"])
    assert p["source"] == "watch_desk"
    assert p["trigger_type"] is None


def test_trigger_detail_is_human_readable_and_bounded():
    from app.services.pipeline_service import _trigger_detail
    d = _trigger_detail(
        {"watch_wake": True,
         "watch_trigger": {"type": "price_below", "detail": "TRMB fell to $54.00"}},
        ["TRMB"],
    )
    assert d == "Watch Desk trip TRMB (price_below) — TRMB fell to $54.00"

    long = _trigger_detail({"research_request": True, "research_reason": "x" * 900}, ["A"])
    assert len(long) <= 500


# ── The guard that keeps the trigger event out of the asset grid ─────────────

def test_trigger_event_cannot_become_a_phantom_asset_row():
    """The client derives asset rows from events whose phase is one of
    collecting/analyzing/trading, reading the last "_"-segment of `step` as a
    ticker (UnifiedPipelineDashboard.jsx parseEvents). A trigger event landing
    in one of those phases — or ending in an uppercase token — would invent an
    asset row for a stock nothing analysed.

    These two constants mirror the client. If the emit call in start_cycle is
    ever moved to another phase or given a ticker-suffixed step, this fails.
    """
    import inspect
    from app.services import pipeline_service

    src = inspect.getsource(pipeline_service.PipelineService.start_cycle)
    assert 'cls.emit(\n                "starting",\n                "cycle_trigger",' in src, (
        "the trigger emit must stay on phase 'starting' with step 'cycle_trigger'"
    )

    TICKER_PHASES = {"collecting", "analyzing", "trading"}
    phase, step = "starting", "cycle_trigger"
    assert phase not in TICKER_PHASES
    last = step.split("_")[-1]
    assert last != last.upper() or len(last) > 10
