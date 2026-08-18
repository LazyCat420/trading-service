"""SkillOpt gate — does it actually discriminate, or is it a rubber stamp?

2026-07-25 audit, measured against the real `agent_skills` table:

  - **137 of 145 stored versions were REPLACE**, and no SKIP was ever stored,
    despite the prompt saying "SKIP is the correct default".
  - The board agent's doc grew **1146 -> 1812 chars over 20 versions**, past
    the 1500 the prompt asked for, because MAX_SKILL_CHARS was 4000 — the
    stated limit was never enforced.
  - **6 of 7 accepted edits scored exactly +0.0150** (the maximum possible) and
    **all 66 recorded rejections scored exactly -0.0050**. The gate emitted one
    of two values, so it ranked nothing.
  - One accepted version's only change was renaming a bullet
    ("Conviction-Winrate Scaling" -> "Dynamic Conviction Scaling") with a
    byte-identical body. Whole-doc similarity ran 0.84-0.94 on real edits, so
    the 0.95 near-noop threshold never fired.
  - Fed a genuine edit and deliberate keyword soup, the old scorer rated the
    **soup higher** — "contains a digit" and "contains an imperative verb" are
    satisfied by any rewrite.

These tests pin the fix. They assert RELATIVE ordering wherever possible, not
absolute scores, so tuning the weights does not break them — the property that
matters is that better edits outrank worse ones.
"""
from __future__ import annotations

import pytest

from app.autoresearch import skill_optimizer as S


_CURRENT = "\n".join([
    "- **Sizing**: Always cap position size at 5% of equity.",
    "- **Freshness**: Never trade on price data older than 96h.",
    "- **Stops**: Always require an explicit stop-loss before entry.",
    "- **Regime**: Prefer trend-following setups when VIX is below 20.",
])

_REFLECTION = {"recommendations": [
    "Address the negative expectancy by tightening stop-loss protocols",
    "Recalibrate the confidence scoring mechanism",
    "Resolve data ingestion failures",
]}


def _delta(candidate: str, current: str = _CURRENT) -> float:
    return S._simulate_score_with_skill(candidate, current, 0.5, _REFLECTION) - 0.5


# ── Renames are not learning ────────────────────────────────────────

def test_pure_rename_is_rejected():
    """The exact 2026-07-25 pathology: a bullet's LABEL changes and its body
    does not. Whole-doc similarity cannot catch this; bullet-level can."""
    renamed = _CURRENT.replace("**Sizing**", "**Position Sizing**")
    substantive, reason = S._substantive_change(renamed, _CURRENT)
    assert not substantive, "a pure rename passed as a real edit"
    assert "no_bullet_changed" in reason


def test_rename_is_rejected_structurally_not_by_score():
    """A rename must be blocked outright, not out-pointed. The old near-noop
    check lived in the SCORER, where content bonuses could outweigh it."""
    renamed = _CURRENT.replace("**Sizing**", "**Position Sizing**")
    assert _delta(renamed) > 0, "precondition: this rename scores positively"
    substantive, _ = S._substantive_change(renamed, _CURRENT)
    assert not substantive, "score must not be able to rescue a rename"


def test_reformatting_is_not_a_substantive_change():
    reformatted = _CURRENT.replace("- **", "* **").replace("Always", "always")
    substantive, _ = S._substantive_change(reformatted, _CURRENT)
    assert not substantive


def test_a_genuinely_new_bullet_is_substantive():
    added = _CURRENT + (
        "\n- **Expectancy Veto**: Always veto entries where the projected "
        "average loss exceeds the projected average win."
    )
    substantive, reason = S._substantive_change(added, _CURRENT)
    assert substantive, f"a real new rule was rejected: {reason}"


def test_dropping_a_bullet_is_substantive():
    """The prompt asks the model to drop bullets that no longer earn their
    space, so a pure deletion must be a legal edit."""
    pruned = "\n".join(_CURRENT.splitlines()[:3])
    substantive, reason = S._substantive_change(pruned, _CURRENT)
    assert substantive, f"pruning was rejected: {reason}"


def test_first_ever_doc_is_substantive():
    substantive, _ = S._substantive_change(_CURRENT, "")
    assert substantive


# ── The scorer must rank quality, not just emit two values ──────────

def test_a_genuine_edit_outranks_keyword_soup():
    """The inversion that made this audit worth doing: the old scorer rated
    deliberate keyword stuffing ABOVE a real edit."""
    good = _CURRENT + (
        "\n- **Expectancy Veto**: Always veto entries where the projected "
        "average loss exceeds the projected average win; require tightening "
        "stop-loss when cycle expectancy is negative."
    )
    soup = _CURRENT + (
        "\n- **Mandate**: Mandate mandate mandate veto veto 99 42 7 mandate "
        "recalibrate mandate confidence mandate scoring mandate expectancy "
        "mandate tightening mandate."
    )
    assert _delta(good) > _delta(soup), (
        "keyword soup scores at least as well as a genuine edit — the gate is "
        "rewarding surface features, not content"
    )


