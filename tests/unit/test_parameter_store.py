"""Parameter store — registry envelope sanity + resolver fallback semantics.

The store must be impossible to break the trading path with: an empty table,
an unreachable DB, or an out-of-envelope row must all resolve to safe values.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services import parameter_store as ps


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params=None):
        return _FakeResult(self._row)


def _fake_get_db(row):
    class _Ctx:
        def __enter__(self):
            return _FakeDB(row)

        def __exit__(self, *a):
            return False

    return lambda: _Ctx()


def setup_function(_fn):
    ps.invalidate_cache()


def test_registry_defaults_inside_their_own_bounds():
    for key, spec in ps.PARAMETER_REGISTRY.items():
        assert spec.min_value <= spec.default <= spec.max_value, key
        assert spec.direction in (ps.RISK_UP, ps.RISK_DOWN, ps.RISK_NEUTRAL), key
        assert spec.tier in (ps.TIER_STANDARD, ps.TIER_BOARD), key


def test_defaults_match_previous_hardcoded_values():
    # Parity guard: an empty store must reproduce pre-store behavior exactly.
    assert ps.PARAMETER_REGISTRY["MAX_POSITION_SIZE_PCT"].default == 0.10
    assert ps.PARAMETER_REGISTRY["MAX_CONCENTRATION_PCT"].default == 0.25
    assert ps.PARAMETER_REGISTRY["ANALYSIS_CONFIDENCE_THRESHOLD"].default == 65
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
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(ps, "get_db", _boom)
    assert ps.get_param("MAX_POSITION_SIZE_PCT") == 0.10
    assert ps.get_param("ANALYSIS_CONFIDENCE_THRESHOLD") == 65


def test_empty_table_returns_default(monkeypatch):
    monkeypatch.setattr(ps, "get_db", _fake_get_db(None))
    assert ps.get_param("MAX_CONCENTRATION_PCT") == 0.25


def test_stored_row_wins_and_int_kind_coerces(monkeypatch):
    monkeypatch.setattr(ps, "get_db", _fake_get_db((70.0,)))
    val = ps.get_param("ANALYSIS_CONFIDENCE_THRESHOLD")
    assert val == 70
    assert isinstance(val, int)


def test_out_of_envelope_row_is_clamped_never_honored(monkeypatch):
    # A row wider than the registry bounds (e.g. bounds tightened later)
    # must clamp — a stored 0.90 size cap cannot leak through.
    monkeypatch.setattr(ps, "get_db", _fake_get_db((0.90,)))
    assert ps.get_param("MAX_POSITION_SIZE_PCT") == 0.20  # registry max


def test_cache_and_invalidate(monkeypatch):
    monkeypatch.setattr(ps, "get_db", _fake_get_db((0.15,)))
    assert ps.get_param("MAX_POSITION_SIZE_PCT") == 0.15
    # New DB value, cache still warm → old value served
    monkeypatch.setattr(ps, "get_db", _fake_get_db((0.12,)))
    assert ps.get_param("MAX_POSITION_SIZE_PCT") == 0.15
    ps.invalidate_cache("MAX_POSITION_SIZE_PCT")
    assert ps.get_param("MAX_POSITION_SIZE_PCT") == 0.12


def test_unknown_key_is_a_programming_error():
    try:
        ps.get_param("NOT_A_REAL_PARAM")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
