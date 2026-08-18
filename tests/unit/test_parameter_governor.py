"""Parameter Governor + Validator — the safety envelope around agent-proposed
parameter changes: bounds, tiers, cooldowns, TTL asymmetry, daily budget.

These used to patch `parameter_store.get_db` / `parameter_governor.get_db` and
assert on SQL text ("expires_at" and "NULL" in the executed string,
"make_interval" in the executed string). `get_db` no longer exists — the store
reads via `mongo_query.find_row` and the governor writes via
`mongo_store.insert_docs` — so those monkeypatches set an attribute nobody
reads and the writes went nowhere the test could see.

Rewritten against the Mongo layer. The TTL assertions are stronger for it: a
SQL substring match only proved the word "NULL"/"make_interval" appeared
somewhere in the statement, whereas these read the actual `expires_at` value in
the inserted document, so a TTL written to the wrong field, computed with the
wrong sign, or attached to the wrong parameter key now fails here.
"""
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services import parameter_store as ps
from app.services import parameter_governor as gov
from app.validation import parameter_validator as pv


GOOD_REASON = "VIX spiked above 35 and three positions gapped through their stops overnight."


def setup_function(_fn):
    ps.invalidate_cache()


def _no_db(monkeypatch):
    """DB unreachable everywhere → resolver uses defaults, history lookups fail open."""
    def _boom(*a, **k):
        raise RuntimeError("db down")
    # The store resolves every value through mongo_query.find_row; making it
    # raise is what drives it onto the registry default.
    monkeypatch.setattr(ps.mongo_query, "find_row", _boom)
    monkeypatch.setattr(pv, "_last_change_age_hours", lambda key: None)
    monkeypatch.setattr(pv, "_changes_last_24h", lambda: 0)


class _CaptureStore:
    """Records the documents the governor inserts into Mongo."""

    def __init__(self):
        self.writes = []

    def insert_docs(self, collection, docs, *a, **k):
        self.writes.append((collection, docs))
        return len(docs)


def _capture_writes(monkeypatch):
    cap = _CaptureStore()
    monkeypatch.setattr(gov, "mongo_store", cap)
    return cap


def _last_doc(cap):
    """The single document of the most recent insert, with its collection checked."""
    collection, docs = cap.writes[-1]
    assert collection == "runtime_parameters"
    assert len(docs) == 1
    return docs[0]


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

    cap = _capture_writes(monkeypatch)
    res = gov.propose_parameter_change(
        "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20, GOOD_REASON, agent="v3_board_of_directors")
    assert res["status"] == "applied"
    # The board's change is the one that reached the store, under its own key.
    doc = _last_doc(cap)
    assert doc["param_key"] == "MAX_PORTFOLIO_DRAWDOWN_PCT"
    assert doc["set_by"] == "v3_board_of_directors"


def test_ttl_above_max_rejected(monkeypatch):
    _no_db(monkeypatch)
    res = gov.propose_parameter_change(
        "MAX_POSITION_SIZE_PCT", 0.08, GOOD_REASON, ttl_hours=10_000, agent="v3_board_of_directors")
    assert res["status"] == "rejected"
    assert "ttl" in res["reason"].lower()


# ── Tighten/loosen asymmetry ────────────────────────────────────────────────

def test_tightening_applies_immediately_without_ttl(monkeypatch):
    _no_db(monkeypatch)
    cap = _capture_writes(monkeypatch)
    # Lowering the size cap from 0.10 default is SAFER (RISK_UP param).
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.05, GOOD_REASON, agent="v3_portfolio_manager")
    assert res["status"] == "applied"
    assert res["loosening"] is False
    assert res["expires_in_hours"] is None
    # A tightening is permanent: the stored row carries no expiry at all.
    doc = _last_doc(cap)
    assert doc["param_key"] == "MAX_POSITION_SIZE_PCT"
    assert doc["value"] == 0.05
    assert doc["expires_at"] is None
    assert doc["status"] == "active"


def test_loosening_gets_default_ttl(monkeypatch):
    _no_db(monkeypatch)
    cap = _capture_writes(monkeypatch)
    # Raising the size cap above the 0.10 default is LOOSENING.
    before = datetime.now(timezone.utc)
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.15, GOOD_REASON, agent="v3_board_of_directors")
    assert res["status"] == "applied"
    assert res["loosening"] is True
    ttl = ps.PARAMETER_REGISTRY["MAX_POSITION_SIZE_PCT"].loosen_ttl_hours
    assert res["expires_in_hours"] == ttl
    # The stored row actually expires, and does so that many hours out — not
    # merely "the statement mentioned an interval".
    doc = _last_doc(cap)
    assert doc["expires_at"] is not None
    ahead_hours = (doc["expires_at"] - before).total_seconds() / 3600.0
    assert ttl - 0.01 <= ahead_hours <= ttl + 0.01


def test_lowering_confidence_threshold_is_loosening(monkeypatch):
    _no_db(monkeypatch)
    _capture_writes(monkeypatch)
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
    cap = _capture_writes(monkeypatch)
    res = gov.propose_parameter_change("MAX_POSITION_SIZE_PCT", 0.04, GOOD_REASON, agent="v3_portfolio_manager")
    assert res["status"] == "applied"
    # It really wrote, rather than being waved through without a store write.
    assert _last_doc(cap)["value"] == 0.04


def test_list_parameters_covers_full_registry(monkeypatch):
    _no_db(monkeypatch)
    out = gov.list_parameters()
    keys = {p["key"] for p in out["parameters"]}
    assert keys == set(ps.PARAMETER_REGISTRY)
    for p in out["parameters"]:
        assert "value" in p and "min" in p and "max" in p and "tier" in p
