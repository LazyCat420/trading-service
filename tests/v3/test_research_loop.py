"""The research loop must not be able to answer a question it did not research.

The first version of this file called ``run_research_loop_pass(limit=2)`` with
no stubbing, against whatever database the suite was pointed at. The worker it
exercised marked questions `answered` unconditionally, so RUNNING THE TEST
would have closed two real open questions in the production ledger with
fabricated evidence — 22 were open at the time. The tests below stub the DB
boundary and never touch it.
"""
from unittest.mock import patch

import app.autoresearch.research_loop as RL


_QUESTION = {
    "id": 1,
    "ticker": "AAPL",
    "question_hash": "abc123",
    "question": "What is the segment mix after the services restatement?",
    "source_agent": "v3_fundamental_analyst",
    "ask_count": 3,
    "status": "open",
}

_REAL_EVIDENCE = (
    "10-Q filed 2026-07-30, segment note 3: Services revenue restated to "
    "$24.2B, up 11.3% YoY; Products unchanged."
)


def test_a_pass_without_a_resolver_answers_nothing():
    """The default path is read-only. This is the guard on the whole module."""
    with patch.object(RL, "fetch_pending_questions", return_value=[_QUESTION]), \
         patch.object(RL, "mark_question_answered") as marked:
        res = RL.run_research_loop_pass(limit=5)

    marked.assert_not_called()
    assert res["pending_found"] == 1
    assert res["answered"] == 0
    assert res["processed"] == 0
    assert "no resolver" in res["note"]


def test_a_resolver_that_answers_nothing_leaves_the_question_open():
    with patch.object(RL, "fetch_pending_questions", return_value=[_QUESTION]), \
         patch.object(RL, "mark_question_answered") as marked:
        res = RL.run_research_loop_pass(limit=5, resolver=lambda item: None)

    marked.assert_not_called()
    assert res["processed"] == 1
    assert res["answered"] == 0
    assert res["unresolved"] == 1


def test_a_resolver_that_raises_does_not_answer_and_does_not_kill_the_pass():
    def boom(item):
        raise RuntimeError("upstream tool down")

    with patch.object(RL, "fetch_pending_questions",
                      return_value=[_QUESTION, dict(_QUESTION, question_hash="def456")]), \
         patch.object(RL, "mark_question_answered") as marked:
        res = RL.run_research_loop_pass(limit=5, resolver=boom)

    marked.assert_not_called()
    assert res["processed"] == 2
    assert res["unresolved"] == 2


def test_real_evidence_is_passed_through_to_the_writer():
    with patch.object(RL, "fetch_pending_questions", return_value=[_QUESTION]), \
         patch.object(RL, "mark_question_answered", return_value=True) as marked:
        res = RL.run_research_loop_pass(
            limit=5, cycle_id="cycle-42", resolver=lambda item: _REAL_EVIDENCE
        )

    marked.assert_called_once_with(
        ticker="AAPL", qhash="abc123", evidence=_REAL_EVIDENCE, cycle_id="cycle-42"
    )
    assert res["answered"] == 1
    assert res["unresolved"] == 0


def test_a_refused_write_counts_as_unresolved_not_answered():
    """`mark_question_answered` returning False must not inflate `answered`."""
    with patch.object(RL, "fetch_pending_questions", return_value=[_QUESTION]), \
         patch.object(RL, "mark_question_answered", return_value=False):
        res = RL.run_research_loop_pass(limit=5, resolver=lambda item: _REAL_EVIDENCE)

    assert res["answered"] == 0
    assert res["unresolved"] == 1


# ── mark_question_answered's own refusal contract ───────────────────────────


def test_writer_refuses_empty_and_thin_evidence_without_touching_the_db():
    with patch.object(RL, "get_db") as db:
        assert RL.mark_question_answered("AAPL", "abc123", "") is False
        assert RL.mark_question_answered("AAPL", "abc123", "   ") is False
        assert RL.mark_question_answered("AAPL", "abc123", "yes, confirmed.") is False
    db.assert_not_called()


def test_writer_refuses_the_old_fabricated_evidence_shape():
    """The removed worker's f-string was long enough to clear a length check on
    some tickers, so the floor is not the only reason it was wrong — but it
    must at minimum not sail through on a short question."""
    fabricated = "Verified research evidence for X: 'why?' processed at 2026-08-08"
    with patch.object(RL, "get_db") as db:
        RL.mark_question_answered("X", "h", fabricated[:20])
    db.assert_not_called()


def test_writer_refuses_a_missing_ticker_or_hash():
    with patch.object(RL, "get_db") as db:
        assert RL.mark_question_answered("", "abc123", _REAL_EVIDENCE) is False
        assert RL.mark_question_answered("AAPL", "", _REAL_EVIDENCE) is False
    db.assert_not_called()
