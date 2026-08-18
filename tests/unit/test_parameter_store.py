"""Parameter store — registry envelope sanity + resolver fallback semantics.

The store must be impossible to break the trading path with: an empty table,
an unreachable DB, or an out-of-envelope row must all resolve to safe values.

These used to stub `parameter_store.get_db` with a fake connection whose
`execute(...).fetchone()` returned the row. `get_db` no longer exists — the
resolver calls `mongo_query.find_row`, which returns the row as a TUPLE in the
requested column order — so the monkeypatch set an attribute nobody reads and
every case actually hit the live-DB guard. The fakes now stand in for
`find_row` directly, and additionally pin the collection and the freshness sort
the resolver depends on to pick the CURRENT row.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services import parameter_store as ps


def _fake_find_row(row, seen=None):
    """Stand in for mongo_query.find_row, returning `row` (a tuple or None).

    Also checks the resolver asks the store the question it claims to: the
    active row for THIS key, newest first.
    """
    def _find_row(collection, filt, columns, sort=None, **kwargs):
        assert collection == "runtime_parameters"
        assert filt["status"] == "active"
        assert columns == ["value"]
        # Newest row wins, otherwise a stale expired value could be served.
        assert sort == [("created_at", -1)]
        if seen is not None:
            seen.append(filt["param_key"])
        return row

    return _find_row


def setup_function(_fn):
    ps.invalidate_cache()


def test_registry_defaults_inside_their_own_bounds():
    for key, spec in ps.PARAMETER_REGISTRY.items():
        assert spec.min_value <= spec.default <= spec.max_value, key
        assert spec.direction in (ps.RISK_UP, ps.RISK_DOWN, ps.RISK_NEUTRAL), key
        assert spec.tier in (ps.TIER_STANDARD, ps.TIER_BOARD), key


def test_defaults_match_previous_hardcoded_values():
    # Parity guard: an empty store must reproduce pre-store behavior exactly.
    #
    # ONE deliberate exception: ANALYSIS_CONFIDENCE_THRESHOLD moved 65 -> 70 on
    # 2026-07-26 on measured evidence (BUYs below 70 underperform the always-long
    # null by -4.78%, n=130, NW t=-5.49, bootstrap p=0.000, stable across both
    # chronological halves). Parity with the old hardcoded value is no longer the
    # goal for this parameter; it is asserted at its fitted value in
    # tests/unit/test_calibration_and_integrity.py, which also carries the
    # derivation. Every OTHER default must still match.
    assert ps.PARAMETER_REGISTRY["MAX_POSITION_SIZE_PCT"].default == 0.10
    assert ps.PARAMETER_REGISTRY["MAX_CONCENTRATION_PCT"].default == 0.25
    assert ps.PARAMETER_REGISTRY["ANALYSIS_CONFIDENCE_THRESHOLD"].default == 70
    assert ps.PARAMETER_REGISTRY["DATA_QUALITY_FLOOR"].default == 40
    assert ps.PARAMETER_REGISTRY["MAX_PORTFOLIO_DRAWDOWN_PCT"].default == 0.25
    assert ps.PARAMETER_REGISTRY["ATR_STOP_MULTIPLIER"].default == 2.0
    assert ps.PARAMETER_REGISTRY["TAKE_PROFIT_RR_RATIO"].default == 2.0
    assert ps.PARAMETER_REGISTRY["TRIAGE_DEEP_HOURS"].default == 72
    assert ps.PARAMETER_REGISTRY["TRIAGE_GLANCE_HOURS"].default == 48
    assert ps.PARAMETER_REGISTRY["MAX_WATCH_WAKES_PER_DAY"].default == 6
    assert ps.PARAMETER_REGISTRY["FLASH_BRIEFING_INTERVAL_HOURS"].default == 4
    assert ps.PARAMETER_REGISTRY["WATCHDESK_EVAL_INTERVAL_MINUTES"].default == 15
    assert ps.PARAMETER_REGISTRY["EQUATION_LAB_MAX_PER_RUN"].default == 2


def test_db_failure_falls_back_to_default(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ps.mongo_query, "find_row", _boom)
    assert ps.get_param("MAX_POSITION_SIZE_PCT") == 0.10
    # Reads the registry rather than a literal: this test is about FAIL-OPEN
    # behaviour, not about any particular threshold, and hardcoding the number
    # made it fail for the wrong reason when the floor was re-fitted (2026-07-26).
    assert ps.get_param("ANALYSIS_CONFIDENCE_THRESHOLD") == \
        ps.PARAMETER_REGISTRY["ANALYSIS_CONFIDENCE_THRESHOLD"].default


def test_empty_table_returns_default(monkeypatch):
    monkeypatch.setattr(ps.mongo_query, "find_row", _fake_find_row(None))
    assert ps.get_param("MAX_CONCENTRATION_PCT") == 0.25


def test_stored_row_wins_and_int_kind_coerces(monkeypatch):
    seen = []
    monkeypatch.setattr(ps.mongo_query, "find_row", _fake_find_row((70.0,), seen))
    val = ps.get_param("ANALYSIS_CONFIDENCE_THRESHOLD")
    assert val == 70
    assert isinstance(val, int)
    # The stored value was fetched for THIS key, not some other row that
    # happened to carry the same number.
    assert seen == ["ANALYSIS_CONFIDENCE_THRESHOLD"]


def test_out_of_envelope_row_is_clamped_never_honored(monkeypatch):
    # A row wider than the registry bounds (e.g. bounds tightened later)
    # must clamp — a stored 0.90 size cap cannot leak through.
    monkeypatch.setattr(ps.mongo_query, "find_row", _fake_find_row((0.90,)))
    assert ps.get_param("MAX_POSITION_SIZE_PCT") == 0.20  # registry max


def test_cache_and_invalidate(monkeypatch):
    monkeypatch.setattr(ps.mongo_query, "find_row", _fake_find_row((0.15,)))
    assert ps.get_param("MAX_POSITION_SIZE_PCT") == 0.15
    # New DB value, cache still warm → old value served
    monkeypatch.setattr(ps.mongo_query, "find_row", _fake_find_row((0.12,)))
    assert ps.get_param("MAX_POSITION_SIZE_PCT") == 0.15
    ps.invalidate_cache("MAX_POSITION_SIZE_PCT")
    assert ps.get_param("MAX_POSITION_SIZE_PCT") == 0.12


def test_unknown_key_is_a_programming_error():
    try:
        ps.get_param("NOT_A_REAL_PARAM")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
