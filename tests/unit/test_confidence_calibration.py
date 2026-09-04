"""Confidence must be ordered, and must mean what it says.

Measured 2026-07-31 over resolved directional calls, 5-point buckets n>=20:
60->51.5%, 65->43.3%, 70->61.8%, 75->63.2%, 80->66.7%, 85->67.4%, 90->68.3%,
95->45.5%. Mostly ordered, overstated by ~15.8 points on average, and
inverted at the top.

The old discrimination term compared win_rate(conf>=70) against
win_rate(conf<50). On that data it scores ~1.0 — full marks — because the
>=70 aggregate does beat the <50 aggregate. It cannot see the 95 collapse,
and it ignores 50-69 entirely. That is a two-value gate wearing a
distribution's name.
"""

import pytest

from app.autoresearch.auditors.decision_audit import (
    SCORE_VERSION,
    _bucket_rank_tau,
    _confidence_axis,
    _rank_discrimination,
)
from app.autoresearch.confidence_calibration import _pava, calibrated_confidence


def _bucket(win_rate: float, n: int = 50, conf: int = 70):
    wins = int(round(win_rate * n))
    return [(None, conf, 5.0, "WIN")] * wins + [(None, conf, -5.0, "LOSS")] * (n - wins)


def _wr(rows):
    return len([r for r in rows if r[3] == "WIN"]) / len(rows)


# ── discrimination ──────────────────────────────────────────────────────────

def test_a_top_bucket_collapse_is_penalised():
    """The live shape. The two-bucket term gave this full credit."""
    q = {70: _bucket(0.618), 75: _bucket(0.632), 85: _bucket(0.674),
         90: _bucket(0.683), 95: _bucket(0.455)}
    score, tau = _rank_discrimination(q, _wr)
    assert tau < 1.0, "an inversion at the top must not score as perfect ordering"
    assert score < 1.0
    # Still mostly ordered, so it should not read as fully inverted either.
    assert score > 0.5


def test_perfect_ordering_scores_one():
    q = {60: _bucket(0.40), 70: _bucket(0.55), 80: _bucket(0.65), 90: _bucket(0.75)}
    score, tau = _rank_discrimination(q, _wr)
    assert tau == 1.0 and score == 1.0


def test_perfect_inversion_scores_zero():
    q = {60: _bucket(0.75), 70: _bucket(0.65), 80: _bucket(0.55), 90: _bucket(0.40)}
    score, tau = _rank_discrimination(q, _wr)
    assert tau == -1.0 and score == 0.0


def test_no_ordering_is_neutral_half():
    q = {60: _bucket(0.60), 70: _bucket(0.60), 80: _bucket(0.60)}
    score, _ = _rank_discrimination(q, _wr)
    assert score == 0.5


def test_one_bucket_cannot_produce_a_relationship():
    """Returning a number from a single point would invent one."""
    score, tau = _rank_discrimination({70: _bucket(0.9)}, _wr)
    assert tau is None and score == 0.5


def test_the_middle_of_the_scale_is_no_longer_ignored():
    """50-69 was invisible to the old term. An inversion there must count."""
    q = {50: _bucket(0.70), 60: _bucket(0.50), 70: _bucket(0.72), 90: _bucket(0.75)}
    _, tau = _rank_discrimination(q, _wr)
    assert tau < 1.0


# ── which axis ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("win,mag,expected", [
    (0.9, 0.9, "both"),
    (0.0, 0.9, "magnitude"),
    (0.9, 0.0, "frequency"),
    (0.0, 0.0, "neither"),
    (None, None, "neither"),
])
def test_axis_naming(win, mag, expected):
    assert _confidence_axis(win, mag) == expected


# ── isotonic calibration ────────────────────────────────────────────────────

def test_pava_leaves_an_ordered_sequence_alone():
    pts = [(70.0, 60.0, 100), (80.0, 65.0, 100), (90.0, 70.0, 100)]
    assert [round(y, 1) for _, y, _ in _pava(pts)] == [60.0, 65.0, 70.0]


def test_pava_pools_an_inversion_instead_of_deleting_it():
    """The 95 bucket must not vanish — it merges, and the merged group
    reports the combined rate, which is the honest 'these are not
    distinguishable' statement."""
    pts = [(90.0, 70.0, 100), (95.0, 45.0, 100)]
    out = _pava(pts)
    ys = [round(y, 1) for _, y, _ in out]
    assert len(out) == 2, "both buckets are still reported"
    assert ys[0] == ys[1] == 57.5, "pooled to the weighted mean"


def test_pava_weights_by_sample_size():
    # A 10-row inversion should barely move a 1000-row neighbour.
    pts = [(90.0, 70.0, 1000), (95.0, 0.0, 10)]
    pooled = round(_pava(pts)[0][1], 1)
    assert 69.0 < pooled < 70.0


def test_pava_output_is_monotone_for_a_messy_input():
    pts = [(60.0, 51.5, 33), (65.0, 43.3, 150), (70.0, 61.8, 309),
           (75.0, 63.2, 484), (80.0, 66.7, 30), (85.0, 67.4, 307),
           (90.0, 68.3, 63), (95.0, 45.5, 44)]
    ys = [y for _, y, _ in _pava(pts)]
    assert all(b >= a - 1e-9 for a, b in zip(ys, ys[1:])), ys


def test_calibrated_confidence_refuses_to_extrapolate():
    cmap = {"buckets": [
        {"bucket": 70, "n": 100, "stated": 70, "realized": 62, "calibrated": 62, "overstatement": 8},
        {"bucket": 90, "n": 100, "stated": 90, "realized": 68, "calibrated": 68, "overstatement": 22},
    ]}
    assert calibrated_confidence(70, cmap) == 62
    assert calibrated_confidence(20, cmap) is None, "below the evidenced range"
    assert calibrated_confidence(99, cmap) is None, "above the evidenced range"


def test_no_map_yields_no_claim():
    assert calibrated_confidence(80, {"buckets": []}) is None


def test_score_version_was_bumped():
    """The calibration term changed, so cycles either side are not
    comparable on it. An unbumped version makes that jump look like an
    improvement — the exact failure the stamp exists to prevent.

    Asserted as a FLOOR, not an equality. `== "v5"` caught the revert it was
    written for, but it also failed the next legitimate bump (v6, 2026-09-04,
    the per-cycle judge term) — a guard that goes red for being superseded
    trains people to edit it without reading it. The floor still fails if the
    calibration change is reverted, and survives honest formula changes, each
    of which carries its own entry in the SCORE_VERSION changelog.
    """
    assert SCORE_VERSION.startswith("v")
    assert int(SCORE_VERSION[1:]) >= 5, (
        f"SCORE_VERSION is {SCORE_VERSION}: the calibration change that made "
        "cycles non-comparable is not stamped"
    )
