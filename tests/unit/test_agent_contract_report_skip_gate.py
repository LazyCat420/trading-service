"""agent_contract_report must not score a designed FA/VA skip as 6 FAILs.

2026-08-31: both one-ticker observe cycles hit JA triage QUANT_ONLY — FA is
stubbed ("Fundamental analysis bypassed" in data_gaps) and valuation is never
queued ("valuation IS fundamental analysis"). The report scored 23/29 with 6
false FAILs each time. Same class as the debate-skip N/A branch it already has.
"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "agent_contract_report",
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "agent_contract_report.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_checks = _mod._checks

_BASE = {
    "regime_classification": {"forward_call": "FLAT", "regime": "HIGH_VOLATILITY"},
    "desk_note": {"triage_recommendation": "QUANT_ONLY", "catalyst_call": "none", "data_gaps": []},
    "quant_report": {"overlays": {"x": 1}, "risk_metrics": {"atr": 1}},
    "final_decision": {"action": "HOLD", "conviction_vector": {"data_quality": 60}},
    "trade_decision": {"signal_weights": {"q": 1}, "internal_consensus_score": 0.5},
}


def test_quant_only_skip_is_na_not_fail():
    desk = dict(_BASE)
    desk["fundamental_report"] = {
        "summary": "Fundamental analysis skipped on the Junior Analyst's triage recommendation (QUANT_ONLY)",
        "pillars": {"revenue_growth": "Not analyzed"},
        "data_gaps": ["DataGap: Fundamental analysis bypassed"],
    }
    rows = _checks(desk, wb_sections={"signals", "market_context"})
    failing = [(a, n) for a, n, ok, _ in rows if not ok]
    assert not any(a in ("fundamental", "valuation") for a, _ in failing), failing
    assert any("FA+VA checks N/A" in n for _, n, ok, _ in rows if ok)


def test_real_fa_run_still_fails_on_missing_fields():
    """The gate must not fail open: a REAL FA artifact missing its promised
    fields still scores FAIL."""
    desk = dict(_BASE)
    desk["desk_note"] = {**_BASE["desk_note"], "triage_recommendation": "FULL"}
    desk["fundamental_report"] = {
        "summary": "real analysis", "pillars": {}, "data_gaps": [],
        "thesis_direction": "NEUTRAL",
    }
    desk["valuation_report"] = {}
    rows = _checks(desk, wb_sections={"signals", "market_context"})
    failing = {(a, n) for a, n, ok, _ in rows if not ok}
    assert ("fundamental", "near_term_read present") in failing
    assert ("valuation", "verdict present") in failing
