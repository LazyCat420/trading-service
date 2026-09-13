"""The recorder must tell a KEEP-HOLD from a flat-wait, or nothing grades.

`claim_type` classifies a HOLD as `flat_wait` only when `hold_reason_held is
False`. That field is NOT on the decision artifact — it lives on the desk, at
`cycle_metadata.held`. `app/v3/challenger.py:114` merges it before calling
`claim_type`; `record_cycle_decisions` did not.

WHAT THAT COST, MEASURED 2026-09-12 (before this fix):

    decisions in 90 days                     1,557
    with a claim_type (i.e. gradeable)           7   (0.4%)
    hold_reason_held present on any desk         0
    HOLDs with cycle_metadata.held is False    502   <- all of them ungradeable

Every one of those 502 was written `outcome_evidence_state='unsupported_claim'`
and skipped by `resolve_pending_outcomes` forever. The desk kept deciding, the
resolver kept running, and the learning loop had no population at all — which
looks exactly like a quiet system.

The three tests below are the house shape: the broken value stated explicitly,
the fix as a no-op where nothing was wrong, and a control proving the fixture
can tell the two apart.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.autoresearch.outcome_evidence import claim_type


DECISION_AT = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)


def _artifact(action: str) -> dict:
    """A decision artifact exactly as `analysis_results.result_json` stores it.

    Note what is NOT here: `hold_reason_held`. That absence is the bug — the
    recorder passed this dict straight to `claim_type`.
    """
    return {"action": action, "confidence": 62, "reasoning": "...",
            "stop_loss": 1.0, "take_profit": 2.0}


def test_a_held_flag_of_false_makes_a_hold_gradeable():
    """The fix: merged `hold_reason_held` turns a HOLD into a flat_wait claim."""
    merged = {**_artifact("HOLD"), "hold_reason_held": False}
    assert claim_type("HOLD", merged) == "flat_wait"


def test_without_the_held_flag_every_hold_is_unsupported():
    """The bug, stated as a value. This is what the recorder produced for 502 rows."""
    assert claim_type("HOLD", _artifact("HOLD")) is None, (
        "a HOLD with no hold_reason_held has no claim_type — which is why "
        "resolve_pending_outcomes had nothing to resolve"
    )


def test_a_held_name_is_still_not_a_forecast():
    """The fix is a no-op where the contract already said no.

    `held is True` is a KEEP decision, not an avoided-decline forecast, and must
    stay ungradeable. A fix that made EVERY hold gradeable would be wrong.
    """
    merged = {**_artifact("HOLD"), "hold_reason_held": True}
    assert claim_type("HOLD", merged) is None


def test_the_fixture_can_tell_the_three_apart():
    """Non-vacuity control: False / True / missing must not collapse."""
    labels = {
        "missing": claim_type("HOLD", _artifact("HOLD")),
        "held_true": claim_type("HOLD", {**_artifact("HOLD"), "hold_reason_held": True}),
        "held_false": claim_type("HOLD", {**_artifact("HOLD"), "hold_reason_held": False}),
    }
    assert labels["held_false"] == "flat_wait"
    assert labels["held_false"] != labels["missing"], (
        "if these were equal the fixture could not detect the fix at all"
    )


def test_recorder_merges_held_from_the_desk():
    """End-to-end on the recorder: the desk's cycle_metadata.held must reach claim_type.

    Proven RED against the pre-fix recorder, which passed `result` unmerged and
    therefore wrote `unsupported_claim` for this row.
    """
    from app.autoresearch import outcome_tracker

    desk_rows = [{
        "ticker": "NVDA",
        # JSON TEXT on purpose: `desk_data` is a string on roughly half the
        # collection after the cutover, and the recorder must handle both.
        "desk_data": json.dumps({"cycle_metadata": {"held": False}}),
    }]
    analysis_rows = [("NVDA", 62, json.dumps(_artifact("HOLD")), DECISION_AT)]
    written: list[dict] = []

    def _find_docs(collection, query, **kw):
        if collection == "shared_desk":
            return desk_rows
        return []

    def _insert_docs(collection, docs):
        if collection == "decision_outcomes":
            written.extend(docs)

    with patch.object(outcome_tracker.mongo_store, "find_docs", _find_docs), \
         patch.object(outcome_tracker.mongo_store, "insert_docs", _insert_docs), \
         patch.object(outcome_tracker.mongo_store, "update_docs", lambda *a, **k: None), \
         patch.object(outcome_tracker.mongo_query, "find_rows",
                      lambda c, q, cols, **kw: analysis_rows if c == "analysis_results" else []), \
         patch.object(outcome_tracker.mongo_query, "find_row", lambda *a, **k: None), \
         patch("app.autoresearch.outcome_evidence.entry_observation",
               lambda t, at: {"price": 100.0, "date": DECISION_AT, "source": "yfinance"}), \
         patch.object(outcome_tracker, "resolve_overridden_from", lambda *a, **k: None), \
         patch.object(outcome_tracker, "write_outcome_to_memory", lambda **k: None):
        outcome_tracker.record_cycle_decisions("cycle-v3-test", {})

    assert written, "the recorder wrote no decision_outcomes row at all"
    row = written[0]
    assert row["claim_type"] == "flat_wait", (
        f"expected flat_wait from cycle_metadata.held=False, got "
        f"{row['claim_type']!r} — the held flag never reached claim_type"
    )
    assert row["outcome_evidence_state"] == "pending", (
        f"a gradeable claim must be pending, got {row['outcome_evidence_state']!r}"
    )
