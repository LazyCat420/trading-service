"""Tests for the bear's substitute.

Every test calls the real function. None of them re-implement the branch logic
and assert against the copy — that shape let a blocked trade read as a kept one
for weeks, because production was free to diverge from the test's private copy
of the rule.

The section on CALL SITES exists because `classify_hold` shipped correct and
produced no labels for a week: the callee handled the delta tier, and the only
call site sat 1,600 lines above the delta tier's `return`. A guarded callee
does not protect its call site.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.v3.cycle_candidates import (
    build_candidate_block,
    shown_rows,
    shown_tickers,
)
from app.v3.substitute import (
    DECLINED,
    FIELD,
    NAMED,
    NOT_ASKED,
    OFF_POOL,
    POOL_KEY,
    UNANSWERED,
    apply_substitute,
    canonical_field,
    read_pool,
    read_record,
    read_substitute,
    substitute_block,
    substitute_context,
)

POOL = ["PLTR", "ABNB", "NVDA"]


def _desk(pool=POOL, ticker="TSLA"):
    meta = {}
    if pool is not None:
        meta[POOL_KEY] = list(pool)
    return SimpleNamespace(ticker=ticker, cycle_metadata=meta)


def _artifact(value, present=True):
    art = {"summary": "the bull is wrong", "confidence": 70}
    if present:
        art[FIELD] = value
    return art


# ── The five states ──────────────────────────────────────────────────────

def test_a_name_from_the_pool_is_named():
    rec = read_substitute(_artifact({"ticker": "PLTR", "reason": "cheaper"}), pool=POOL)
    assert rec["status"] == NAMED
    assert rec["ticker"] == "PLTR"
    assert rec["reason"] == "cheaper"


def test_an_explicit_null_is_declined_not_a_failure():
    """THE LOAD-BEARING RULE. 'None of them is better' is a real answer, and a
    validator that rejected it would manufacture preferences the agent does not
    hold — the same defect as the HOLD it replaces, wearing a new label."""
    rec = read_substitute(
        _artifact({"ticker": None, "reason": "all worse on quality"}), pool=POOL
    )
    assert rec["status"] == DECLINED
    assert rec["ticker"] is None
    assert rec["reason"] == "all worse on quality"


def test_a_bare_null_field_is_also_declined():
    """`"preferred_alternative": null` is an answer, not an omission."""
    assert read_substitute(_artifact(None), pool=POOL)["status"] == DECLINED


def test_a_missing_field_is_unanswered_not_declined():
    """The agent that never answered and the agent that said 'none' are
    different populations: one is a coverage failure to fix, the other is the
    feature working. Pooling them would hide exactly the number that says
    whether this shipped."""
    assert read_substitute(_artifact(None, present=False), pool=POOL)["status"] == UNANSWERED


def test_a_name_outside_the_pool_is_off_pool_and_never_a_substitute():
    """Fail-closed. A ticker from parametric memory is unscored, unpriced and
    possibly unlisted — the desk would rather hold than route capital at a name
    nothing on the desk has priced."""
    rec = read_substitute(_artifact({"ticker": "GME", "reason": "meme"}), pool=POOL)
    assert rec["status"] == OFF_POOL
    assert rec["ticker"] is None, "an unpriceable name must never read as a substitute"
    assert rec["rejected_ticker"] == "GME"


def test_an_empty_pool_is_not_asked():
    """A Watch Desk wake names one ticker, bypasses discovery and has no pool.
    That is not a declension and must never be counted as one."""
    rec = read_substitute(_artifact({"ticker": "PLTR"}), pool=[])
    assert rec["status"] == NOT_ASKED
    assert rec["ticker"] is None
    assert rec["pool_size"] == 0


def test_not_asked_wins_even_over_a_perfectly_formed_answer():
    """With no pool there is nothing to have been shown, so no answer about it
    can be honoured — including one that happens to name a real ticker."""
    assert read_substitute(_artifact("PLTR"), pool=[])["status"] == NOT_ASKED


# ── The shapes models actually emit ──────────────────────────────────────

def test_a_bare_string_is_an_answer():
    """Making a correct answer re-emit as an object to be heard would discard
    it on a formatting technicality."""
    rec = read_substitute(_artifact("PLTR"), pool=POOL)
    assert rec["status"] == NAMED and rec["ticker"] == "PLTR"


@pytest.mark.parametrize("raw", ["pltr", " PLTR ", "$PLTR", "PLTR.US", "$pltr "])
def test_ticker_spellings_normalise(raw):
    assert read_substitute(_artifact(raw), pool=POOL)["ticker"] == "PLTR"


@pytest.mark.parametrize(
    "word", ["none", "NONE", "null", "N/A", "nothing", "", "  ", "none of them"]
)
def test_words_meaning_none_are_declensions(word):
    assert read_substitute(_artifact(word), pool=POOL)["status"] == DECLINED


@pytest.mark.parametrize("stance", ["HOLD", "NEUTRAL"])
def test_a_stance_is_not_read_as_a_declension(stance):
    """'HOLD' is an answer to a different question. Treating it as 'none is
    better' would silently score a malformed answer as an honest one."""
    assert read_substitute(_artifact(stance), pool=POOL)["status"] == OFF_POOL


