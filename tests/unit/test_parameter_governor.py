"""Parameter Governor + Validator — the safety envelope around agent-proposed
parameter changes: bounds, tiers, cooldowns, TTL asymmetry, daily budget."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services import parameter_store as ps
from app.services import parameter_governor as gov
from app.validation import parameter_validator as pv


GOOD_REASON = "VIX spiked above 35 and three positions gapped through their stops overnight."


def setup_function(_fn):
    ps.invalidate_cache()


def _no_db(monkeypatch):
    """DB unreachable everywhere → resolver uses defaults, history lookups fail open."""
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(ps, "get_db", _boom)
    monkeypatch.setattr(pv, "_last_change_age_hours", lambda key: None)
    monkeypatch.setattr(pv, "_changes_last_24h", lambda: 0)


class _CaptureDB:
    def __init__(self):
        self.writes = []

    def execute(self, sql, params=None):
        self.writes.append((" ".join(sql.split()), params))
        class _R:
            def fetchone(self_inner):
                return None
        return _R()


def _capture_get_db(cap):
    class _Ctx:
        def __enter__(self):
            return cap
        def __exit__(self, *a):
            return False
    return lambda: _Ctx()


# ── Validator rejections (teach-y messages) ──────────────────────────────────

def test_unknown_key_rejected_lists_known_keys(monkeypatch):
    _no_db(monkeypatch)
    res = gov.propose_parameter_change("NOT_A_PARAM", 1.0, GOOD_REASON, agent="v3_board_of_directors")
    assert res["status"] == "rejected"
    assert "MAX_POSITION_SIZE_PCT" in res["reason"]


def test_out_of_bounds_rejected_states_envelope(monkeypatch):
    _no_db(monkeypatch)
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.50, GOOD_REASON, agent="v3_board_of_directors")
    assert res["status"] == "rejected"
    assert "[0.02, 0.2]" in res["reason"]


def test_vague_reason_rejected(monkeypatch):
    _no_db(monkeypatch)
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.08, "vibes", agent="v3_board_of_directors")
    assert res["status"] == "rejected"
    assert "reason" in res["reason"].lower()


def test_worker_agent_not_authorized(monkeypatch):
    _no_db(monkeypatch)
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.08, GOOD_REASON, agent="v3_junior_analyst")
    assert res["status"] == "rejected"
    assert "not authorized" in res["reason"]


def test_board_tier_param_blocked_for_pm_allowed_for_board(monkeypatch):
    _no_db(monkeypatch)
    res = gov.propose_parameter_change(
        "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20, GOOD_REASON, agent="v3_portfolio_manager")
    assert res["status"] == "rejected"
    assert "board" in res["reason"]

    cap = _CaptureDB()
    monkeypatch.setattr(gov, "get_db", _capture_get_db(cap))
    res = gov.propose_parameter_change(
        "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20, GOOD_REASON, agent="v3_board_of_directors")
    assert res["status"] == "applied"


def test_ttl_above_max_rejected(monkeypatch):
    _no_db(monkeypatch)
    res = gov.propose_parameter_change(
        "MAX_POSITION_SIZE_PCT", 0.08, GOOD_REASON, ttl_hours=10_000, agent="v3_board_of_directors")
    assert res["status"] == "rejected"
    assert "ttl" in res["reason"].lower()


# ── Tighten/loosen asymmetry ────────────────────────────────────────────────

def test_tightening_applies_immediately_without_ttl(monkeypatch):
    _no_db(monkeypatch)
    cap = _CaptureDB()
    monkeypatch.setattr(gov, "get_db", _capture_get_db(cap))
    # Lowering the size cap from 0.10 default is SAFER (RISK_UP param).
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.05, GOOD_REASON, agent="v3_portfolio_manager")
    assert res["status"] == "applied"
    assert res["loosening"] is False
    assert res["expires_in_hours"] is None
    sql, params = cap.writes[-1]
    assert "expires_at" in sql and "NULL" in sql


def test_loosening_gets_default_ttl(monkeypatch):
    _no_db(monkeypatch)
    cap = _CaptureDB()
    monkeypatch.setattr(gov, "get_db", _capture_get_db(cap))
    # Raising the size cap above the 0.10 default is LOOSENING.
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.15, GOOD_REASON, agent="v3_board_of_directors")
    assert res["status"] == "applied"
    assert res["loosening"] is True
    assert res["expires_in_hours"] == ps.PARAMETER_REGISTRY["MAX_POSITION_SIZE_PCT"].loosen_ttl_hours
    sql, params = cap.writes[-1]
    assert "make_interval" in sql


def test_lowering_confidence_threshold_is_loosening(monkeypatch):
    _no_db(monkeypatch)
    cap = _CaptureDB()
    monkeypatch.setattr(gov, "get_db", _capture_get_db(cap))
    # RISK_DOWN param: lower value = riskier.
    res = gov.propose_parameter_change(
        "ANALYSIS_CONFIDENCE_THRESHOLD", 55, GOOD_REASON, agent="v3_portfolio_manager")
    assert res["status"] == "applied"
    assert res["loosening"] is True
    assert res["expires_in_hours"] is not None


def test_loosening_cooldown_enforced(monkeypatch):
    _no_db(monkeypatch)
    monkeypatch.setattr(pv, "_last_change_age_hours", lambda key: 1.0)  # changed 1h ago
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.15, GOOD_REASON, agent="v3_board_of_directors")
    assert res["status"] == "rejected"
    assert "cooldown" in res["reason"].lower()


def test_daily_budget_enforced_for_loosening(monkeypatch):
    _no_db(monkeypatch)
    monkeypatch.setattr(pv, "_changes_last_24h", lambda: pv.MAX_CHANGES_PER_DAY)
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.15, GOOD_REASON, agent="v3_board_of_directors")
    assert res["status"] == "rejected"
    assert "budget" in res["reason"].lower()


def test_tightening_skips_cooldown_and_budget(monkeypatch):
    _no_db(monkeypatch)
    monkeypatch.setattr(pv, "_last_change_age_hours", lambda key: 0.1)
    monkeypatch.setattr(pv, "_changes_last_24h", lambda: 999)
    cap = _CaptureDB()
    monkeypatch.setattr(gov, "get_db", _capture_get_db(cap))
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.04, GOOD_REASON, agent="v3_portfolio_manager")
    assert res["status"] == "applied"


def test_list_parameters_covers_full_registry(monkeypatch):
    _no_db(monkeypatch)
    out = gov.list_parameters()
    keys = {p["key"] for p in out["parameters"]}
    assert keys == set(ps.PARAMETER_REGISTRY)
    for p in out["parameters"]:
        assert "value" in p and "min" in p and "max" in p and "tier" in p
