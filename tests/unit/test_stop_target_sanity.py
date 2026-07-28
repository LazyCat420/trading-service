"""Stop/target sanity: a decimal-error detector, not a strategy opinion.

Measured over 14 days: 3 of 358 decisions carried an implausible level,
including LMT with `stop_loss` $0.92 and `take_profit` $1.25 against a $581.33
close. Nothing checked any of it — `stop_loss`, `take_profit` and
`position_size_pct` are emitted by BOTH the Board and the synthesizer and were
the largest unguarded surface the 2026-07-28 fidelity audit found. They size
real orders: a $0.92 stop on a $581 stock either never triggers or liquidates
instantly, depending which side the code compares.
"""

import inspect

import pytest

# The band, mirrored from the gate. Stated here as data so the test declares
# the contract instead of re-reading the implementation and agreeing with
# itself; the last test pins the two together.
BANDS = {"stop_loss": (0.3, 1.5), "take_profit": (0.7, 3.0)}


def _implausible(field: str, value: float, close: float) -> bool:
    lo, hi = BANDS[field]
    return value <= 0 or not (close * lo <= value <= close * hi)


class TestTheBandCatchesRealDefects:
    def test_the_lmt_decimal_error(self):
        """The case that motivated the guard."""
        assert _implausible("stop_loss", 0.92, 581.33)
        assert _implausible("take_profit", 1.25, 581.33)

    def test_the_ally_case(self):
        assert _implausible("stop_loss", 11.04, 43.97)

    @pytest.mark.parametrize("value", [0.0, -5.0])
    def test_zero_and_negative_are_never_valid(self, value):
        assert _implausible("stop_loss", value, 100.0)


class TestTheBandDoesNotSecondGuessStrategy:
    """A guard that rejects legitimate trades is worse than none — it gets
    switched off, taking the decimal-error catch with it."""

    @pytest.mark.parametrize("stop", [95.0, 88.0, 70.0, 45.0, 140.0])
    def test_plausible_stops_pass(self, stop):
        assert not _implausible("stop_loss", stop, 100.0)

    @pytest.mark.parametrize("target", [105.0, 130.0, 200.0, 290.0, 75.0])
    def test_ambitious_but_real_targets_pass(self, target):
        assert not _implausible("take_profit", target, 100.0)

    def test_a_stop_above_the_price_is_allowed(self):
        """Legitimate on a SHORT thesis. The guard is about magnitude, not
        direction — direction is the desk's call."""
        assert not _implausible("stop_loss", 112.0, 100.0)


class TestTheGuardIsWiredAndFailsOpen:
    def test_it_drops_rather_than_clamps(self):
        """A clamped level is a number the desk never chose, presented as
        though it had — the Board's exit logic would then act on our
        arithmetic instead of its thesis."""
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch._apply_policy_gates)
        assert "DROPPED_IMPLAUSIBLE_LEVEL" in src
        assert "decision[_field] = None" in src

    def test_the_price_lookup_cannot_block_a_cycle(self):
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch._apply_policy_gates)
        assert "a price lookup must never block" in src

    def test_the_band_in_code_matches_the_contract_here(self):
        """If someone widens the band in the gate, this fails rather than
        silently describing a band that no longer exists."""
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch._apply_policy_gates)
        for field, (lo, hi) in BANDS.items():
            assert f'"{field}": ({lo}, {hi})' in src
