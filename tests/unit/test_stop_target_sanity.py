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
    """The check lives in `_drop_implausible_levels`, not in the policy gate.

    It moved out on 2026-08-03. Inside the gate chain it ran at whatever point
    that chain happened to be called, and the two callers disagreed: the full
    panel gated BEFORE building the result (drop reached the executor) while the
    delta re-look gated AFTER (the bad level survived in
    result["estimate"]["stop_loss"] and went to buy() as a live stop order).
    Both also persisted the desk and the trade row before the gate ran, so
    telemetry recorded a drop the database never saw.
    """

    def test_it_drops_rather_than_clamps(self):
        """A clamped level is a number the desk never chose, presented as
        though it had — the Board's exit logic would then act on our
        arithmetic instead of its thesis."""
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch._drop_implausible_levels)
        assert "DROPPED_IMPLAUSIBLE_LEVEL" in src
        assert "decision[_field] = None" in src

    def test_the_price_lookup_cannot_block_a_cycle(self):
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch._drop_implausible_levels)
        assert "a price lookup must never block" in src

    def test_the_band_in_code_matches_the_contract_here(self):
        """If someone widens the band, this fails rather than silently
        describing a band that no longer exists."""
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch._drop_implausible_levels)
        for field, (lo, hi) in BANDS.items():
            assert f'"{field}": ({lo}, {hi})' in src

    def test_the_policy_gate_no_longer_touches_levels(self):
        """Pinning the split. A sanitizer that mutates the artifact must not
        sit in a chain whose call position varies by triage tier."""
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch._apply_policy_gates)
        assert "DROPPED_IMPLAUSIBLE_LEVEL" not in src

    def test_it_runs_before_the_trade_row_is_written(self):
        """`trade_results` used to store levels the sanitizer had rejected,
        because the write happened first."""
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch._persist_trade_verdict)
        drop_at = src.index("_drop_implausible_levels(desk)")
        save_at = src.index("save_trade_result(ticker, cycle_id, trade_decision)")
        assert drop_at < save_at

    def test_the_delta_tier_sanitizes_before_it_builds_its_result(self):
        """This is the regression. The delta path used to call the gates AFTER
        `_build_v1_compatible_result`, so the dropped level survived in
        result["estimate"]["stop_loss"] — which pipeline_service hands straight
        to buy() as a live stop order."""
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch.run_v3_pipeline)
        region = src[:src.index('result["triage_tier"] = "v3_delta"')]
        persist_at = region.rindex("_persist_trade_verdict(")
        build_at = region.rindex("_build_v1_compatible_result(desk")
        assert persist_at < build_at, (
            "the delta tier builds its result before sanitizing/persisting"
        )

    def test_the_full_panel_sanitizes_before_it_builds_its_result(self):
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch.run_v3_pipeline)
        region = src[src.index("LAYER 6: Policy Gates"):]
        drop_at = region.index("_drop_implausible_levels(desk)")
        build_at = region.index("_build_v1_compatible_result(desk")
        assert drop_at < build_at

    def test_the_glance_tier_is_exempt_and_that_is_correct(self):
        """A Triage-Gate glance writes a hardcoded HOLD@0 before any agent runs.
        It carries no stop_loss/take_profit to sanitize and can never trade, so
        the absence of a sanitizer call on that path is by design, not an
        oversight for someone to 'fix' later."""
        from app.v3 import orchestrator as orch

        src = inspect.getsource(orch.run_v3_pipeline)
        glance = src[src.index('triage_tier == "v3_glance"'):]
        glance = glance[:glance.index("return result")]
        assert "stop_loss" not in glance and "take_profit" not in glance
