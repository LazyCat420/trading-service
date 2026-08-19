"""
Blast-radius limits for automated self-healing patches.

These assertions are the boundary between "the bot fixes the trading cycle" and
"the bot rewrites its own guardrails". Treat a failure here as a security-level
regression, not a style nit.
"""
import pytest

from app.cognition.evolution.repair_scope import (
    assert_patchable,
    is_patchable,
)


@pytest.mark.parametrize("path", [
    "app/v3/orchestrator.py",
    "app/v3/agent_runner.py",
    "app/collectors/yfinance_collector.py",
    "app/cognition/debate/debate_coordinator.py",
    "app/services/pipeline_service.py",
    "app/trading/scoring_engine.py",
    "app/autoresearch/eval_engine.py",
    "app/agents/prompts/evolve_designer.md",
])
def test_trading_cycle_source_is_patchable(path):
    allowed, reason = is_patchable(path)
    assert allowed, f"{path} should be repairable but was refused: {reason}"


@pytest.mark.parametrize("path,why", [
    # The fixer must not be able to rewrite the fixer.
    ("app/cognition/evolution/debate.py", "repair machinery"),
    ("app/cognition/evolution/repair_scope.py", "the scope guard itself"),
    # A bad migration is not recoverable by rolling back a source patch.
    ("scripts/migration/pg_migrations.py", "schema"),
    ("scripts/migration/schema_pg.sql", "schema"),
    # Settings and secrets handling.
    ("app/config/settings.py", "config"),
    # Build/deploy/dependency surfaces.
    ("deploy.sh", "deploy"),
    ("Dockerfile", "build"),
    ("docker-compose.yml", "build"),
    ("requirements.txt", "dependencies"),
    ("entrypoint.sh", "build"),
    ("scripts/self_healing_watchdog.py", "the watchdog itself"),
    (".github/workflows/ci.yml", "CI"),
    # Letting the fixer edit tests lets it pass by deleting the assertion.
    ("tests/unit/test_repair_scope.py", "tests"),
])
def test_protected_paths_are_refused(path, why):
    allowed, reason = is_patchable(path)
    assert not allowed, f"{path} ({why}) must NOT be auto-patchable — got: {reason}"


@pytest.mark.parametrize("path", [
    "../../../etc/passwd",
    "app/v3/../../../etc/passwd",
    "/etc/passwd",
    "/app/v3/orchestrator.py",
])
def test_path_traversal_is_refused(path):
    allowed, _ = is_patchable(path)
    assert not allowed, f"{path} escaped the repo root but was allowed"


def test_unknown_paths_default_to_refused():
    """Deny-by-default: an unrecognised path is refused, not allowed."""
    allowed, _ = is_patchable("some/random/module.py")
    assert not allowed


@pytest.mark.parametrize("path", [
    "app/v3/orchestrator.pyc",
    "app/v3/config.yml",
    "app/v3/data.json",
    "",
])
def test_non_source_suffixes_refused(path):
    allowed, _ = is_patchable(path)
    assert not allowed


def test_deny_beats_allow():
    """A denied path inside an allowed prefix stays denied."""
    # app/cognition/ is allowed; app/cognition/evolution/ is carved back out.
    assert is_patchable("app/cognition/debate/thesis_agent.py")[0] is True
    assert is_patchable("app/cognition/evolution/deployer.py")[0] is False


def test_assert_patchable_raises_with_reason():
    with pytest.raises(PermissionError, match="Automated repair refused"):
        assert_patchable("scripts/migration/pg_migrations.py")
    # And stays silent on a legitimate target.
    assert_patchable("app/v3/orchestrator.py")
