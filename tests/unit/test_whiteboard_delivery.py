"""The whiteboard delivers what only it has.

Measured 2026-08-08 before this change: the board held ~26,000 chars, delivered
8,077, and 93% of 329 boards hit the cap. 87% of what it did deliver was a
second copy of something the SharedDesk carries anyway, which is what pushed
its own unique payload off the end — `market_context`, written 312 times and
called MANDATORY in the junior analyst's prompt, reached 0 of 39 downstream
readers.

Every test calls the real function. The taxonomy tests in particular exist
because the thing that broke was a hand-maintained list in one file that a new
artifact type in another file never joined.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.agents.whiteboard_sections import (
    ANNOTATIONS_ONLY,
    COLLABORATION,
    CONTROL,
    DESK_CARRIED,
    FULL,
    SKIP,
    classify,
    render_mode,
    sort_key,
)

COLLAB = ("market_context", "risk_flags", "signals", "consensus", "trade_plan")


# ── The taxonomy, and the drift that broke the old one ───────────────────

def test_every_desk_artifact_classifies_as_desk_carried():
    """THE DRIFT GUARD, and the test that would have caught the bug.

    `_SECTION_PRIORITY` listed 15 names while the desk defined 13 artifact
    types, and `bull_defense`, `delta_report` and `trade_decision` were in
    neither — so they sorted after everything and `bull_defense` was delivered
    0 times out of 73. Deriving the class from the desk's own registry means a
    new artifact type cannot be missed; this test pins that it IS derived.
    """
    from app.v3.shared_desk import _VALID_ARTIFACT_TYPES

    for artifact in _VALID_ARTIFACT_TYPES:
        assert classify(artifact) == DESK_CARRIED, (
            f"{artifact} is a SharedDesk artifact but the whiteboard does not "
            f"know it — it will be treated as unique data and duplicated"
        )


def test_the_three_sections_the_old_list_missed():
    """Named explicitly so a regression is legible, not just a count change."""
    for missed in ("bull_defense", "delta_report", "trade_decision"):
        assert classify(missed) == DESK_CARRIED


@pytest.mark.parametrize("section", COLLAB)
def test_collaboration_sections_are_whiteboard_only(section):
    assert classify(section) == COLLABORATION


def test_task_queue_is_control_and_never_summarised():
    assert classify("task_queue") == CONTROL
    for flag in (True, False):
        assert render_mode("task_queue", for_agent_prompt=flag,
                           has_annotations=True) == SKIP


@pytest.mark.parametrize("unknown", ["something_new", "MARKET_NOTES", "", None])
def test_an_unknown_section_is_treated_as_unique_data(unknown):
    """Fail toward keeping it. An unrecognised section is one nothing else
    carries, so guessing 'duplicate' would silently drop data; guessing
    'unique' costs a few hundred characters."""
    assert classify(unknown) == COLLABORATION


def test_classification_survives_the_desk_registry_being_unavailable():
    """A classification failure must never empty a board."""
    with patch.dict("sys.modules", {"app.v3.shared_desk": None}):
        assert classify("desk_note") == DESK_CARRIED     # static fallback
        assert classify("market_context") == COLLABORATION


# ── Ordering: class first, so a duplicate can never displace unique data ──

def test_collaboration_outranks_every_duplicate():
    from app.v3.shared_desk import _VALID_ARTIFACT_TYPES

    worst_collab = max(sort_key(c) for c in COLLAB)
    best_desk = min(sort_key(a) for a in _VALID_ARTIFACT_TYPES)
    assert worst_collab < best_desk, (
        "a desk-carried section can outrank a whiteboard-only one — the "
        "unique payload can be pushed off the end again"
    )


def test_a_brand_new_artifact_type_still_sorts_after_collaboration():
    """The old list put unknown names last of all; the risk now is the reverse
    — an unlisted DESK artifact jumping ahead of real collaboration data."""
    assert sort_key("market_context") < sort_key("some_future_report")


def test_ordering_is_stable_and_total():
    names = list(COLLAB) + ["desk_note", "final_decision", "task_queue", "zz_unknown"]
    assert sorted(names, key=sort_key) == sorted(names, key=sort_key)


# ── The render policy ────────────────────────────────────────────────────

@pytest.mark.parametrize("section", COLLAB)
def test_collaboration_always_renders_in_full(section):
    for flag in (True, False):
        assert render_mode(section, for_agent_prompt=flag,
                           has_annotations=False) == FULL


def test_a_duplicate_body_is_dropped_from_an_agent_prompt():
    assert render_mode("desk_note", for_agent_prompt=True,
                       has_annotations=False) == SKIP


def test_but_its_annotations_are_not():
    """THE REGRESSION THIS ALMOST SHIPPED. The desk carries the artifact; it
    does NOT carry the notes teammates wrote on it, and nothing else does.
    324 of 518 annotations attach to desk_note (227) and fundamental_report
    (97) — dropping body and notes together would trade one data loss for
    another, and silence the channel the prompts call load-bearing."""
    assert render_mode("desk_note", for_agent_prompt=True,
                       has_annotations=True) == ANNOTATIONS_ONLY


def test_an_explicit_read_gets_the_whole_board():
    """`whiteboard_read`/`whiteboard_summarize` asked for the board. Answering
    with a filtered view would be its own kind of lie."""
    for section in ("desk_note", "final_decision", "market_context"):
        assert render_mode(section, for_agent_prompt=False,
                           has_annotations=False) == FULL


# ── summarize() end to end, against a fake board ─────────────────────────

def _entry(eid, section, body, version=1):
    """One `whiteboard_entries` document as `mongo_store.find_docs` returns it.

    These used to be tuples fed through a fake `db.execute`/`fetchall` handle
    that dispatched on SQL text. `summarize()` reads `mongo_store.find_docs`
    now, which returns DICTS — a patched `get_db` intercepted nothing and the
    board under test was whatever production held.
    """
    return {
        "id": eid,
        "section": section,
        "author_agent": "v3_junior_analyst",
        "content": json.dumps({"text": body}),
        "version": version,
        "edited_by": ["v3_junior_analyst"],
    }


BOARD = [
    _entry(1, "desk_note", "DESK BODY " * 20),
    _entry(2, "market_context", "MANDATORY HANDOFF"),
    _entry(3, "final_decision", "DECISION BODY " * 20),
    _entry(4, "signals", "QUANT SIGNAL"),
]
ANNS = [{
    "section": "desk_note",
    "author_agent": "v3_quant_analyst",
    "note": "DISPUTE: leverage is worse",
}]


async def _summarize(entries, anns=(), *, for_agent_prompt):
    """Run the real summarize() against a fake board.

    Dispatch is on the COLLECTION name, never on query text: entries and
    annotations are two different collections, so a summarize() that read the
    wrong one gets the wrong list instead of silently the right one.
    """
    from app.agents.whiteboard import whiteboard

    store = MagicMock()
    store.find_docs.side_effect = lambda coll, *a, **k: {
        "whiteboard_entries": list(entries),
        "whiteboard_annotations": list(anns),
    }[coll]
    query = MagicMock()
    query.find_row.return_value = None
    query.find_rows.return_value = []

    with patch("app.agents.whiteboard.mongo_store", store), \
         patch("app.agents.whiteboard.mongo_query", query):
        out = await whiteboard.summarize(
            ticker="TSLA", cycle_id="c1", for_agent_prompt=for_agent_prompt
        )

    # The read must be scoped to the ticker and cycle asked for, and must
    # exclude superseded versions — a board that leaked another ticker's
    # entries would still satisfy every content assertion below.
    entries_call = next(
        c for c in store.find_docs.call_args_list if c[0][0] == "whiteboard_entries"
    )
    assert entries_call[0][1] == {
        "cycle_id": "c1", "ticker": "TSLA", "superseded_by": None
    }
    return out


@pytest.mark.asyncio
async def test_the_agent_prompt_keeps_unique_data_and_drops_duplicates():
    out = await _summarize(BOARD, ANNS, for_agent_prompt=True)
    assert "MANDATORY HANDOFF" in out, "market_context is the whole point"
    assert "QUANT SIGNAL" in out
    assert "DESK BODY" not in out, "the desk already delivered this body"
    assert "DECISION BODY" not in out


@pytest.mark.asyncio
async def test_the_annotation_on_a_dropped_section_still_arrives():
    out = await _summarize(BOARD, ANNS, for_agent_prompt=True)
    assert "DISPUTE: leverage is worse" in out
    assert "DESK_NOTE" in out, "its header must remain so the note has a subject"
    assert "DESK BODY" not in out, "...but not its body"


@pytest.mark.asyncio
async def test_an_explicit_call_renders_everything():
    out = await _summarize(BOARD, ANNS, for_agent_prompt=False)
    for expected in ("MANDATORY HANDOFF", "DESK BODY", "DECISION BODY", "QUANT SIGNAL"):
        assert expected in out


@pytest.mark.asyncio
async def test_collaboration_is_rendered_before_any_duplicate():
    out = await _summarize(BOARD, ANNS, for_agent_prompt=False)
    assert out.index("MARKET_CONTEXT") < out.index("DESK_NOTE")
    assert out.index("SIGNALS") < out.index("FINAL_DECISION")


@pytest.mark.asyncio
async def test_an_empty_board_costs_no_tokens():
    assert await _summarize([], for_agent_prompt=True) == ""


@pytest.mark.asyncio
async def test_a_board_of_only_unannotated_duplicates_renders_nothing():
    """A header promising a whiteboard, followed by no whiteboard, is worse
    than no block at all — the caller injects nothing when this is empty."""
    dupes = [_entry(1, "desk_note", "x"), _entry(3, "final_decision", "y")]
    assert await _summarize(dupes, for_agent_prompt=True) == ""


# ── Wiring ───────────────────────────────────────────────────────────────

def test_the_agent_prompt_caller_asks_for_the_deduped_view():
    """A guarded callee does not protect its call site: `for_agent_prompt`
    defaults to False, so the one caller that needs it must pass it.

    Reads the CALL, not the file. A substring check stayed green when the
    argument was deleted, because the comment above it explains the argument by
    name — the probe was reading prose about the code instead of the code.
    """
    import ast
    import inspect

    from app.v3 import agent_runner

    tree = ast.parse(inspect.getsource(agent_runner))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "summarize"
    ]
    assert calls, "agent_runner no longer calls whiteboard.summarize at all"
    assert any(
        any(kw.arg == "for_agent_prompt"
            and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in c.keywords)
        for c in calls
    ), (
        "agent_runner no longer requests the deduped whiteboard — every agent "
        "is back to receiving 87% duplicated content"
    )


def test_the_reserved_write_guard_covers_every_desk_artifact():
    """The hand-listed guard named 11 sections against 13 artifact types, so
    valuation_report, bull_defense and delta_report were writable by any agent
    holding the tool."""
    from app.tools.whiteboard_tools import _is_reserved
    from app.v3.shared_desk import _VALID_ARTIFACT_TYPES

    for artifact in _VALID_ARTIFACT_TYPES:
        assert _is_reserved(artifact), f"{artifact} is writable by an agent"
    assert _is_reserved("task_queue")
    for collab in COLLAB:
        assert not _is_reserved(collab), f"{collab} is what agents are FOR"


def test_the_tool_no_longer_advertises_sections_nothing_writes():
    """377 reads against `consensus` and `trade_plan`, 376 empty. An
    advertised surface that does not exist costs a turn every time."""
    from app.tools.registry import registry

    desc = ""
    for name in ("whiteboard_read",):
        tool = registry.get(name) if hasattr(registry, "get") else None
        if tool is not None:
            desc = getattr(tool, "description", "") or ""
    if not desc:
        import inspect

        from app.tools import whiteboard_tools

        desc = inspect.getsource(whiteboard_tools)
        desc = desc[desc.index("Read a section"):desc.index("Omit section")]
    assert "'consensus'" not in desc and "'trade_plan'" not in desc


