"""The deploy script cannot re-arm a routing pin, and the container's env is checked.

On 2026-09-02 `deploy.sh` appended `SOLO_JETSON_MODE=${SOLO_JETSON_MODE:-true}`
and `DECISION_MODEL_PATTERN=${DECISION_MODEL_PATTERN:-deepseek|nemotron}` to the
NAS container's `.env`. Nothing failed: the desk simply stopped using the DGX
Spark for two days (191 nemotron runs, 0 GLM). Both defects live in text a unit
test can parse, so they are gated here rather than left to a reader.

The condemned text is embedded as a FIXTURE, copied verbatim from
`git show 93330e6:deploy.sh` lines 127-128. It is NOT read back out of git: a
control pinned to a moving ref passes for the wrong reason once the fix lands.
"""
import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

#: git show 93330e6:deploy.sh, lines 127-128 — the two lines that caused it.
CONDEMNED_93330E6 = (
    """  ssh "$DEPLOY_SSH_HOST" "echo 'SOLO_JETSON_MODE=${SOLO_JETSON_MODE:-true}' >> '${DEPLOY_COMPOSE_DIR}/.env'"\n"""
    """  ssh "$DEPLOY_SSH_HOST" "echo 'DECISION_MODEL_PATTERN=${DECISION_MODEL_PATTERN:-deepseek|nemotron}' >> '${DEPLOY_COMPOSE_DIR}/.env'"\n"""
)

#: The dev's 2026-09-03 fix: default flipped, glm added. Still writes the flag,
#: and still lets an operator's shell decide — which is how it was armed.
HALF_FIXED_21BC4B6 = (
    """  ssh "$DEPLOY_SSH_HOST" "echo 'SOLO_JETSON_MODE=${SOLO_JETSON_MODE:-false}' >> '${DEPLOY_COMPOSE_DIR}/.env'"\n"""
    """  ssh "$DEPLOY_SSH_HOST" "echo 'DECISION_MODEL_PATTERN=${DECISION_MODEL_PATTERN:-deepseek|nemotron|glm}' >> '${DEPLOY_COMPOSE_DIR}/.env'"\n"""
)

#: The NAS container's .env, lines 380-381, read 2026-09-03 17:30 PT.
NAS_ENV_2026_09_02 = {
    "SOLO_JETSON_MODE": "true",
    "DECISION_MODEL_PATTERN": "deepseek|nemotron",
    "DGX_MAX_CONCURRENT": "6",
    "JETSON_MAX_CONCURRENT": "6",
}

CLEAN_ENV = {
    "DECISION_MODEL_PATTERN": "deepseek|nemotron|glm",
    "DGX_MAX_CONCURRENT": "6",
    "JETSON_MAX_CONCURRENT": "6",
}


@pytest.fixture(scope="module")
def vs():
    spec = importlib.util.spec_from_file_location(
        "verify_shipped_routing_gate", REPO / "scripts" / "verify_shipped.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTheScriptText:
    def test_the_condemned_lines_are_caught(self, vs):
        found = vs.deploy_env_violations(CONDEMNED_93330E6)
        assert len(found) == 2, found
        assert any("SOLO_JETSON_MODE" in f for f in found)
        assert any("glm" in f for f in found)

    def test_the_half_fix_is_still_caught(self, vs):
        """Defaulting the pin to false is not the same as removing it."""
        found = vs.deploy_env_violations(HALF_FIXED_21BC4B6)
        assert len(found) == 1 and "SOLO_JETSON_MODE" in found[0], found

    def test_the_real_deploy_script_is_clean(self, vs):
        assert vs.deploy_env_violations((REPO / "deploy.sh").read_text()) == []

    def test_a_comment_mentioning_the_flag_is_not_a_violation(self, vs):
        assert vs.deploy_env_violations("  # SOLO_JETSON_MODE was removed 2026-09-03\n") == []


class TestTheContainerEnv:
    def test_the_09_02_env_fails_both_ways(self, vs):
        statuses = {c: s for c, s, _ in vs.routing_env_verdicts(NAS_ENV_2026_09_02)}
        assert list(statuses.values()).count(vs.FAIL) == 2, statuses

    def test_a_clean_env_passes(self, vs):
        assert vs.FAIL not in [s for _, s, _ in vs.routing_env_verdicts(CLEAN_ENV)]

    def test_a_pattern_without_glm_fails_even_without_the_flag(self, vs):
        env = dict(CLEAN_ENV, DECISION_MODEL_PATTERN="deepseek|nemotron")
        assert vs.FAIL in [s for _, s, _ in vs.routing_env_verdicts(env)]


class TestTheLiveRoutes:
    def test_decision_on_the_dgx_passes_and_collector_on_the_jetson_passes(self, vs):
        rows = vs.route_verdicts(("GLM-5.3-Flash-EXL3", "vllm-2"), ("nemotron35", "vllm"))
        assert [s for _, s, _ in rows] == [vs.PASS, vs.PASS]

    def test_a_decision_agent_on_the_jetson_warns_not_fails(self, vs):
        """Overflow and fallback both put a decision agent on the Jetson; only
        the cycle's telemetry can say whether it is permanent."""
        rows = vs.route_verdicts(("nemotron35", "vllm"), ("nemotron35", "vllm"))
        assert rows[0][1] == vs.WARN

    def test_a_failed_resolution_fails(self, vs):
        rows = vs.route_verdicts("ModelContractError: dgx serving qwen", ("nemotron35", "vllm"))
        assert rows[0][1] == vs.FAIL

    def test_the_remote_probe_asks_for_both_routes(self, vs):
        assert "decision_route" in vs.REMOTE_PROBE and "collector_route" in vs.REMOTE_PROBE
        assert "routing_env" in vs.REMOTE_PROBE
