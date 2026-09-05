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
        """Raw context passes through, up to the configured ceiling.

        The ceiling was a hardcoded 128_000 until `1f7b66f` (2026-09-04) made it
        `int(os.getenv("MAX_CONTEXT_BUDGET_CEILING", "1000000"))`. This test
        still asserted 128_000 and went red on master; it was not run.

        It now reads the ceiling from the module instead of transcribing it, so
        a deliberate change to the default does not break the test while an
        accidental change to the SHAPE (a discount reapplied, the min() dropped,
        a non-numeric env value swallowed) still does.
        """
        from app.config import context_budget
        from app.config.context_budget import _effective_from_raw

        ceiling = context_budget._effective_from_raw(10**12)
        assert ceiling > 0, "the ceiling must bound something"

        # Below the ceiling: raw passes through undiscounted (EFFECTIVE_RATIO 1.0).
        assert _effective_from_raw(8192) == 8192
        assert _effective_from_raw(32768) == 32768
        assert _effective_from_raw(ceiling - 1) == ceiling - 1

        # At and above it: clamped, never exceeded.
        assert _effective_from_raw(ceiling) == ceiling
        assert _effective_from_raw(ceiling * 2) == ceiling

    def test_the_context_ceiling_is_env_overridable(self, monkeypatch):
        """`MAX_CONTEXT_BUDGET_CEILING` is read per call, not at import."""
        from app.config.context_budget import _effective_from_raw

        monkeypatch.setenv("MAX_CONTEXT_BUDGET_CEILING", "50000")
        assert _effective_from_raw(1048576) == 50000
        monkeypatch.setenv("MAX_CONTEXT_BUDGET_CEILING", "200000")
        assert _effective_from_raw(1048576) == 200000

    def test_the_ceiling_reaches_no_production_caller(self):
        """AUDIT 2026-09-05 — this whole path is dead, and that is the finding.

        `_effective_from_raw` is called only by `register_model_context`, whose
        documented caller `vllm_client.discover_roles()` was deleted in
        `c82526b`. Nothing in `app/` calls either, so `get_context_budget()`
        always returns the hardcoded `_DEFAULT_BUDGET` and the 1M ceiling above
        changes no request in production.

        This test exists so the claim is CHECKED rather than remembered. When
        someone re-wires model discovery, this goes red and the audit note in
        the plan (step 1, item 2) becomes stale in a visible way.
        """
        import pathlib
        import re

        app = pathlib.Path(__file__).resolve().parents[2] / "app"
        callers = []
        for py in app.rglob("*.py"):
            if py.name == "context_budget.py":
                continue
            text = py.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"\bregister_model_context\b", text):
                callers.append(str(py.relative_to(app)))
        assert callers == [], (
            "register_model_context has a caller again "
            f"({callers}) — the context ceiling is no longer dead code, so "
            "the 1M default now reaches real requests and needs the capacity "
            "test the audit deferred."
        )

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
