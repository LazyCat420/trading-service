"""The challenger scoreboard must measure the quantity the experiment treats.

exp-2026-07-confidence-spread instructed the synthesizer to spread confidence
across the full 0-100 scale. Its primary metric was a sign test over ACTIONS,
and `agree` is written as pure action equality (app/v3/challenger.py), so the
scoreboard threw away every pair where the two sides picked the same action —
469 of 522, measured 2026-09-04, of which 269 carried a confidence gap wider
than the panel's own +/-3 pt noise band. The metric could not see the
treatment.

The spec's pre-registered secondary signal (challenger stdev >= 2x champion's
over >= 30 pairs) existed only as prose in experiments/*.md and was never
computed, so for 47 days nothing could report that the treatment had failed.

`confidence_effect` closes both gaps. Reproduced against the live 522 pairs:
spread ratio 1.00, mean shift -2.98 pts, 269 pairs beyond the noise band.
"""

import json
from pathlib import Path

import pytest

from app.routers.challenger_router import confidence_effect, regressing_sectors


def test_it_reports_the_spread_ratio_the_spec_asked_for():
    """Champion tight, challenger wide — the outcome the experiment wanted."""
    pairs = [(65.0, 40.0), (65.0, 90.0), (64.0, 45.0), (66.0, 85.0)]
    out = confidence_effect(pairs)
    assert out["spread_ratio"] > 2.0
    assert out["spread_ratio_target"] == 2.0


def test_the_target_needs_thirty_pairs_not_just_a_ratio():
    """A wide ratio on 4 pairs must not read as the signal being met."""
    pairs = [(65.0, 40.0), (65.0, 90.0), (64.0, 45.0), (66.0, 85.0)]
    assert confidence_effect(pairs)["spread_target_met"] is False


def test_a_no_op_treatment_reports_a_ratio_of_one():
    pairs = [(60.0, 60.0), (70.0, 70.0), (55.0, 55.0), (65.0, 65.0)]
    out = confidence_effect(pairs)
    assert out["spread_ratio"] == 1.0
    assert out["mean_shift"] == 0.0
    assert out["pairs_moved_beyond_noise_band"] == 0


def test_it_counts_the_pairs_the_action_metric_discards():
    """Same action both sides, real confidence gap — invisible to the sign test."""
    pairs = [(70.0, 50.0), (70.0, 69.0), (60.0, 40.0)]
    out = confidence_effect(pairs)
    assert out["pairs_moved_beyond_noise_band"] == 2
    assert out["noise_band_pts"] == 3


def test_too_few_pairs_returns_a_note_not_a_number():
    """No evidence must not be dressed up as a measurement."""
    assert "note" in confidence_effect([])
    assert "spread_ratio" not in confidence_effect([(60.0, 55.0)])


def test_a_sector_reports_both_denominators():
    """'champion 4-0 on 14 disagreements' read as 4 of 14; it was 4 of 4."""
    sectors = {
        "Information Technology": {
            "pairs": 141, "disagreements": 14, "informative": 4,
            "champion_wins": 4, "challenger_wins": 0,
        },
    }
    slot = sectors["Information Technology"]
    assert slot["informative"] == slot["champion_wins"] + slot["challenger_wins"]
    assert slot["disagreements"] > slot["informative"]
    assert regressing_sectors(sectors) == ["Information Technology"]


def test_the_shipped_spec_is_stopped_with_a_verdict():
    """The experiment's own reject rule was met; the spec must record it.

    e-value 76,884 with leader = champion is the pre-registered REJECT
    condition, not a promotion signal. Leaving `enabled: true` kept paying for
    a full extra decision-agent call per ticker per cycle.
    """
    spec = json.loads(
        (Path(__file__).resolve().parents[2] / "experiments" / "active_spec.json").read_text()
    )
    assert spec["enabled"] is False
    assert spec["verdict"] == "rejected"
    assert spec["stopped_at"]


def test_a_disabled_spec_stops_the_challenger_running():
    from app.v3.challenger import get_challenger_spec
    assert get_challenger_spec() is None