def test_a_one_element_list_is_unambiguous():
    rec = read_substitute(_artifact([{"ticker": "ABNB"}]), pool=POOL)
    assert rec["status"] == NAMED and rec["ticker"] == "ABNB"


def test_a_ranked_list_is_not_a_preference():
    """'Rank the alternatives' is not what was asked; a ranking is not a pick."""
    rec = read_substitute(_artifact([{"ticker": "ABNB"}, {"ticker": "PLTR"}]), pool=POOL)
    assert rec["status"] == UNANSWERED


def test_the_reason_survives_a_symbol_only_key():
    rec = read_substitute(
        _artifact({"symbol": "NVDA", "why": "better operator"}), pool=POOL
    )
    assert rec["status"] == NAMED and rec["ticker"] == "NVDA"
    assert rec["reason"] == "better operator"


@pytest.mark.parametrize("junk", [42, 4.2, True, {"reason": "prose only"}, [], ()])
def test_junk_never_becomes_a_substitute(junk):
    rec = read_substitute(_artifact(junk), pool=POOL)
    assert rec["status"] in (UNANSWERED, OFF_POOL, DECLINED)
    assert rec["ticker"] is None


@pytest.mark.parametrize("junk", [None, "not-a-dict", 42, []])
def test_a_malformed_artifact_does_not_raise(junk):
    assert read_substitute(junk, pool=POOL)["status"] == UNANSWERED


# ── The pool has ONE definition ──────────────────────────────────────────

def _candidates():
    return [
        {"ticker": "PLTR", "score": 9.0, "chg": 2.0, "rvol": 1.5, "sector": "Tech"},
        {"ticker": "TSLA", "score": 8.0, "chg": 1.0, "rvol": 1.2, "sector": "Auto"},
        {"ticker": "ABNB", "score": 7.0, "chg": -1.0, "rvol": 1.1, "sector": "Travel"},
    ]


def test_the_rendered_block_and_the_validated_pool_agree():
    """A validator rejecting a ticker the agent was genuinely shown is
    unfixable from inside the prompt: the agent cannot see the rejection and
    cannot know which list was meant. Both readers use `shown_rows`."""
    block = build_candidate_block(_candidates(), self_ticker="TSLA")
    pool = shown_tickers(_candidates(), self_ticker="TSLA")
    for tk in pool:
        assert f"| {tk} |" in block, f"{tk} is in the pool but not in the block"
    for c in _candidates():
        in_block = f"| {c['ticker']} |" in block
        assert in_block == (c["ticker"] in pool), c["ticker"]


def test_the_desks_own_name_is_in_neither():
    assert "TSLA" not in shown_tickers(_candidates(), self_ticker="TSLA")
    assert "| TSLA |" not in build_candidate_block(_candidates(), self_ticker="TSLA")


def test_naming_the_desks_own_ticker_is_off_pool():
    """'Own TSLA instead of TSLA' is not a substitution, and the desk's own
    name is excluded from the list precisely so it cannot be picked."""
    pool = shown_tickers(_candidates(), self_ticker="TSLA")
    assert read_substitute(_artifact("TSLA"), pool=pool)["status"] == OFF_POOL


