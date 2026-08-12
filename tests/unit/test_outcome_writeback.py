"""The outcome -> memory writeback loop.

Every memory row this service writes is written at DECISION time and, until
this change, was never revised. Measured on the live database 2026-08-12:

    decision_outcomes resolved ............ 2,394
    episodic_memory   outcome='pending' ...   572   (+3,080 at the 'neutral' default)
    episodic_observations source_type='outcome'   4   (all from June; the writer is gone)

So `episodic_memory.retrieve`'s "ranked by most successful outcomes" ranked a
column where every row tied at 0, and the consolidator — which distils
canonical memories — was handed `Outcome: BUY (None)`, i.e. the ACTION under
the label "outcome".

The negative controls come first on purpose: a writeback that fires on an
unresolved or degraded decision would teach the memory system an outcome that
does not exist yet, which is worse than the gap it replaces.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.autoresearch import outcome_tracker


class _FakeStore:
    def __init__(self):
        self.observations = []

    def add_episodic_observation(self, observation: dict) -> str:
        self.observations.append(observation)
        return "obs-fake"


class _FakeEpisodes:
    def __init__(self):
        self.calls = []

    def record_outcome(self, cycle_id, ticker, outcome, outcome_score):
        self.calls.append((cycle_id, ticker, outcome, outcome_score))
        return 1


@pytest.fixture
def sinks(monkeypatch):
    store, episodes = _FakeStore(), _FakeEpisodes()
    monkeypatch.setattr(outcome_tracker, "_memory_store", lambda: store)
    monkeypatch.setattr(outcome_tracker, "_episode_store", lambda: episodes)
    return store, episodes


# ── Negative controls ────────────────────────────────────────────────────────

def test_no_writeback_without_a_resolved_outcome(sinks):
    """An unresolved row must write nothing. `outcome=None` is the shape a
    caller passes when resolution did not happen."""
    store, episodes = sinks
    outcome_tracker.write_outcome_to_memory(
        cycle_id="cycle-1", ticker="AAPL", action="BUY",
        outcome=None, pnl_pct=None, confidence=80,
    )
    assert store.observations == []
    assert episodes.calls == []


def test_no_writeback_for_a_degraded_decision(sinks):
    """DEGRADED is the label the orchestrator writes when a desk produced no
    decision. It is not an action and cannot have an outcome."""
    store, episodes = sinks
    outcome_tracker.write_outcome_to_memory(
        cycle_id="cycle-2", ticker="AAPL", action="DEGRADED",
        outcome="WIN", pnl_pct=4.0, confidence=0,
    )
    assert store.observations == []
    assert episodes.calls == []


@pytest.mark.parametrize("label", ["DEGRADED_ARTIFACT", "CANCELED"])
def test_no_writeback_for_a_non_outcome_label(sinks, label):
    """DEGRADED_ARTIFACT is a pipeline crash scored against a price — 358 rows,
    the third-largest label on decision_outcomes. Every other consumer of this
    column already excludes it (decision_audit, confidence_calibration,
    power_report); the memory system must too, or the desk learns that
    crashing is a strategy."""
    store, episodes = sinks
    outcome_tracker.write_outcome_to_memory(
        cycle_id="cycle-8", ticker="AAPL", action="BUY",
        outcome=label, pnl_pct=11.46, confidence=0,
    )
    assert store.observations == []
    assert episodes.calls == []


def test_no_writeback_without_a_cycle_id(sinks):
    """cycle_id+ticker is the only join back to the memory rows. Without it
    the update would match every episode for the ticker."""
    store, episodes = sinks
    outcome_tracker.write_outcome_to_memory(
        cycle_id="", ticker="AAPL", action="BUY",
        outcome="WIN", pnl_pct=4.0, confidence=80,
    )
    assert store.observations == []
    assert episodes.calls == []


# ── The loop itself ──────────────────────────────────────────────────────────

def test_resolved_win_writes_an_outcome_observation(sinks):
    store, episodes = sinks
    outcome_tracker.write_outcome_to_memory(
        cycle_id="cycle-3", ticker="AAPL", action="BUY",
        outcome="WIN", pnl_pct=6.25, confidence=82,
    )

    assert len(store.observations) == 1
    obs = store.observations[0]
    # source_type is the documented contract (app/db/README_memory_contracts.md)
    # and is what distinguishes this row from the decision-time one written by
    # the same cycle — the dedup guard in MemoryStore keys on it.
    assert obs["source_type"] == "outcome"
    assert obs["cycle_id"] == "cycle-3"
    assert obs["outcome_label"] == "WIN"
    # Raw pnl, matching the four rows already on the table (-7.67, -8.76,
    # +5.07, -5.61) and what the consolidator renders.
    assert obs["outcome_score"] == pytest.approx(6.25)
    assert "BUY" in obs["observation_text"]
    assert "6.25" in obs["observation_text"]


def test_resolved_outcome_updates_the_working_memory_episode(sinks):
    store, episodes = sinks
    outcome_tracker.write_outcome_to_memory(
        cycle_id="cycle-4", ticker="MSFT", action="BUY",
        outcome="LOSS", pnl_pct=-12.0, confidence=71,
    )
    assert len(episodes.calls) == 1
    cycle_id, ticker, outcome, score = episodes.calls[0]
    assert (cycle_id, ticker, outcome) == ("cycle-4", "MSFT", "LOSS")
    # Normalised to the -1.0..1.0 range the column is documented as carrying
    # (migrations.py), clamped: -12% is past the -10% anchor.
    assert score == pytest.approx(-1.0)


def test_episode_score_is_normalised_not_raw(sinks):
    _, episodes = sinks
    outcome_tracker.write_outcome_to_memory(
        cycle_id="cycle-5", ticker="NVDA", action="BUY",
        outcome="WIN", pnl_pct=5.0, confidence=90,
    )
    assert episodes.calls[0][3] == pytest.approx(0.5)


def test_hold_outcomes_are_written_too(sinks):
    """HOLDs are ~72% of the observations on the table (581 of 818). A loop
    that only graded BUY/SELL would leave the majority tier unresolved."""
    store, episodes = sinks
    outcome_tracker.write_outcome_to_memory(
        cycle_id="cycle-6", ticker="GOOG", action="HOLD",
        outcome="HOLD_AVOIDED_DECLINE", pnl_pct=-3.2, confidence=64,
    )
    assert store.observations[0]["outcome_label"] == "HOLD_AVOIDED_DECLINE"
    assert episodes.calls[0][2] == "HOLD_AVOIDED_DECLINE"


# ── The call sites ───────────────────────────────────────────────────────────
#
# The function above can be perfect and never invoked. These drive the two
# resolvers through a faked cursor and assert the loop actually closes on the
# live path.

def _patch_db(pending_rows):
    """A get_db() whose first execute() returns `pending_rows`."""
    calls = {"n": 0}

    @contextmanager
    def factory():
        conn = MagicMock()

        def execute_side_effect(*args, **kwargs):
            calls["n"] += 1
            cursor = MagicMock()
            cursor.fetchall.return_value = pending_rows if calls["n"] == 1 else []
            return cursor

        conn.execute.side_effect = execute_side_effect
        yield conn

    return patch("app.autoresearch.outcome_tracker.get_db", factory)


@pytest.fixture
def captured(monkeypatch):
    seen = []
    monkeypatch.setattr(
        outcome_tracker, "write_outcome_to_memory",
        lambda **kw: seen.append(kw),
    )
    return seen


def test_batch_resolver_feeds_memory(captured):
    created = datetime(2026, 8, 1, tzinfo=timezone.utc)
    row = ("do-1", "AAPL", "BUY", 100.0, created, "cycle-v3-9", 82)
    with _patch_db([row]), patch("app.quant.returns.latest_close", return_value=110.0):
        stats = outcome_tracker.resolve_pending_outcomes()

    assert stats["resolved"] == 1
    assert len(captured) == 1
    assert captured[0]["cycle_id"] == "cycle-v3-9"
    assert captured[0]["ticker"] == "AAPL"
    assert captured[0]["outcome"] == "WIN"
    assert captured[0]["pnl_pct"] == pytest.approx(10.0)
    assert captured[0]["confidence"] == 82


def test_batch_resolver_writes_nothing_when_the_row_cannot_resolve(captured):
    """No current price -> no resolution -> no memory. The negative control for
    the call site: an unresolvable row must leave both tiers untouched."""
    created = datetime(2026, 8, 1, tzinfo=timezone.utc)
    row = ("do-2", "AAPL", "BUY", 100.0, created, "cycle-v3-9", 82)
    with _patch_db([row]), patch("app.quant.returns.latest_close", return_value=None):
        stats = outcome_tracker.resolve_pending_outcomes()

    assert stats["resolved"] == 0
    assert captured == []


def test_exit_resolver_feeds_memory(captured):
    row = ("do-3", "BUY", 50.0, "cycle-v3-11", 74)
    with _patch_db([row]):
        resolved = outcome_tracker.resolve_outcome_for_exit("NVDA", exit_price=45.0)

    assert resolved == 1
    assert len(captured) == 1
    assert captured[0]["cycle_id"] == "cycle-v3-11"
    assert captured[0]["outcome"] == "LOSS"
    assert captured[0]["pnl_pct"] == pytest.approx(-10.0)


def test_exit_resolver_skips_holds(captured):
    """A HOLD claim is about the horizon; an exit at day 2 says nothing about
    it, so the existing resolver skips it — and so must the writeback."""
    row = ("do-4", "HOLD", 50.0, "cycle-v3-12", 60)
    with _patch_db([row]):
        resolved = outcome_tracker.resolve_outcome_for_exit("NVDA", exit_price=45.0)

    assert resolved == 0
    assert captured == []


def test_a_failing_sink_never_propagates(monkeypatch, sinks):
    """A lost outcome is worse than a lost memory row: the writeback runs
    inside the resolver, after the decision_outcomes UPDATE."""
    store, episodes = sinks

    def _boom():
        raise RuntimeError("memory store down")

    monkeypatch.setattr(outcome_tracker, "_memory_store", _boom)
    outcome_tracker.write_outcome_to_memory(
        cycle_id="cycle-7", ticker="AAPL", action="BUY",
        outcome="WIN", pnl_pct=1.5, confidence=75,
    )
    # The episode half still ran — one broken sink must not silence the other.
    assert episodes.calls and episodes.calls[0][0] == "cycle-7"
