"""Agent cost must survive a desk that dies mid-pipeline.

`persist_telemetry` ran once, at the very end of `run_v3_pipeline`, so every
agent's spend accumulated in memory on `desk.agent_telemetry` until then. A
ticker that raised before that line lost its ENTIRE cost record — the tokens
were spent, the rows were never written.

Measured since 2026-07-12, when this telemetry began (before that the table is
empty, so any coverage figure spanning the boundary averages two populations and
reads as a partial outage):

    PM_DONE         429 desks    99.5% have cost rows
    ABORTED          40 desks     0.0%
    DEBATE_DONE       9 desks     0.0%
    RESEARCH_DONE    22 desks     4.5%

71 desks with no cost record, at a median 664,627 tokens per completed desk, is
up to ~47M tokens — ~14.5% of true spend — invisible.

THE RISK THESE TESTS EXIST FOR
------------------------------
Flushing more often can DOUBLE-COUNT, which would corrupt the exact numbers the
change is meant to fix — a worse failure than under-counting, because it looks
like data. `agent_telemetry` round-trips through `SharedDesk.to_dict`/`from_dict`,
so a desk reloaded from Postgres carries its entries back and must not re-bill
them. Hence: the written-marker lives inside the entry, and the flush happens
BEFORE serialization so the marker is in `desk_data`.
"""

from __future__ import annotations

import pytest

from app.v3 import telemetry as tel
from app.v3.shared_desk import SharedDesk
from app.v3.telemetry import PERSISTED_FLAG, flush_agent_telemetry


@pytest.fixture
def written(monkeypatch):
    """Capture what would be inserted, per call."""
    calls = []

    def _persist(desk, entries):
        calls.append([dict(e) for e in entries])

    monkeypatch.setattr(tel, "_persist_entries", _persist)
    return calls


def _desk(n=3, ticker="HOOD"):
    d = SharedDesk(cycle_id="cycle-1", ticker=ticker)
    for i in range(n):
        d.record_agent_telemetry({
            "agent_name": f"agent_{i}", "phase": "RESEARCH",
            "token_usage": 1000 * (i + 1), "loops_used": 1,
        })
    return d


# ── The loss it prevents ─────────────────────────────────────────────────


def test_flush_writes_the_pending_entries(written):
    desk = _desk(3)

    assert flush_agent_telemetry(desk) == 3
    assert len(written) == 1
    assert [e["agent_name"] for e in written[0]] == ["agent_0", "agent_1", "agent_2"]


def test_cost_survives_a_desk_that_never_reaches_the_end(written):
    """The HOOD case: two agents paid for, then the pipeline dies.

    Before this change the desk was saved at DEBATE_DONE and the cost rows were
    never written, because the only writer ran after the phase HOOD never
    reached.
    """
    desk = _desk(2)
    flush_agent_telemetry(desk)          # what save_desk now does mid-flight

    spent = sum(e["token_usage"] for e in written[0])
    assert spent == 3000, "the tokens already spent must be on record"


# ── Double-counting: the failure mode that looks like data ───────────────


def test_a_second_flush_writes_nothing(written):
    desk = _desk(3)

    assert flush_agent_telemetry(desk) == 3
    assert flush_agent_telemetry(desk) == 0
    assert len(written) == 1


def test_only_new_entries_are_written_after_more_agents_run(written):
    desk = _desk(2)
    flush_agent_telemetry(desk)
    desk.record_agent_telemetry({"agent_name": "late", "token_usage": 99})

    assert flush_agent_telemetry(desk) == 1
    assert [e["agent_name"] for e in written[1]] == ["late"]


def test_persist_telemetry_after_a_flush_is_a_no_op(written):
    """Both writers coexist: the end-of-pipeline call is now a backstop."""
    desk = _desk(3)
    flush_agent_telemetry(desk)
    tel.persist_telemetry(desk)

    assert len(written) == 1, "end-of-pipeline persist double-billed the desk"


def test_the_marker_survives_the_desks_round_trip_to_postgres(written):
    """A reloaded desk must not re-bill.

    `to_dict`/`from_dict` carry `agent_telemetry` verbatim, so the marker has to
    live inside the entry — this is what makes flushing-on-every-save safe.
    """
    desk = _desk(3)
    flush_agent_telemetry(desk)

    revived = SharedDesk.from_dict(desk.to_dict())
    assert all(e.get(PERSISTED_FLAG) for e in revived.agent_telemetry)
    assert flush_agent_telemetry(revived) == 0
    assert len(written) == 1


