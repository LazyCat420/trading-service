"""`shared_desk.desk_data` is JSON TEXT, and every reader has to know it.

`save_desk` stores `json.dumps(desk.to_dict(), default=str)`
(app/v3/desk_persistence.py). MongoDB cannot descend into a string, so any
server-side path into that field — a `{"desk_data.x": ...}` filter, a
`{"desk_data.x.y": 1}` projection — matches nothing and returns nothing. It
does not raise: the document simply comes back without the key, and the caller
reads one fewer row than exists.

Measured on the live store 2026-08-19, same content both ways:

    desk_data as a document -> projection returns preferred_alternative
    desk_data as a string   -> projection returns no `desk_data` key at all

The casualties found by that measurement:

  * `scripts/cycle_healthcheck.py` graded cycle-v3-1787193855 as "verdicts
    produced 0/3" while all three desks held a `final_decision`;
  * `app/v3/substitute_demand.recent_substitute_demand` skipped every
    text-shaped desk. It still answered, because 36 of the 42 desks in its
    72-hour window predated the cutover and were real documents — the six
    written after it were invisible, and once the window rolls past them the
    function returns `{}` in silence.

Both were repaired by parsing the field instead of querying into it. These
tests pin that repair with a desk of each shape.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.v3 import substitute_demand


def _desk(alt_ticker: str | None, as_text: bool):
    data = {
        "phase": "PM_DONE",
        "final_decision": {"action": "HOLD"},
        "bear_rebuttal": {
            "preferred_alternative": (
                {"status": "NAMED", "ticker": alt_ticker} if alt_ticker
                else {"status": "NOT_ASKED"}
            )
        },
    }
    return {"desk_data": json.dumps(data) if as_text else data}


@pytest.fixture()
def store():
    with patch.object(substitute_demand, "mongo_store", MagicMock(), create=True) as m:
        yield m


def _run(docs):
    """substitute_demand imports mongo_store inside the function, so the patch
    has to sit on `app.db.mongo_store` itself, not on the module's globals."""
    from app.db import mongo_store as real
    with patch.object(real, "find_docs", MagicMock(return_value=docs)) as fd:
        return substitute_demand.recent_substitute_demand(), fd


def test_a_text_shaped_desk_still_yields_its_substitute():
    """The regression: this is the shape every desk has had since the cutover."""
    out, _ = _run([_desk("GOOG", as_text=True), _desk("GOOG", as_text=True)])
    assert out == {"GOOG": 2}


def test_a_document_shaped_desk_still_works():
    """Desks written before the cutover are real embedded documents and must
    keep counting — a fix that only handles the new shape loses the history."""
    out, _ = _run([_desk("BHP", as_text=False)])
    assert out == {"BHP": 1}


def test_both_shapes_mix_in_one_window():
    out, _ = _run([_desk("GOOG", as_text=True), _desk("GOOG", as_text=False),
                   _desk("GS", as_text=True), _desk(None, as_text=True)])
    assert out == {"GOOG": 2, "GS": 1}


def test_it_does_not_project_a_path_into_the_text_field():
    """The mechanism, not just the outcome.

    A dotted projection returns no `desk_data` at all for a text-shaped desk,
    so asking for one is how the rows went missing without an error. Reading
    the whole field is what makes the parse possible.
    """
    _, find_docs = _run([_desk("GOOG", as_text=True)])
    projection = find_docs.call_args.kwargs.get("projection") or {}
    assert "desk_data" in projection
    assert not any(k.startswith("desk_data.") for k in projection), (
        f"projection descends into the JSON text: {sorted(projection)}")


def test_a_corrupt_desk_costs_one_row_not_the_call():
    out, _ = _run([{"desk_data": "{not json"}, _desk("EAT", as_text=True)])
    assert out == {"EAT": 1}
