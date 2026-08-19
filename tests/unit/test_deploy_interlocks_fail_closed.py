"""The deploy interlocks must read Mongo, and must fail CLOSED.

WHAT WAS BROKEN
---------------
Two hooks stop a deploy while a trading cycle is live, because deploying
restarts the container and kills the cycle mid-flight, stranding
`pipeline_state`. Both read that state from POSTGRES, and both failed OPEN:

  .claude/hooks/guard_deploy.py
      every failure path called `warn()`, which exits 0 = allow.

  trading-service/.claude/hooks/_check_cycle_running.py
      imported `psycopg2` — which is not installed (the repo uses psycopg3, and
      since the 2026-08-18 teardown no Postgres driver is in the image at all)
      — and swallowed the ImportError into `print("unknown|")`. Its caller,
      `guard_deploy_during_cycle.sh`, only blocked on `running*`, so `unknown`
      fell through to exit 0.

So the second hook has been permitting every deploy, and the first was one
migration step from doing the same: `pipeline_state` is staged at `:mongo` in
`deploy-kit/.env.deploy`, after which the Postgres row stops being written and
the probe reads an empty table — which looks exactly like "no cycle running".

Three separate ways to arrive at "allow" with no evidence. That is the
"a check that passes for both states is not a check" shape, and the cost of a
false allow here is a killed 30-minute cycle and a half-executed decision set.

WHAT THESE TESTS PIN
--------------------
They run the hooks as SUBPROCESSES with a real payload and assert exit codes,
rather than inspecting source text: the contract is "exit 2 blocks, exit 0
allows", and that is what a wrong refactor breaks. The Mongo they read is the
isolated `trading_bot_pytest` database — never production.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

SUN = pathlib.Path(
    os.environ.get("CLAUDE_PROJECT_DIR") or "/home/lazycat/github/projects/sun"
)
REPO = pathlib.Path(__file__).resolve().parents[2]

GUARD_DEPLOY = SUN / ".claude" / "hooks" / "guard_deploy.py"
GUARD_CYCLE_SH = REPO / ".claude" / "hooks" / "guard_deploy_during_cycle.sh"

PAYLOAD = json.dumps({
    "tool_input": {"command": "cd trading-service && ./deploy.sh"},
    "cwd": str(SUN / "trading-service"),
})

UNREACHABLE = "mongodb://127.0.0.1:1/"

pytestmark = pytest.mark.real_mongo


@pytest.fixture
def state(real_mongo):
    """Write `pipeline_state` in the ISOLATED database the fixture pins."""
    coll = real_mongo["pipeline_state"]
    coll.delete_many({})

    def _set(status: str, cycle_id: str = "cycle-TEST") -> None:
        coll.delete_many({})
        coll.insert_one({
            "singleton_id": "current", "status": status,
            "phase": "analyzing", "cycle_id": cycle_id,
        })

    yield _set
    coll.delete_many({})


def _run(cmd: list[str], env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Point BOTH hooks at the isolated test database.
    env["TRADING_MONGO_DB"] = "trading_bot_pytest"
    env.pop("DEPLOY_SKIP_CYCLE_CHECK", None)
    env.update(env_extra)
    return subprocess.run(
        cmd, input=PAYLOAD, capture_output=True, text=True, timeout=60, env=env
    )


HOOKS = [
    pytest.param(["python3", str(GUARD_DEPLOY)], id="guard_deploy"),
    pytest.param(["bash", str(GUARD_CYCLE_SH)], id="guard_deploy_during_cycle"),
]


@pytest.mark.parametrize("hook", HOOKS)
def test_a_live_cycle_blocks_the_deploy(hook, state):
    """The thing the hooks exist for."""
    state("running")
    result = _run(hook, {})
    assert result.returncode == 2, (
        f"a RUNNING cycle did not block the deploy (exit {result.returncode}). "
        f"stdout={result.stdout!r} stderr={result.stderr[:300]!r}"
    )
    assert "cycle-TEST" in result.stderr or "RUNNING" in result.stderr.upper()


@pytest.mark.parametrize("hook", HOOKS)
@pytest.mark.parametrize("status", ["idle", "done", "error", "stopped", "interrupted"])
def test_an_idle_cycle_allows_the_deploy(hook, status, state):
    """Fail-closed must not mean "always closed" — an idle store allows."""
    state(status)
    result = _run(hook, {})
    assert result.returncode == 0, (
        f"status={status!r} blocked the deploy but is an idle state "
        f"(exit {result.returncode}): {result.stderr[:300]!r}"
    )


@pytest.mark.parametrize("hook", HOOKS)
def test_an_unreachable_store_blocks_rather_than_allows(hook, state):
    """THE REGRESSION. An unreadable check is not evidence of an idle cycle.

    Both hooks used to allow here — one by `warn()`, one by `unknown|` falling
    through the case statement.
    """
    state("running")   # a cycle IS live; the hook just cannot see it
    result = _run(hook, {"PRISM_MONGO_URI": UNREACHABLE, "MONGO_URI": UNREACHABLE})
    assert result.returncode == 2, (
        "an unreachable store PERMITTED the deploy — this is the fail-open "
        f"bug (exit {result.returncode}): {result.stdout[:200]!r}"
    )


@pytest.mark.parametrize("hook", HOOKS)
def test_the_override_is_explicit_and_still_works(hook, state):
    """A blocked deploy must have a deliberate way through, or it gets bypassed
    by disabling the hook entirely — which removes the guard permanently rather
    than for one run."""
    state("running")
    result = _run(hook, {
        "PRISM_MONGO_URI": UNREACHABLE, "MONGO_URI": UNREACHABLE,
        "DEPLOY_SKIP_CYCLE_CHECK": "1",
    })
    assert result.returncode == 0, (
        f"DEPLOY_SKIP_CYCLE_CHECK=1 did not override (exit {result.returncode})"
    )


@pytest.mark.parametrize("hook", HOOKS)
def test_a_non_deploy_command_is_not_touched(hook):
    """The gate must not block unrelated commands."""
    env = dict(os.environ)
    env["TRADING_MONGO_DB"] = "trading_bot_pytest"
    result = subprocess.run(
        hook, input=json.dumps({"tool_input": {"command": "ls -la"}}),
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0


def test_neither_hook_still_reads_postgres():
    """The probes must not import a Postgres driver.

    `psycopg2` was never installed, so its ImportError became `unknown|` on
    every call; `psycopg` is gone from the image entirely as of teardown. A
    probe that reaches for either is reading a store that is not there.
    """
    for path in (GUARD_DEPLOY,
                 REPO / ".claude" / "hooks" / "_check_cycle_running.py"):
        source = path.read_text(encoding="utf-8")
        code_lines = [
            ln for ln in source.splitlines()
            if ("psycopg" in ln and "import" in ln and not ln.lstrip().startswith("#"))
        ]
        assert not code_lines, (
            f"{path.name} still imports a Postgres driver: {code_lines}"
        )
        assert "pymongo" in source, f"{path.name} does not read Mongo"