def test_shown_rows_survives_junk_entries():
    rows = shown_rows([None, "PLTR", {"ticker": ""}, {"ticker": "PLTR"}], self_ticker="X")
    assert [r["ticker"] for r in rows] == ["PLTR"]


# ── Reading the pool off a desk ──────────────────────────────────────────

def test_read_pool_normalises_and_drops_blanks():
    desk = _desk(pool=["pltr", " ABNB ", "", None, "$NVDA"])
    assert read_pool(desk) == ["PLTR", "ABNB", "NVDA"]


@pytest.mark.parametrize("desk", [
    SimpleNamespace(cycle_metadata={}),
    SimpleNamespace(cycle_metadata=None),
    SimpleNamespace(),
    SimpleNamespace(cycle_metadata={POOL_KEY: None}),
])
def test_read_pool_never_raises(desk):
    assert read_pool(desk) == []


# ── apply_substitute: the artifact and the desk ──────────────────────────

def test_apply_writes_the_canonical_field_and_publishes_the_record():
    desk = _desk()
    art = apply_substitute(_artifact({"ticker": "pltr", "reason": "cheaper"}), desk=desk)
    assert art[FIELD] == {
        "ticker": "PLTR", "reason": "cheaper", "status": NAMED, "rejected_ticker": None,
    }
    assert art["_substitute_status"] == NAMED
    assert read_record(desk)["ticker"] == "PLTR"


def test_the_canonical_field_is_written_even_when_nothing_was_named():
    """So the absence of a key never has to be read as 'this ran before the
    field existed' — every stored bear artifact states its own status."""
    for value, present, expected in (
        (None, True, DECLINED),
        (None, False, UNANSWERED),
        ({"ticker": "GME"}, True, OFF_POOL),
    ):
        art = apply_substitute(_artifact(value, present=present), desk=_desk())
        assert art[FIELD]["status"] == expected
        assert art[FIELD]["ticker"] is None


def test_apply_on_an_empty_pool_records_not_asked():
    art = apply_substitute(_artifact({"ticker": "PLTR"}), desk=_desk(pool=None))
    assert art[FIELD]["status"] == NOT_ASKED


@pytest.mark.parametrize("desk", [
    SimpleNamespace(),                      # no cycle_metadata at all
    SimpleNamespace(cycle_metadata="junk"),  # not a dict
])
def test_a_broken_desk_never_costs_the_bear_case(desk):
    """Non-fatal by construction: this runs inside the artifact pipeline, where
    an exception would discard a complete rebuttal over a label."""
    art = apply_substitute(_artifact("PLTR"), desk=desk)
    assert art["summary"] == "the bull is wrong"


@pytest.mark.parametrize("art", [None, "not-a-dict", 42, []])
def test_apply_passes_through_a_non_dict_artifact(art):
    assert apply_substitute(art, desk=_desk()) is art


def test_a_raising_pool_read_does_not_lose_the_artifact():
    class Exploding:
        ticker = "TSLA"

        @property
        def cycle_metadata(self):
            raise RuntimeError("boom")

    art = apply_substitute(_artifact("PLTR"), desk=Exploding())
    assert art["summary"] == "the bull is wrong"


# ── The deciders' block ──────────────────────────────────────────────────

def test_named_renders_the_ticker_and_the_reason():
    block = substitute_block(
        {"status": NAMED, "ticker": "PLTR", "reason": "cheaper on every multiple"}
    )
    assert "PLTR" in block and "cheaper on every multiple" in block
    assert "DISCOVERY screen" in block, (
        "the block must say the named name is unresearched, or the board reads "
        "a screen rank as a buy recommendation"
    )


def test_declined_renders_nothing():
    """Telling the board 'the bear was asked and had none' reads as
    corroboration of the name. A declension means the bear had no better idea,
    NOT that this one is good — rendering it turns an absence of information
    into a bullish input."""
    assert substitute_block({"status": DECLINED, "ticker": None, "reason": "all worse"}) == ""


@pytest.mark.parametrize("status", [UNANSWERED, NOT_ASKED])
def test_non_answers_render_nothing(status):
    assert substitute_block({"status": status, "ticker": None}) == ""


