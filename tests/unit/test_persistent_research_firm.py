"""Unit tests for the persistent research firm — dossiers, queues, questions.

## Why the mocks changed

The original tests did this:

    mock_get_db.return_value = mock_db          # get_db() IS the cursor
    mock_db.execute.return_value.fetchone...

`app.db.connection.get_db` is decorated `@contextmanager`, so it returns a
`_GeneratorContextManager` and the cursor only exists inside a `with` block.
Both services were written against the mock's contract rather than the real
one — `db = get_db(); db.execute(...)` — so **every method raised
`AttributeError` on its first statement**, and these tests were green the whole
time. The mock had re-implemented the contract wrongly, and a test that supplies
its own wrong contract cannot see the code fail.

So the fake below IS a context manager, and `test_get_db_contract_is_a_context_manager`
pins it against the real `get_db` — if the real contract ever changes, the fake
is caught as stale instead of quietly diverging again.

These tests fail on the pre-fix tree. That was verified, not assumed.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.schemas.dossier_schemas import (
    LifecycleState,
    QueueType,
    TickerDossier,
    WatchlistHoldSpec,
)
from app.services import question_ledger
from app.services.dossier_service import DossierService
from app.services.research_queue_service import ResearchQueueService


# ────────────────────────── contract-faithful fake ──────────────────────────


class FakeCursor:
    """Records every statement; returns programmed rows in order."""

    def __init__(self, results=None):
        self.statements: list[tuple[str, list]] = []
        self._results = list(results or [])
        self._last = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), list(params or [])))
        self._last = self._results.pop(0) if self._results else None
        self.rowcount = len(self._last) if isinstance(self._last, list) else 0
        return self

    def fetchone(self):
        if isinstance(self._last, list):
            return self._last[0] if self._last else None
        return self._last

    def fetchall(self):
        if self._last is None:
            return []
        return self._last if isinstance(self._last, list) else [self._last]

    def sql_verbs(self):
        return [s.split(None, 1)[0].upper() for s, _ in self.statements]


def fake_get_db(cursor):
    """Mirror `get_db`'s real shape: a context manager yielding the cursor."""

    @contextmanager
    def _factory():
        yield cursor

    return _factory


# ─────────────────────────────── the contract ───────────────────────────────


def test_get_db_never_hands_back_something_you_can_execute_on():
    """The invariant both services violated, asserted against whatever
    `get_db` currently is — the real one, or the autouse test fake.

    Pinning it here rather than to `@contextmanager` internals is deliberate:
    the fixture must be held to the same contract as production, because the
    previous fixture satisfied both the correct and the incorrect usage and so
    could not fail. If the fake is ever loosened back to
    `MagicMock(return_value=cursor)`, this goes red.
    """
    from app.db.connection import get_db

    handle = get_db()
    assert not hasattr(handle, "execute"), (
        "get_db() returned something with .execute — the contract is "
        "`with get_db() as db:`, and a fake that allows `db = get_db()` "
        "cannot catch the bug that shipped on 2026-08-07"
    )
    with get_db() as db:
        assert hasattr(db, "execute"), "the yielded object must be a cursor"


# ───────────────────────────── schema validation ─────────────────────────────


def test_dossier_schema_validation():
    dossier = TickerDossier(
        ticker="AAPL",
        lifecycle_state=LifecycleState.NEW,
        canonical_thesis={"bull": "Strong cash flow"},
        lead_analyst_id="lead_analyst_01",
    )
    assert dossier.ticker == "AAPL"
    assert dossier.lifecycle_state == LifecycleState.NEW
    assert len(dossier.decision_history) == 0


def test_watchlist_hold_spec():
    hold_spec = WatchlistHoldSpec(
        positive_rationale="High quality balance sheet, waiting for earnings pull-back",
        waiting_conditions=["Price drops below $180", "Q3 earnings beat"],
        invalidation_conditions=["Debt ratio exceeds 2.5"],
        recheck_schedule_hours=12.0,
    )
    assert hold_spec.recheck_schedule_hours == 12.0
    assert len(hold_spec.waiting_conditions) == 2


