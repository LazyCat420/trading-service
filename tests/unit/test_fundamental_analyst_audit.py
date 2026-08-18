"""Tests for the 2026-07-24 Fundamental Analyst audit (Phase 3).

Measured over 322 real reports.

- CORRECTED 2026-07-24: the original read ("BULLISH calls averaged -0.54%")
  came from 53 resolved outcomes and does NOT survive the full sample. Scored
  against price_history directly (n=387), BULLISH averages +0.43% at 53% hit
  and the desk is the best-performing research agent at 56.5%. What does hold
  at full power: BEARISH calls are still inverted (+0.75% average move, 42%
  hit), and NEUTRAL is 170 of 387 calls on stocks that then moved +3.74% on
  average, with only 13% actually staying flat — which is the horizon mismatch
  this phase addresses, not a weak analyst.
- Confidence sat at 76-84 regardless. Fetching more data did NOT explain the
  gap: zero-tool runs scored slightly BETTER, so the fix is not "call more
  tools".
- nothing in V3 carried a horizon (`grep horizon` returned nothing), so a
  multi-quarter business view was consumed as a vote on a trade that resolves
  in 7 days. near_term_read is the horizon-matched signal.
- the two qualitative pillars carried no number at all in 70% (moat) and 38%
  (management) of reports, against a prompt demanding "number + source".
"""

import pytest

from app.v3.artifact_validators import validate_artifact as coerce
from app.v3.artifacts import validate_artifact as schema_check


def _report(**fields):
    # positioning_read joined the required set 2026-07-29 (artifacts.py) so the
    # alt-data stance is enforced by the schema rather than by prompt text alone.
    # A valid minimal report therefore carries it.
    base = {
        "summary": "s", "pillars": {}, "thesis_direction": "BULLISH", "confidence": 70,
        "positioning_read": {"stance": "NEUTRAL"},
    }
    base.update(fields)
    return base


class TestHorizon:
    @pytest.mark.parametrize("raw,expected", [
        ("quarters", "QUARTERS"), ("YEARS", "YEARS"), ("Weeks", "WEEKS"),
    ])
    def test_horizon_is_normalized(self, raw, expected):
        assert coerce("fundamental_report", _report(horizon=raw))["horizon"] == expected

    def test_invalid_horizon_is_dropped(self):
        out = coerce("fundamental_report", _report(horizon="DECADES"))
        assert "horizon" not in out

    def test_absent_horizon_is_not_invented(self):
        assert "horizon" not in coerce("fundamental_report", _report())


class TestNearTermRead:
    def test_direction_and_flag_are_normalized(self):
        out = coerce("fundamental_report", _report(
            near_term_read={"direction": "bearish", "matters_this_week": "no", "why": "no trigger"}))
        read = out["near_term_read"]
        assert read["direction"] == "BEARISH"
        assert read["matters_this_week"] is False
        assert read["why"] == "no trigger"

    def test_echoed_literal_drops_the_read(self):
        out = coerce("fundamental_report", _report(
            near_term_read={"direction": "BULLISH|BEARISH|NEUTRAL", "matters_this_week": True}))
        assert "near_term_read" not in out

    def test_read_without_direction_is_dropped(self):
        out = coerce("fundamental_report", _report(near_term_read={"matters_this_week": False}))
        assert "near_term_read" not in out

    def test_non_dict_read_is_dropped(self):
        out = coerce("fundamental_report", _report(near_term_read="not this week"))
        assert "near_term_read" not in out

    def test_thesis_direction_is_untouched(self):
        """The business view stays exactly as written — the near-term read is
        an addition, not a replacement."""
        out = coerce("fundamental_report", _report(
            thesis_direction="BULLISH", horizon="YEARS",
            near_term_read={"direction": "NEUTRAL", "matters_this_week": False}))
        assert out["thesis_direction"] == "BULLISH"
        assert out["near_term_read"]["direction"] == "NEUTRAL"

    def test_schema_accepts_the_new_fields(self):
        assert schema_check("fundamental_report", _report(
            horizon="QUARTERS", near_term_read={"direction": "NEUTRAL"})) == []


class TestPositioningReadIsRequired:
    """positioning_read must be enforced by the SCHEMA, not by prompt text.

    Measured 2026-07-28: the alt-data block was injected into six agents and
    cited by zero. Prompt-level "REQUIRED" lost to a 7,962-char desk view; the
    fix that moved consumption 0/5 -> 5/5 was a required schema field. It was
    shipped in the prompt and the field description but never added to the
    schema's `required` list, so validate_artifact() could not report it
    missing. This pins the enforcement.
    """

    def test_missing_positioning_read_is_reported(self):
        report = _report()
        del report["positioning_read"]
        assert "Missing required field: positioning_read" in schema_check(
            "fundamental_report", report)

    def test_null_positioning_read_is_reported(self):
        assert "Required field is None: positioning_read" in schema_check(
            "fundamental_report", _report(positioning_read=None))

    def test_present_positioning_read_passes(self):
        assert schema_check("fundamental_report", _report()) == []


class TestHorizonMatchedScoring:
    """The scorecard must grade the 7-day claim, not the multi-year one."""

    def _stance(self, artifact):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sc", "scripts/agent_scorecard.py")
        sc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sc)
        return sc._stance(artifact)

    def test_near_term_read_outranks_thesis_direction(self):
        assert self._stance({"thesis_direction": "BULLISH",
                             "near_term_read": {"direction": "NEUTRAL"}}) == 0

    def test_falls_back_for_pre_audit_artifacts(self):
        """Historical reports have no near_term_read; they must still score so
        the baseline stays comparable."""
        assert self._stance({"thesis_direction": "BULLISH"}) == 1

    def test_other_agents_are_unaffected(self):
        assert self._stance({"thesis_direction": "BEARISH"}) == -1
        assert self._stance({"action": "SELL"}) == -1


class TestPromptContract:
    def test_prompt_stops_mandating_a_redundant_whiteboard_read(self):
        from app.v3.agents.fundamental_analyst import SYSTEM_PROMPT

        assert "Do NOT spend a turn on `whiteboard_read`" in SYSTEM_PROMPT

    def test_prompt_gives_quantitative_proxies_for_the_soft_pillars(self):
        from app.v3.agents.fundamental_analyst import SYSTEM_PROMPT

        assert "gross margin" in SYSTEM_PROMPT
        assert "ROIC" in SYSTEM_PROMPT

    def test_prompt_documents_horizon_separation(self):
        from app.v3.agents.fundamental_analyst import SYSTEM_PROMPT

        assert "near_term_read" in SYSTEM_PROMPT
        assert "matters_this_week" in SYSTEM_PROMPT


class TestThesisDirectionNormalization:
    """5 live artifacts emitted the schema placeholder verbatim as their
    direction; every downstream consumer read it as an unmatched string."""

    def test_echoed_schema_literal_becomes_neutral(self):
        out = coerce("fundamental_report", _report(thesis_direction="BULLISH|BEARISH|NEUTRAL"))
        assert out["thesis_direction"] == "NEUTRAL"
        assert any("thesis_direction" in n for n in out.get("_validator_notes", []))

    @pytest.mark.parametrize("raw", ["bullish", "Bearish", "NEUTRAL"])
    def test_case_is_normalized(self, raw):
        out = coerce("fundamental_report", _report(thesis_direction=raw))
        assert out["thesis_direction"] == raw.upper()

    def test_valid_direction_is_untouched(self):
        out = coerce("fundamental_report", _report(thesis_direction="BEARISH"))
        assert out["thesis_direction"] == "BEARISH"