def test_save_desk_flushes_before_serialising(monkeypatch):
    """Order is load-bearing: markers must be inside the saved desk_data.

    Flush after `to_dict()` and the stored copy looks unwritten, so the next
    reload bills every entry a second time.
    """
    import json as _json

    from app.v3 import desk_persistence as dp

    captured = {}

    monkeypatch.setattr(dp, "_ensure_table", lambda: None)
    monkeypatch.setattr(tel, "_persist_entries", lambda desk, entries: None)

    class _DB:
        def execute(self, sql, params=None):
            captured["desk_data"] = params[4]
            return self

    from contextlib import contextmanager

    @contextmanager
    def _get_db():
        yield _DB()

    monkeypatch.setattr("app.db.connection.get_db", _get_db)

    dp.save_desk(_desk(2))

    stored = _json.loads(captured["desk_data"])
    entries = stored["agent_telemetry"]
    assert entries, "telemetry vanished from the stored desk"
    assert all(e.get(PERSISTED_FLAG) for e in entries), (
        "flush ran AFTER to_dict() — the stored desk looks unwritten and will "
        "double-bill on reload"
    )


# ── A failed write must stay pending, not be recorded as billed ──────────


def test_a_failed_write_leaves_the_entries_pending(monkeypatch):
    """Marking on failure would lose the cost record permanently.

    `_persist_entries` re-raises for exactly this reason; swallowing there would
    mark unwritten rows as billed.
    """
    def _boom(desk, entries):
        raise RuntimeError("db down")

    monkeypatch.setattr(tel, "_persist_entries", _boom)
    desk = _desk(2)

    assert flush_agent_telemetry(desk) == 0
    assert not any(e.get(PERSISTED_FLAG) for e in desk.agent_telemetry)


def test_a_failed_write_is_retried_by_the_next_save(monkeypatch):
    calls = []
    state = {"fail": True}

    def _maybe(desk, entries):
        if state["fail"]:
            raise RuntimeError("db down")
        calls.append(len(entries))

    monkeypatch.setattr(tel, "_persist_entries", _maybe)
    desk = _desk(2)
    assert flush_agent_telemetry(desk) == 0

    state["fail"] = False
    assert flush_agent_telemetry(desk) == 2
    assert calls == [2]


def test_a_flush_failure_never_breaks_the_desk_save(monkeypatch):
    """Cost accounting must not be able to lose a desk — that trade is backwards."""
    import json as _json

    from app.v3 import desk_persistence as dp

    captured = {}
    monkeypatch.setattr(dp, "_ensure_table", lambda: None)
    monkeypatch.setattr(
        tel, "flush_agent_telemetry",
        lambda desk: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    class _DB:
        def execute(self, sql, params=None):
            captured["desk_data"] = params[4]
            return self

    from contextlib import contextmanager

    @contextmanager
    def _get_db():
        yield _DB()

    monkeypatch.setattr("app.db.connection.get_db", _get_db)

    dp.save_desk(_desk(2))          # must not raise

    assert _json.loads(captured["desk_data"])["ticker"] == "HOOD"


# ── Edges ────────────────────────────────────────────────────────────────


def test_an_empty_desk_writes_nothing(written):
    assert flush_agent_telemetry(_desk(0)) == 0
    assert written == []


def test_non_dict_entries_are_skipped_not_fatal(written):
    desk = _desk(1)
    desk.agent_telemetry.append("not a dict")

    assert flush_agent_telemetry(desk) == 1


def test_persist_entries_re_raises_a_db_failure(monkeypatch):
    """The REAL `_persist_entries` must raise, not swallow.

    Every other test here stubs `_persist_entries`, so without this one the
    `raise` is invisible to the suite — and it is the whole reason a failed write
    stays pending instead of being marked as billed. Deleting it broke nothing
    until this test existed.
    """
    from contextlib import contextmanager

    monkeypatch.setattr(tel, "_ensure_telemetry_table", lambda: None)

    @contextmanager
    def _boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr("app.db.connection.get_db", _boom)

    with pytest.raises(RuntimeError):
        tel._persist_entries(_desk(2), [{"agent_name": "a", "token_usage": 1}])


def test_a_real_db_failure_leaves_the_entries_pending(monkeypatch):
    """End to end through the real writer: nothing is marked when the DB is down."""
    from contextlib import contextmanager

    monkeypatch.setattr(tel, "_ensure_telemetry_table", lambda: None)

    @contextmanager
    def _boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr("app.db.connection.get_db", _boom)
    desk = _desk(2)

    assert flush_agent_telemetry(desk) == 0
    assert not any(e.get(PERSISTED_FLAG) for e in desk.agent_telemetry)


def test_the_flush_is_wired_into_save_desk():
    """A flush nothing calls is the bug it was written to fix.

    Asserts the CALL, not the mere presence of the name — leaving the import and
    deleting the call would otherwise read as wired.
    """
    import ast
    import inspect
    import textwrap

    from app.v3 import desk_persistence

    tree = ast.parse(textwrap.dedent(inspect.getsource(desk_persistence.save_desk)))
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "flush_agent_telemetry" in called, (
        "save_desk no longer CALLS flush_agent_telemetry — a desk that dies "
        "mid-pipeline goes back to losing its entire token record"
    )