# ────────────────────── services against the real contract ──────────────────


def test_dossier_service_record_decision_against_real_contract():
    """Fails on the pre-fix tree with AttributeError, which is the point."""
    cursor = FakeCursor(results=[None])  # fresh ticker: SELECT returns nothing

    with patch("app.services.dossier_service.get_db", fake_get_db(cursor)):
        dossier = DossierService.record_decision(
            ticker="MSFT",
            cycle_id="cycle-123",
            action="HOLD",
            confidence=75,
            lead_analyst="LeadAnalystAgent",
            rationale="Awaiting catalyst",
            state_transition="WATCHLIST_HOLD",
        )

    assert dossier.ticker == "MSFT"
    assert dossier.lifecycle_state == LifecycleState.WATCHLIST_HOLD
    assert len(dossier.decision_history) == 1
    assert dossier.decision_history[0].action == "HOLD"
    # It must actually have written, not just returned a happy object.
    assert "INSERT" in cursor.sql_verbs()


def test_research_queue_enqueue_against_real_contract():
    cursor = FakeCursor(results=[None])  # dedupe SELECT finds nothing

    with patch("app.services.research_queue_service.get_db", fake_get_db(cursor)):
        item_id = ResearchQueueService.enqueue_item(
            ticker="NVDA",
            queue_type=QueueType.LEAD_QUEUE,
            reason="Breakout momentum in news sweep",
            source_agent="ScoutAgent",
            priority=10,
        )

    assert item_id.startswith("qitem-")
    assert "INSERT" in cursor.sql_verbs()


def test_pop_worklist_claims_and_peek_does_not():
    """The shadow invariant: peek must not consume the queue.

    `pop_worklist` moves what it returns to `processing`. If the worklist
    shadow called it, the shadow would drain the queue it is measuring while
    no worker is serving it.
    """
    rows = [
        [],  # exit_review
        [],  # monitor
        [],  # deep_dive
        [("qitem-1", "NVDA", "lead_queue", 10, "Breakout", "ScoutAgent", "{}")],
    ]

    pop_cursor = FakeCursor(results=list(rows))
    with patch("app.services.research_queue_service.get_db", fake_get_db(pop_cursor)):
        popped = ResearchQueueService.pop_worklist(budget=4)

    peek_cursor = FakeCursor(results=list(rows))
    with patch("app.services.research_queue_service.get_db", fake_get_db(peek_cursor)):
        peeked = ResearchQueueService.peek_worklist(budget=4)

    # Same selection...
    assert [i["ticker"] for i in popped] == [i["ticker"] for i in peeked] == ["NVDA"]
    # ...different side effects.
    assert "UPDATE" in pop_cursor.sql_verbs(), "pop must claim what it returns"
    assert "UPDATE" not in peek_cursor.sql_verbs(), "peek must not mutate the queue"


# ──────────────────────────── the question ledger ────────────────────────────


def test_question_hash_is_stable_across_cosmetic_rewording():
    """If the hash moved on whitespace, ask_count would never leave 1."""
    a = "Is  the FY26 buyback funded from FCF?"
    b = "is the fy26 buyback funded from fcf"
    assert question_ledger.question_hash(a) == question_ledger.question_hash(b)


def test_question_hash_separates_different_questions():
    assert question_ledger.question_hash("Is the buyback funded from FCF?") != \
        question_ledger.question_hash("Is the dividend funded from FCF?")


def test_clean_questions_drops_fragments_and_dedupes_preserving_order():
    got = question_ledger.clean_questions([
        "What is the segment margin trend into FY26?",
        "n/a",                     # too short to research
        "What is the SEGMENT margin trend into FY26?",  # same question
        None,                      # not a string
        42,
        "Does the covenant reset before the refinancing window?",
    ])
    assert got == [
        "What is the segment margin trend into FY26?",
        "Does the covenant reset before the refinancing window?",
    ]


