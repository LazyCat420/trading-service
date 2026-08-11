"""
Bull Defense downstream propagation (2026-08-10 cycle audit).

The defense artifact demands defense_points, concessions, thesis_survives,
final_confidence and independent_risks_answered — and only `summary` was ever
read. This adds the two SCALARS and deliberately withholds the arrays: on
cycle-v3-1786401874 META's defense_points alone ran to 5,060 chars, and the
judge and board were already logging 6.6-11.1k tokens of non-sheddable context
against a 2,048-token embedder that same cycle.
"""

from app.v3.shared_desk import SharedDesk


def _desk_with_defense(**overrides):
    desk = SharedDesk(cycle_id="cycle-test", ticker="META")
    desk.bull_argument = {"summary": "Bull opening.", "confidence": 70}
    desk.bear_rebuttal = {"summary": "Bear rebuttal.", "confidence": 65}
    desk.bull_defense = {
        "summary": "The thesis survives in narrowed form.",
        "final_confidence": 65,
        "thesis_survives": True,
        "defense_points": [{"defense": "D" * 5000}],
        "concessions": [{"conceded_point": "C" * 1500}],
        "independent_risks_answered": [{"risk": "R" * 1400}],
        **overrides,
    }
    return desk


def test_the_defense_confidence_reaches_the_judge_and_board():
    """bull and bear have always carried their confidence into the context;
    the defense silently did not, so its final number was invisible."""
    ctx = _desk_with_defense().get_compressed_context(include_debate=True)
    assert "Bull Final Defense" in ctx
    assert "65%" in ctx


def test_the_survival_verdict_is_stated():
    ctx = _desk_with_defense().get_compressed_context(include_debate=True)
    assert "thesis SURVIVES" in ctx


def test_a_failed_defense_says_so():
    ctx = _desk_with_defense(thesis_survives=False).get_compressed_context(include_debate=True)
    assert "DOES NOT survive" in ctx


def test_a_string_boolean_is_understood():
    """Models return "true" as often as true."""
    ctx = _desk_with_defense(thesis_survives="true").get_compressed_context(include_debate=True)
    assert "thesis SURVIVES" in ctx


def test_a_missing_verdict_is_not_reported_as_a_failure():
    desk = _desk_with_defense()
    del desk.bull_defense["thesis_survives"]
    ctx = desk.get_compressed_context(include_debate=True)
    assert "verdict unstated" in ctx
    assert "DOES NOT survive" not in ctx


def test_the_bulk_arrays_stay_out_of_the_prompt():
    """The point of the split. Adding ~2k tokens to the two agents already
    overflowing the shed budget would buy a restatement of `summary`."""
    ctx = _desk_with_defense().get_compressed_context(include_debate=True)
    assert "D" * 100 not in ctx, "defense_points must not be inlined"
    assert "C" * 100 not in ctx, "concessions must not be inlined"
    assert "R" * 100 not in ctx, "independent_risks_answered must not be inlined"


def test_the_defense_section_stays_small():
    ctx = _desk_with_defense().get_compressed_context(include_debate=True)
    section = ctx.split("## Bull Final Defense", 1)[1]
    assert len(section.split("##")[0]) < 400


def test_no_defense_on_the_desk_adds_no_section():
    desk = _desk_with_defense()
    desk.bull_defense = None
    assert "Bull Final Defense" not in desk.get_compressed_context(include_debate=True)


def test_debate_context_off_excludes_the_defense():
    assert "Bull Final Defense" not in _desk_with_defense().get_compressed_context(
        include_debate=False
    )
