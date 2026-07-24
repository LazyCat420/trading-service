"""Tests for the 2026-07-24 Regime Engine grounding wave.

Covers the three things that audit changed:

- the regime is classified ONCE per cycle and shared by every ticker
  (it used to run per ticker, and 35 of 64 multi-ticker cycles disagreed
  with themselves about the same global market),
- the computed macro-trend block turns levels into the slopes/breadth the
  factor scores are supposed to read,
- forward_call — the engine's only falsifiable output — is normalized so the
  grader can match on exact enums.
"""

import asyncio

import pytest

from app.v3 import regime_cache
from app.v3.artifact_validators import validate_artifact


@pytest.fixture(autouse=True)
def _clean_cache():
    regime_cache.clear()
    yield
    regime_cache.clear()


class TestRegimeCache:
    def test_concurrent_tickers_classify_the_regime_once(self):
        """Six tickers racing on one cycle must produce ONE classification.

        This is the whole point: the engine answers a question about the
        market, not about the ticker.
        """
        computed_by = []

        async def ticker(name: str) -> str:
            async with regime_cache.get_lock("cycle-1"):
                cached = regime_cache.get("cycle-1")
                if cached is not None:
                    return f"reused:{cached['regime']}"
                computed_by.append(name)
                await asyncio.sleep(0.01)  # stand-in for the LLM call
                regime_cache.put("cycle-1", {"regime": "DEEP_DISCOUNT"})
                return "computed"

        async def race():
            return await asyncio.gather(*[ticker(f"T{i}") for i in range(6)])

        results = asyncio.run(race())

        assert len(computed_by) == 1
        assert results.count("computed") == 1
        assert results.count("reused:DEEP_DISCOUNT") == 5

    def test_cycles_do_not_share_a_regime(self):
        regime_cache.put("cycle-a", {"regime": "HIGH_VOLATILITY"})
        assert regime_cache.get("cycle-b") is None
        assert regime_cache.get("cycle-a")["regime"] == "HIGH_VOLATILITY"

    def test_caller_cannot_mutate_the_cached_artifact(self):
        """Each ticker appends the regime to its own desk; if that handed out
        the cached dict itself, one desk's downstream edits would rewrite the
        regime every other ticker sees."""
        regime_cache.put("cycle-1", {"regime": "DEEP_DISCOUNT", "factors": {}})

        borrowed = regime_cache.get("cycle-1")
        borrowed["regime"] = "TAMPERED"

        assert regime_cache.get("cycle-1")["regime"] == "DEEP_DISCOUNT"

    def test_old_cycles_are_evicted(self):
        for i in range(12):
            regime_cache.put(f"cycle-{i}", {"regime": "CONTRADICTORY"})

        assert regime_cache.get("cycle-0") is None
        assert regime_cache.get("cycle-11") is not None
        assert len(regime_cache._CACHE) <= regime_cache._MAX_CYCLES

    def test_empty_and_malformed_entries_are_ignored(self):
        regime_cache.put("cycle-1", {})
        regime_cache.put("cycle-2", None)
        regime_cache.put("", {"regime": "DEEP_DISCOUNT"})

        assert regime_cache.get("cycle-1") is None
        assert regime_cache.get("cycle-2") is None


class TestForwardCallNormalization:
    def _call(self, **fields):
        art = validate_artifact(
            "regime_classification",
            {"regime": "DEEP_DISCOUNT", "forward_call": dict(fields)},
        )
        return art.get("forward_call")

    def test_lowercase_directions_are_normalized(self):
        call = self._call(spx_direction="up", vol_direction="stable", conviction=65)
        assert call["spx_direction"] == "UP"
        assert call["vol_direction"] == "STABLE"
        assert call["conviction"] == 65.0

    def test_echoed_schema_literal_is_dropped(self):
        """Models sometimes emit the schema placeholder verbatim; an
        un-normalized 'UP|DOWN|FLAT' would score as a miss forever."""
        call = self._call(spx_direction="UP|DOWN|FLAT", vol_direction="RISING")
        assert "spx_direction" not in call
        assert call["vol_direction"] == "RISING"

    def test_fully_invalid_call_is_removed(self):
        art = validate_artifact(
            "regime_classification",
            {"regime": "DEEP_DISCOUNT",
             "forward_call": {"spx_direction": "sideways-ish", "vol_direction": "?"}},
        )
        assert "forward_call" not in art

    def test_non_dict_call_is_removed(self):
        art = validate_artifact(
            "regime_classification",
            {"regime": "DEEP_DISCOUNT", "forward_call": "SPX will go up"},
        )
        assert "forward_call" not in art

    def test_conviction_is_clamped(self):
        assert self._call(spx_direction="DOWN", conviction=150)["conviction"] == 100.0
        assert self._call(spx_direction="DOWN", conviction=-20)["conviction"] == 0.0
        assert "conviction" not in self._call(spx_direction="DOWN", conviction="high")

    def test_missing_forward_call_is_not_invented(self):
        art = validate_artifact("regime_classification", {"regime": "DEEP_DISCOUNT"})
        assert "forward_call" not in art


class TestMacroTrendBlock:
    def test_nan_closes_never_reach_a_derived_number(self):
        """asset_prices carries NaN for symbols a vendor returned empty — the
        market_regime table is full of them. NaN compares false everywhere and
        would silently poison every percentage."""
        from app.v3.macro_trend import _finite, _pct_change, _zscore

        assert _finite(float("nan")) is None
        assert _finite(float("inf")) is None
        assert _finite("not a number") is None
        assert _finite(18.74) == 18.74

        assert _pct_change([100.0, 101.0], 5) is None       # not enough history
        assert _pct_change([0.0, 1.0, 2.0, 3.0, 4.0, 5.0], 5) is None  # zero base
        assert _zscore([5.0] * 30) is None                   # zero variance

    def test_pct_change_and_sma_distance_are_correct(self):
        from app.v3.macro_trend import _pct_change, _sma_distance

        closes = [100.0, 100.0, 100.0, 100.0, 100.0, 110.0]
        assert _pct_change(closes, 5) == pytest.approx(10.0)

        # latest 110 against a 6-day SMA of 101.666...
        assert _sma_distance(closes, 6) == pytest.approx(8.196, abs=0.01)
        assert _sma_distance(closes, 50) is None  # not enough history

    def test_block_is_empty_when_nothing_can_be_computed(self, monkeypatch):
        """A grounding failure must degrade the regime call, never abort the
        cycle."""
        import app.v3.macro_trend as mt

        monkeypatch.setattr(mt, "_load_series", lambda *a, **k: {})
        assert mt.build_macro_trend_block() == ""
        assert mt.build_macro_trend_lines() == []

    def test_db_failure_is_swallowed(self, monkeypatch):
        import app.v3.macro_trend as mt

        def boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(mt, "_load_series", boom)
        assert mt.build_macro_trend_lines() == []
