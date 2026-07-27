"""Data-provenance controls added 2026-07-27.

Four changes, each aimed at a distinct failure in cycle-v3-1785107795:

1. HOLD_NO_PRICE_DATA — ASIC and ARCVF had ZERO price_history rows, ran the
   full panel, and ASIC reached BUY at 68 confidence. Categorical, so a gate.
2. Graded per-indicator freshness — one `stale` bool served consumers with
   tolerances from hours (entry price) to months (SMA-200).
3. Absence is louder than staleness — a missing baseline returned "" and
   warned nobody.
4. Trading-day age, per market — a Monday "3 days old" is a weekend; a
   Wednesday "3 days old" is an outage.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from app.quant import technical_baseline as tb


class TestGradedFreshness:
    def test_trend_tolerates_what_momentum_does_not(self):
        """The whole point of grading: 5 trading days is fine for SMA-200 and
        not fine for a stochastic. One boolean could not say both."""
        trend_mult, trend_label = tb._freshness(5, "trend")
        mom_mult, mom_label = tb._freshness(5, "momentum")

        assert trend_mult == 1.0 and trend_label == "current"
        assert mom_mult < 1.0
        assert "current" not in mom_label

    def test_price_horizon_is_the_tightest(self):
        """A close used for sizing goes stale fastest."""
        assert tb._freshness(2, "price")[0] < 1.0
        assert tb._freshness(2, "band")[0] == 1.0

    def test_decay_is_graded_not_a_cliff(self):
        fresh = tb._freshness(3, "momentum")[0]
        mid = tb._freshness(6, "momentum")[0]
        far = tb._freshness(10, "momentum")[0]
        assert fresh == 1.0
        assert 0.3 < mid < 1.0, "mid-range should decay, not snap"
        assert far == 0.3

    def test_floor_is_never_zero(self):
        """A very old indicator is still a better anchor than an invented
        number — the multiplier floors at 0.3 rather than discarding."""
        assert tb._freshness(9999, "momentum")[0] == 0.3

    def test_unknown_age_is_treated_as_suspect(self):
        mult, label = tb._freshness(None, "trend")
        assert mult == 0.3
        assert "unknown" in label

    def test_horizons_are_ordered_price_tightest_trend_loosest(self):
        assert tb._HORIZONS["price"][0] < tb._HORIZONS["momentum"][0]
        assert tb._HORIZONS["momentum"][0] < tb._HORIZONS["trend"][0]


class TestAbsenceIsLoud:
    def test_missing_baseline_emits_an_explicit_block(self):
        """The ASIC case. The old code returned "" here, so the ticker the
        agent knew LEAST about produced the LEAST warning."""
        with patch.object(tb, "compute_technical_baseline", return_value={}):
            block = tb.build_technical_baseline_block("ASIC")

        assert block != ""
        assert "NONE ON FILE" in block
        assert "data_gaps" in block

    def test_present_baseline_labels_each_line(self):
        fake = {
            "as_of": date(2026, 7, 24), "age_days": 2, "age_trading_days": 1,
            "close": 202.84, "rsi": 52.1, "sma_200": 206.19,
            "sma_200_status": "BELOW", "stale": False,
        }
        with patch.object(tb, "compute_technical_baseline", return_value=fake):
            block = tb.build_technical_baseline_block("COF")

        assert "[price:" in block and "[momentum:" in block and "[trend:" in block
        # The header must not assert blanket authority any more — that wording
        # is what told the board a Friday close was current on a Sunday.
        assert "these are the authoritative values" not in block


class TestNoPriceDataGate:
    def test_gate_blocks_a_trade_that_would_otherwise_execute(self):
        """A decision that clears every other gate is still blocked when the
        ticker has no price history at all — the ASIC case.

        Confidence is 80 (above the floor) and full risk mitigation is present
        precisely so nothing ELSE can block: this must be the data gate.
        """
        from app.v3.shared_desk import SharedDesk
        from app.v3.orchestrator import _apply_policy_gates

        desk = SharedDesk(ticker="ASIC", cycle_id="cycle-test")
        desk.regime_classification = {"summary": "regime ok"}
        desk.final_decision = {
            "action": "BUY", "confidence": 80, "stop_loss": 10.0,
            "dynamic_trigger": {"type": "trailing_drop", "value": 0.1},
            "position_size_pct": 2.0,
        }

        import app.quant.technical_baseline as _tb
        with patch.object(_tb, "has_price_history", return_value=False), \
             patch("app.v3.orchestrator._record_gate",
                   side_effect=lambda d, label, **k: label):
            assert _apply_policy_gates(desk) == "HOLD_NO_PRICE_DATA"

    def test_gate_does_not_mask_a_more_specific_verdict(self):
        """Ordered LAST on purpose. A low-confidence BUY on a ticker with no
        data must still report the CONFIDENCE reason — placed early, this gate
        relabelled every specific block and lost the real diagnosis."""
        from app.v3.shared_desk import SharedDesk
        from app.v3.orchestrator import _apply_policy_gates

        desk = SharedDesk(ticker="ASIC", cycle_id="cycle-test")
        desk.regime_classification = {"summary": "regime ok"}
        desk.final_decision = {"action": "BUY", "confidence": 40}

        import app.quant.technical_baseline as _tb
        with patch.object(_tb, "has_price_history", return_value=False), \
             patch("app.v3.orchestrator._record_gate",
                   side_effect=lambda d, label, **k: label):
            assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE"

    def test_hold_is_not_gated_by_missing_data(self):
        """A HOLD short-circuits before the probe — refusing to act on a
        ticker we know nothing about is already the safe direction."""
        from app.v3.orchestrator import _apply_policy_gates

        desk = MagicMock()
        desk.ticker = "ASIC"
        desk.trade_decision = {"action": "HOLD", "confidence": 50}
        desk.final_decision = {}
        desk.cycle_metadata = {}

        with patch("app.quant.technical_baseline.has_price_history",
                   return_value=False):
            assert _apply_policy_gates(desk) == "HOLD_NO_SIGNAL"

    def test_probe_failure_fails_open(self):
        """This gate catches a missing table, not a Postgres hiccup — a DB
        error must not halt trading."""
        from app.v3.orchestrator import _apply_policy_gates

        desk = MagicMock()
        desk.ticker = "COF"
        desk.trade_decision = {"action": "BUY", "confidence": 74}
        desk.final_decision = {}
        desk.cycle_metadata = {"held": True}
        desk.has_artifact.return_value = True
        desk.tournament_result = {}

        with patch("app.quant.technical_baseline.has_price_history",
                   side_effect=RuntimeError("db down")), \
             patch("app.v3.orchestrator._record_gate",
                   side_effect=lambda d, label, **k: label):
            # Must NOT return HOLD_NO_PRICE_DATA — it fell through.
            assert _apply_policy_gates(desk) != "HOLD_NO_PRICE_DATA"


class TestStaleFillWarning:
    def test_warn_threshold_is_below_the_refusal_threshold(self):
        """The refusal boundary must stay generous enough to clear a weekend
        (Fri close -> Mon open is ~65h); the warning is what catches a fill
        priced off a cached bar inside that window."""
        from app.trading import paper_trader as pt

        assert pt.STALE_FILL_WARN_HOURS < pt.MAX_PRICE_AGE_HOURS
        assert pt.MAX_PRICE_AGE_HOURS >= 72, "must clear a normal weekend"

    def test_the_cof_case_warns_but_does_not_refuse(self):
        """COF filled off a ~48h-old close: inside the 96h limit, so it must
        still trade, but it must no longer do so silently."""
        from app.trading import paper_trader as pt

        age = 48.0
        assert age < pt.MAX_PRICE_AGE_HOURS
        assert age > pt.STALE_FILL_WARN_HOURS


def test_every_horizon_class_is_reachable_from_a_field():
    """A horizon nothing maps to is dead config.

    Read inside the test body, not at import: a module-level reference to
    tb._HORIZONS turns a missing attribute into a COLLECTION error, which
    aborts the whole file and makes the negative control unreadable.
    """
    mapped = set(tb._FIELD_CLASS.values())
    for cls in tb._HORIZONS:
        assert cls in mapped, f"horizon '{cls}' is not reachable from any field"