def test_keyword_soup_does_not_clear_the_bar():
    soup = _CURRENT + (
        "\n- **Mandate**: Mandate mandate mandate veto veto 99 42 7 mandate "
        "recalibrate mandate confidence mandate scoring mandate expectancy "
        "mandate tightening mandate."
    )
    assert _delta(soup) <= S.MIN_SCORE_DELTA


def test_a_genuine_edit_clears_the_bar():
    """The complement — the gate must not be tightened into rejecting
    everything, which would be its own silent failure."""
    good = _CURRENT + (
        "\n- **Expectancy Veto**: Always veto entries where the projected "
        "average loss exceeds the projected average win; require tightening "
        "stop-loss when cycle expectancy is negative."
    )
    assert _delta(good) > S.MIN_SCORE_DELTA


def test_the_scorer_is_not_binary():
    """The old gate emitted +0.0150 or -0.0050 and nothing else. Distinct
    candidates must produce distinct scores or the gate ranks nothing."""
    cands = [
        _CURRENT + "\n- **A**: Always veto trades when expectancy is negative below 1.2 R:R.",
        _CURRENT + "\n- **B**: Prefer setups with confirmation.",
        _CURRENT + "\n- **C**: Mandate mandate mandate mandate mandate mandate mandate.",
        "\n".join(_CURRENT.splitlines()[:3]),
    ]
    scores = {round(_delta(c), 5) for c in cands}
    assert len(scores) >= 3, f"gate collapses distinct candidates onto {scores}"


# ── Bloat control ───────────────────────────────────────────────────

def test_bloat_is_penalized():
    bloated = _CURRENT + "\n" + "\n".join(
        f"- **R{i}**: Always verify indicator {i} and cap exposure at {i}%."
        for i in range(14)
    )
    assert _delta(bloated) < _delta(_CURRENT + "\n- **New**: Always veto negative expectancy setups below 1.2 R:R."), (
        "a 18-bullet doc scored no worse than a tidy one"
    )


def test_enforced_limit_is_not_looser_than_the_requested_one():
    """MAX_SKILL_CHARS was 4000 while the prompt asked for 1500 — which is why
    the board doc drifted to 1812 with nothing objecting."""
    assert S.MAX_SKILL_CHARS >= S.TARGET_SKILL_CHARS
    assert S.MAX_SKILL_CHARS <= S.TARGET_SKILL_CHARS * 1.5, (
        "the enforced ceiling is far looser than the requested size, so the "
        "requested size is decorative"
    )


def test_prompt_states_the_enforced_limit():
    prompt = S._build_optimizer_prompt("v3_board_of_directors", "Board.", _CURRENT, _REFLECTION)
    assert str(S.TARGET_SKILL_CHARS) in prompt
    assert "4000" not in prompt


def test_prompt_forbids_renames_explicitly():
    """The model cannot avoid a failure mode nobody told it about."""
    prompt = S._build_optimizer_prompt("v3_board_of_directors", "Board.", _CURRENT, _REFLECTION)
    assert "renam" in prompt.lower()


# ── Bullet parsing ──────────────────────────────────────────────────

@pytest.mark.parametrize("line,expected_body_fragment", [
    ("- **Label**: Always cap size at 5%.", "always cap size at 5%"),
    ("* Different Label: Always cap size at 5%.", "always cap size at 5%"),
    ("- Always cap size at 5%.", "always cap size at 5%"),
])
def test_bullet_normalization_strips_the_label(line, expected_body_fragment):
    """Two bullets with the same body under different labels must compare
    equal — that is the whole mechanism behind rename detection."""
    got = S._bullets(line)
    assert got and expected_body_fragment in got[0], got


def test_bullets_ignores_prose():
    doc = "Some heading text\n- **A**: Always do the thing.\nTrailing prose."
    assert len(S._bullets(doc)) == 1


# ── An over-budget doc must be able to shrink ───────────────────────
#
# Tightening MAX_SKILL_CHARS from 4000 to 1800 left 5 of 7 live docs over the
# target and the board's (1811) over the ceiling. Without an escape hatch an
# over-budget doc is frozen: every candidate near its size is rejected, so it
# can never come back down.

def test_over_budget_doc_is_told_to_shrink():
    over = "- **X**: Always do the thing with threshold 5%. " * 45
    assert len(over) > S.TARGET_SKILL_CHARS, "precondition"
    prompt = S._build_optimizer_prompt("v3_board_of_directors", "Board.", over, _REFLECTION)
    assert "OVER BUDGET" in prompt, "the model was never told to shrink"


def test_in_budget_doc_is_not_told_to_shrink():
    prompt = S._build_optimizer_prompt("v3_board_of_directors", "Board.", _CURRENT, _REFLECTION)
    assert "OVER BUDGET" not in prompt
