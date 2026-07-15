"""
Tests for the dynamic regime classification (plan Section 1).

The Regime Engine now emits factors / market_context_tags / board_directive /
suggested_pipeline_modifications alongside the coarse 3-value label, the
orchestrator honors the engine's own skip recommendation (not a hardcoded
label comparison), and the desk context renders the new fields so downstream
agents actually see them.
"""
from app.v3.artifacts import validate_artifact
from app.v3.orchestrator import _regime_recommends_skip_fa
from app.v3.shared_desk import SharedDesk


FULL_ARTIFACT = {
    "regime": "CONTRADICTORY",
    "confidence": 70,
    "rationale": "Mixed signals across breadth and vol.",
    "factors": {
        "volatility": 0.7, "trend_strength": 0.3, "macro_risk": 0.8,
        "sector_momentum": 0.4, "liquidity": 0.6,
    },
    "market_context_tags": ["rate-sensitive", "earnings-week"],
    "board_directive": "Weight quant signals first; demand wider stops.",
    "suggested_pipeline_modifications": [],
}


# ── Schema accepts the new fields ────────────────────────────────────

def test_schema_accepts_dynamic_regime_fields():
    assert validate_artifact("regime_classification", FULL_ARTIFACT) == []


def test_schema_still_enforces_regime_enum():
    bad = dict(FULL_ARTIFACT, regime="MOON_PHASE")
    errors = validate_artifact("regime_classification", bad)
    assert any("regime" in e for e in errors)


# ── Skip-FA decision belongs to the Regime Engine ────────────────────

def test_engine_skip_recommendation_honored():
    content = dict(FULL_ARTIFACT, suggested_pipeline_modifications=["skip_fundamental_analyst"])
    assert _regime_recommends_skip_fa(content) is True


def test_engine_empty_mods_means_run_fa_even_in_high_vol():
    # The engine explicitly said "no modifications" — its call beats the label
    content = dict(FULL_ARTIFACT, regime="HIGH_VOLATILITY", suggested_pipeline_modifications=[])
    assert _regime_recommends_skip_fa(content) is False


def test_missing_mods_falls_back_to_legacy_label_heuristic():
    content = {"regime": "HIGH_VOLATILITY", "confidence": 80}
    assert _regime_recommends_skip_fa(content) is True
    content = {"regime": "DEEP_DISCOUNT", "confidence": 80}
    assert _regime_recommends_skip_fa(content) is False


# ── Desk context renders the new fields for downstream agents ────────

def test_desk_context_renders_factors_tags_and_directive():
    desk = SharedDesk(ticker="TEST", cycle_id="c1")
    desk.append_artifact("regime_classification", dict(FULL_ARTIFACT))

    ctx = desk.get_compressed_context()

    assert "volatility=0.7" in ctx
    assert "rate-sensitive" in ctx
    assert "Directive to the Board" in ctx
    assert "wider stops" in ctx


def test_desk_context_tolerates_minimal_regime_artifact():
    desk = SharedDesk(ticker="TEST", cycle_id="c1")
    desk.append_artifact("regime_classification", {
        "regime": "DEEP_DISCOUNT", "confidence": 88, "rationale": "calm",
    })
    ctx = desk.get_compressed_context()
    assert "DEEP_DISCOUNT" in ctx
    assert "Directive" not in ctx
