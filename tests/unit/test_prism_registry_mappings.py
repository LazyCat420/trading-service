"""Unit tests for prism agent registry mappings and fallback resolution."""

from app.services.prism_agent_registry import resolve_agent_id


def test_autoresearch_reflection_maps_to_synthesizer():
    assert resolve_agent_id("autoresearch_reflection") == "CUSTOM_SYNTHESIZER_AGENT"
    assert resolve_agent_id("reflection") == "CUSTOM_SYNTHESIZER_AGENT"


def test_auditors_map_to_meta_audit_agent():
    assert resolve_agent_id("auditor_1") == "CUSTOM_META_AUDIT_AGENT"
    assert resolve_agent_id("auditor_2") == "CUSTOM_META_AUDIT_AGENT"
    assert resolve_agent_id("auditor_3") == "CUSTOM_META_AUDIT_AGENT"
    assert resolve_agent_id("chief_auditor") == "CUSTOM_META_AUDIT_AGENT"
    assert resolve_agent_id("evaluator") == "CUSTOM_META_AUDIT_AGENT"
