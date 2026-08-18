"""`compute_calibrated_confidence` — the blend, and the inputs it must refuse.

The first version of this file asserted bounds with
``(baseline=120, board=150) == 100.0`` and ``(-20, -10) == 0.0``. Both passed
WITHOUT ever exercising the clamp: an out-of-range board confidence took the
early-return branch, so the assertions were really testing the fallback while
reading as a test of the blend. Every case here states which branch it is on.
"""
import math

from app.quant.decision_score import (
    _BASELINE_WEIGHT,
    _BOARD_WEIGHT,
    compute_calibrated_confidence as calib,
)


def test_weights_sum_to_one():
    """A blend whose weights do not sum to 1 silently rescales the output."""
    assert _BASELINE_WEIGHT + _BOARD_WEIGHT == 1.0


def test_blend_is_the_stated_weighted_mean():
    # 0.55*80 + 0.45*60 = 44 + 27 = 71.0
    assert calib(80, 60) == 71.0
    # 0.55*40 + 0.45*90 = 22 + 40.5 = 62.5
    assert calib(40, 90) == 62.5


def test_board_absent_falls_back_to_baseline():
    assert calib(65) == 65.0
    assert calib(65, None) == 65.0


def test_blend_lies_between_its_inputs():
    """A weighted mean of two in-range values can never leave the range, so the
    clamp is unreachable from valid input — which is why the clamp cannot be
    tested through this function and the old bounds test was vacuous."""
    for base, board in ((1, 100), (100, 1), (84, 55), (28, 74)):
        assert min(base, board) <= calib(base, board) <= max(base, board)


def test_zero_baseline_is_the_unscoreable_sentinel_and_returns_none():
    """confidence=0 is what every degraded path writes; real scores start at 15.

    Blending it produced 0.55*0 + 0.45*70 = 31.5 — a crashed pipeline that
    reads downstream as a cautious low-confidence decision.
    """
    assert calib(0, 70) is None
    assert calib(0) is None
    assert calib(0.0, 55) is None


def test_negative_baseline_returns_none():
    assert calib(-20, 60) is None


def test_out_of_range_board_returns_none_rather_than_a_different_quantity():
    """The old form fell back to the bare baseline here, returning a number
    that was NOT a blend under a name that promises one."""
    assert calib(80, 150) is None
    assert calib(80, -10) is None


def test_non_finite_inputs_return_none():
    assert calib(float("nan"), 60) is None
    assert calib(float("inf"), 60) is None
    assert calib(80, float("nan")) is None


def test_unparseable_inputs_return_none():
    assert calib(None) is None
    assert calib("high", 60) is None
    assert calib(80, "very") is None


def test_blend_widens_the_boards_compressed_scale():
    """The whole point of the blend, asserted as a property.

    The Board's stated confidence occupies a ~19-point window (measured 55-74,
    sd 4.65) while the baseline ranges 0-84 (sd 14.78). Mixing in the baseline
    must INCREASE dispersion versus the board alone, or the function is not
    doing the job it was added for.
    """
    boards = [55, 58, 61, 64, 67, 70, 74]
    baselines = [28, 45, 84, 60, 33, 78, 52]
    blended = [calib(b, d) for b, d in zip(baselines, boards)]
    assert all(x is not None for x in blended)

    def sd(xs):
        m = sum(xs) / len(xs)
        return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))

    assert sd(blended) > sd(boards)
