"""
Tests for Context Window Governance system.

Covers:
  - Context budget registry (model-aware budgeting)
  - Tool result compression (inline summarization)
  - Progressive summarizer (debate turn + research compression)
  - Debate prompt capping
  - History compression with model-aware thresholds

All tests are pure unit tests — no DB, no LLM, no network calls.
"""


# ── Phase 1: Context Budget Registry ──────────────────────────────────


class TestContextBudget:
    """Tests for app.config.context_budget."""

    def test_default_budget_returns_valid(self):
        """Default budget should be usable even before model discovery."""
        from app.config.context_budget import get_context_budget

        budget = get_context_budget()
        assert budget.effective_context_tokens > 0
        assert budget.compressor_threshold > 0
        assert budget.data_context_chars > 0
        assert budget.tool_result_chars > 0

    def test_register_model_creates_budget(self):
        """Registering a model should create a budget accessible via get_context_budget."""
        from app.config.context_budget import register_model_context, get_context_budget

        budget = register_model_context("test-model-32k", 32768)
        assert budget.raw_context_tokens == 32768
        assert budget.effective_context_tokens == 32768

        # Should be retrievable
        fetched = get_context_budget("test-model-32k")
        assert fetched.model_id == "test-model-32k"

    def test_effective_from_raw_scaling(self):
        """Effective context should return raw context up to a 128k cap."""
        from app.config.context_budget import _effective_from_raw

        # Asserts 100% of raw context up to a 128K hard cap
        assert _effective_from_raw(8192) == 8192
        assert _effective_from_raw(32768) == 32768
        assert _effective_from_raw(131072) == 128000
        assert _effective_from_raw(262144) == 128000
        assert _effective_from_raw(1048576) == 128000

    def test_compressor_threshold_is_75_percent(self):
        """Compressor threshold should be 75% of effective context."""
        from app.config.context_budget import register_model_context

        budget = register_model_context("test-threshold", 32768)
        expected_effective = 32768
        expected_threshold = int(expected_effective * 0.75)
        assert budget.compressor_threshold == expected_threshold


    def test_total_allocated_fits_within_effective(self):
        """Sum of all budget slices should not exceed effective context."""
        from app.config.context_budget import register_model_context

        for raw in [8192, 16384, 32768, 65536, 131072, 262144]:
            budget = register_model_context(f"test-alloc-{raw}", raw)
            # Total allocated should be ≤ effective (with 3% margin for rounding)
            assert budget.total_allocated <= budget.effective_context_tokens, (
                f"raw={raw}: allocated={budget.total_allocated} > "
                f"effective={budget.effective_context_tokens}"
            )

    def test_estimate_tokens(self):
        """Token estimation delegates to context_gate's tiktoken-based counter
        (falling back to the chars/4 heuristic), so assert consistency with the
        delegate rather than heuristic-specific values."""
        from app.config.context_budget import estimate_tokens
        from app.services.context_gate import estimate_tokens as gate_estimate

        assert estimate_tokens("") == 0
        assert estimate_tokens("a" * 100) == gate_estimate("a" * 100)
        assert estimate_tokens("hello world") == gate_estimate("hello world")
        assert estimate_tokens("hello world") >= 1

    def test_partial_model_id_match(self):
        """Should match partial model IDs for HuggingFace-style paths."""
        from app.config.context_budget import register_model_context, get_context_budget

        register_model_context("org/big-model-v2", 32768)

        # Partial match should work
        budget = get_context_budget("big-model-v2")
        assert budget.model_id == "org/big-model-v2"


# ── End-to-End Context Sizing ──────────────────────────────────────


class TestContextSizing:
    """Integration-level tests that verify context budgets are enforced."""

    def test_all_budgets_consistent(self):
        """All registered budgets should have consistent internal state."""
        from app.config.context_budget import register_model_context

        for raw in [8192, 16384, 32768, 65536, 131072, 262144]:
            b = register_model_context(f"consistency-{raw}", raw)
            # Compressor threshold < effective
            assert b.compressor_threshold < b.effective_context_tokens
            # Char conversions are consistent
            assert b.data_context_chars == b.data_context_budget * 4
            assert b.tool_result_chars == b.tool_result_budget * 4
            # Total allocated doesn't exceed effective
            assert b.total_allocated <= b.effective_context_tokens

    def test_war_context_budget_enforced(self):
        """War context chars budget should be reasonable."""
        from app.config.context_budget import get_context_budget

        budget = get_context_budget()
        # War context should be a small slice
        assert budget.war_context_chars > 0
        assert budget.war_context_chars < budget.effective_context_chars
        # Should be < 25% of total effective
        assert budget.war_context_chars < budget.effective_context_chars * 0.25