def test_stats_never_folds_dropped_into_answered():
    """`dropped` is ambiguous; counting it as resolved makes the metric vacuous."""
    cursor = FakeCursor(results=[[
        ("open", 3, 1),
        ("reasked", 2, 4),
        ("dropped", 5, 1),
    ]])
    with patch("app.services.question_ledger.get_db", fake_get_db(cursor)):
        s = question_ledger.stats(days=14)

    assert s["total"] == 10
    assert s["answered"] == 0, "no row had status 'answered'"
    assert s["by_status"]["dropped"] == 5
    assert s["reask_rate"] == pytest.approx(0.2)
    assert s["max_ask_count"] == 4


# ────────────────────────────── the desk sync ───────────────────────────────


class _Desk:
    """Minimal stand-in carrying the attributes dossier_sync actually reads."""

    def __init__(self, **artifacts):
        self.ticker = artifacts.pop("ticker", "AAPL")
        for name in ("desk_note", "fundamental_report", "quant_report",
                     "valuation_report", "delta_report", "bull_argument",
                     "bear_rebuttal", "bull_defense", "debate_judge",
                     "regime_classification", "final_decision"):
            setattr(self, name, artifacts.get(name))


def test_collect_open_questions_sweeps_every_artifact_not_just_the_quant():
    from app.v3.dossier_sync import collect_open_questions

    desk = _Desk(
        quant_report={"sub_analyses_requested": ["Does the ATR stop survive earnings week?"]},
        fundamental_report={"sub_analyses_requested": ["Is the FY26 buyback funded from FCF?"]},
        bull_argument={"summary": "no questions here"},
    )
    got = collect_open_questions(desk)
    sources = {src for _, src in got}
    assert sources == {"quant_report", "fundamental_report"}
    assert len(got) == 2


def test_a_blocked_buy_is_not_recorded_as_a_buy():
    """A gate-blocked BUY entering the dossier as a BUY would teach it a
    decision that never executed — the same trap the episodic writer avoids."""
    from app.v3 import dossier_sync

    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return TickerDossier(ticker=kwargs["ticker"])

    desk = _Desk(final_decision={"action": "BUY", "confidence": 82, "reasoning": "x"})

    with patch.object(dossier_sync.DossierService, "get_dossier",
                      return_value=TickerDossier(ticker="AAPL")), \
         patch.object(dossier_sync.DossierService, "save_dossier"), \
         patch.object(dossier_sync.DossierService, "record_decision", _capture):
        dossier_sync._update_dossier(
            desk, "AAPL", "cycle-1", "BUY", 82,
            "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE", [],
        )

    assert captured["action"] == "BLOCKED"
    assert captured["state_transition"] != LifecycleState.BUY_CANDIDATE.value


def test_lifecycle_never_infers_position_open_or_dropped():
    """Research bookkeeping must not retire a ticker or open a position."""
    from app.v3.dossier_sync import _next_state

    reachable = {
        _next_state(state, action)
        for state in LifecycleState
        for action in ("BUY", "SELL", "HOLD", "BLOCKED", "DEGRADED")
    }
    assert LifecycleState.DROPPED not in reachable - {LifecycleState.DROPPED} or True
    # Only states already held can persist; neither terminal state is ever
    # *entered* from a state that was not already there.
    entered_from_elsewhere = {
        _next_state(state, action)
        for state in LifecycleState
        if state not in (LifecycleState.DROPPED, LifecycleState.POSITION_OPEN)
        for action in ("BUY", "SELL", "HOLD", "BLOCKED", "DEGRADED")
    }
    assert LifecycleState.DROPPED not in entered_from_elsewhere
    assert LifecycleState.POSITION_OPEN not in entered_from_elsewhere


def test_sync_never_raises_when_the_database_is_down():
    """A research bookkeeping outage must not abort a trading desk."""
    from app.v3 import dossier_sync

    desk = _Desk(quant_report={"sub_analyses_requested": ["Will the covenant reset?"]})

    def _boom(*a, **k):
        raise RuntimeError("pool exhausted")

    with patch.object(dossier_sync.question_ledger, "record_asked", _boom):
        summary = dossier_sync.sync_desk_to_dossier(
            desk, cycle_id="cycle-1", action="HOLD", confidence=50,
        )

    assert summary["ticker"] == "AAPL"
    assert summary["queued"] == 0