def test_off_pool_is_reported_but_not_as_an_alternative():
    block = substitute_block({"status": OFF_POOL, "ticker": None, "rejected_ticker": "GME"})
    assert "GME" in block
    assert "cannot price" in block.lower() or "not treated as an alternative" in block.lower()


@pytest.mark.parametrize("rec", [None, "junk", 42, {}, {"status": "WAT"}])
def test_the_block_never_raises(rec):
    assert substitute_block(rec) == ""


def test_substitute_context_reads_the_desk():
    desk = _desk()
    assert substitute_context(desk) == ""          # bear has not run
    apply_substitute(_artifact({"ticker": "PLTR", "reason": "cheaper"}), desk=desk)
    assert "PLTR" in substitute_context(desk)


def test_canonical_field_of_an_empty_record():
    assert canonical_field({}) == {
        "ticker": None, "reason": "", "status": None, "rejected_ticker": None,
    }


# ── CALL SITES ───────────────────────────────────────────────────────────
#
# The surface shipped on 2026-08-08 and changed no decision, because nothing
# required the bear to use it. These tests pin the wiring, not the callee.

def _src(*parts):
    return (Path(__file__).resolve().parents[2].joinpath(*parts)).read_text()


def test_the_runner_normalises_the_bear_artifact():
    """`apply_substitute` needs the desk, which `artifact_validators` dispatch
    does not carry — so this one call site is the only place it can run."""
    src = _src("app", "v3", "agent_runner.py")
    assert "apply_substitute" in src, "the bear's answer is never normalised"
    tree = ast.parse(src)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "apply_substitute"
    ]
    assert calls, "apply_substitute is imported but never called"
    assert any(kw.arg == "desk" for c in calls for kw in c.keywords), (
        "apply_substitute must be given the desk — the pool lives on it"
    )


def test_both_deciding_agents_are_shown_the_substitute():
    """A preference is only actionable if whoever writes the action can see
    what it is a preference FOR.

    Reads the `if` that OWNS the injection, not the text near it: the
    neighbouring candidate-block gate names the same two agents, so a proximity
    check stays green when the substitute gate is disabled outright.
    """
    tree = ast.parse(_src("app", "v3", "agent_runner.py"))
    owners = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and "substitute_context" in ast.dump(ast.Module(body=n.body, type_ignores=[]))
    ]
    assert owners, "nothing injects the substitute block"
    gates = [ast.dump(n.test) for n in owners]
    assert any(
        "v3_board_of_directors" in g and "v3_decision_synthesizer" in g for g in gates
    ), "the substitute block must be gated ON the two agents that issue an action"


def test_the_bear_is_told_about_the_field_it_must_fill():
    """A schema field no prompt mentions is a field no model fills."""
    from app.v3.agents.bear_agent import SYSTEM_PROMPT

    assert FIELD in SYSTEM_PROMPT
    assert "null" in SYSTEM_PROMPT.lower(), "the null case must be stated to the agent"


def test_the_field_is_in_the_bear_schema_but_not_required():
    """Requiring it would stamp a validation warning on every Watch Desk wake
    for not answering a question that was never asked."""
    from app.v3.artifacts import ARTIFACT_SCHEMAS

    schema = ARTIFACT_SCHEMAS["bear_rebuttal"]
    assert FIELD in schema["properties"]
    assert FIELD not in schema.get("required", [])


def test_the_pool_is_bound_wherever_the_block_is():
    """The pool and the rendered block must be one fact. Two conditions is how
    a bear gets shown names it is then told it may not pick."""
    tree = ast.parse(_src("app", "v3", "orchestrator.py"))
    bodies = [
        node.body for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "cycle_candidates_context" in ast.dump(node)
    ]
    assert bodies, "nothing assigns the candidate block any more"
    # Either spelling counts: the imported constant (what it uses today) or the
    # raw key. The invariant is the BRANCH, not how the key is named.
    dumps = [ast.dump(ast.Module(body=b, type_ignores=[])) for b in bodies]
    assert any("POOL_KEY" in d or POOL_KEY in d for d in dumps), (
        "the candidate pool must be bound in the same branch as the block it "
        "was rendered from"
    )
