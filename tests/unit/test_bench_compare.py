"""The bench regression bar — what it fails on, and what it refuses to fail on.

`compare_runs` is deliberately asymmetric: a contract PASS->FAIL is a hard
failure, a timing change usually is not. That asymmetry is the whole design and
it is what these tests pin.

THE BOX IS SHARED. Parallel sessions, a live trading cycle and the collectors
all move wall-clock by more than any change under test. Measured on this repo,
unit-test classes SHRINK on a busy box rather than fail. A benchmark that goes
red on a 20% timing move trains people to ignore it, which is strictly worse
than not having one — so timing may only fail a comparison when BOTH runs were
taken with no live cycle.
"""

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "scripts" / "bench_stage.py"


def _compare_runs():
    """Load the function without importing the module's heavy deps.

    The module must be registered in `sys.modules` BEFORE `exec_module`:
    `@dataclass` resolves its annotations through `sys.modules[cls.__module__]`,
    and on a module that is not registered that lookup returns None and raises
    `AttributeError: 'NoneType' object has no attribute '__dict__'`.
    """
    import sys

    spec = importlib.util.spec_from_file_location("_bench_stage_probe", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod.compare_runs


def _run(*stages, live=False):
    """Fixtures use the REAL key `run_stage` emits, not an invented one.

    The first version of this helper wrote `{"ok": bool}`. `run_stage` writes
    `{"status": "PASS"|"FAIL"}`. Every test below passed against the invented
    shape while `compare_runs` could not detect a single contract regression on
    a real baseline — a test that defines its own subject proves nothing.
    `test_the_status_key_matches_what_run_stage_actually_emits` pins the shape
    against the producer so this cannot happen again.
    """
    return {"ticker": "EXLS", "cycle_id": "c", "live_cycle": live,
            "results": [{"stage": n, "status": "PASS" if ok else "FAIL",
                         "median_s": t} for n, ok, t in stages]}


def test_a_contract_regression_fails():
    """PASS -> FAIL is a behavioural statement. It does not depend on load."""
    n = _compare_runs()(_run(("wake_pool", True, 1.0)),
                        _run(("wake_pool", False, 1.0)))
    assert n == 1


def test_a_fix_is_reported_and_does_not_fail():
    n = _compare_runs()(_run(("wake_pool", False, 1.0)),
                        _run(("wake_pool", True, 1.0)))
    assert n == 0


def test_a_narrower_run_is_not_a_regression():
    """`bench_stage wake_pool --compare full-baseline.json` is a normal thing
    to do. Failing it would make the flag unusable for the quick checks it
    exists for."""
    n = _compare_runs()(_run(("wake_pool", True, 1.0)), _run())
    assert n == 0


def test_a_new_stage_does_not_fail():
    n = _compare_runs()(_run(), _run(("wake_pool", True, 1.0)))
    assert n == 0


def test_a_huge_slowdown_fails_on_a_CLEAN_box():
    n = _compare_runs()(_run(("data_report", True, 1.0), live=False),
                        _run(("data_report", True, 9.0), live=False))
    assert n == 1


@pytest.mark.parametrize("base_live,now_live", [(True, False), (False, True), (True, True)])
def test_the_SAME_slowdown_cannot_fail_when_a_cycle_was_LIVE(base_live, now_live):
    """The load caveat, asserted rather than written in a docstring.

    This is the test that stops the benchmark becoming noise. A 9x number taken
    against a live cycle is a measurement of the cycle, not of the code.
    """
    n = _compare_runs()(_run(("data_report", True, 1.0), live=base_live),
                        _run(("data_report", True, 9.0), live=now_live))
    assert n == 0


def test_a_contract_regression_STILL_fails_on_a_loaded_box():
    """Load excuses timing. It does not excuse behaviour."""
    n = _compare_runs()(_run(("wake_pool", True, 1.0), live=True),
                        _run(("wake_pool", False, 1.0), live=True))
    assert n == 1


def test_a_modest_slowdown_does_not_fail_even_when_clean():
    """1.4x notes, 2.5x fails. A bar set at noise level is a bar nobody reads."""
    n = _compare_runs()(_run(("data_report", True, 1.0), live=False),
                        _run(("data_report", True, 1.5), live=False))
    assert n == 0


def test_a_zero_baseline_time_does_not_divide_by_zero():
    n = _compare_runs()(_run(("wake_pool", True, 0.0), live=False),
                        _run(("wake_pool", True, 5.0), live=False))
    assert n == 0


def test_the_status_key_matches_what_run_stage_actually_emits():
    """THE TEST THAT WOULD HAVE CAUGHT IT.

    Every test above builds its own fixture, so all of them passed while
    `compare_runs` read a key (`ok`) that no real row has — making the whole
    regression bar decorative. This one reads the PRODUCER: it parses
    `run_stage`'s result literal out of the source and asserts the field
    `compare_runs` depends on is really there.
    """
    import ast

    tree = ast.parse(_SRC.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "run_stage")
    emitted = {k.value for node in ast.walk(fn) if isinstance(node, ast.Dict)
               for k in node.keys if isinstance(k, ast.Constant)}

    assert "status" in emitted, (
        f"run_stage no longer emits 'status' — compare_runs reads it. Emitted: "
        f"{sorted(emitted)}")
    assert "median_s" in emitted, "run_stage no longer emits 'median_s'"
    assert "ok" not in emitted, (
        "run_stage now emits 'ok' as well as 'status' — pick ONE and make "
        "compare_runs read it; two spellings of the same fact is how the first "
        "version of this bar silently never fired")


def test_a_stage_failing_in_both_runs_is_not_reported_as_a_pass(capsys):
    """A green tick on a red stage is how a broken bar goes unnoticed."""
    _compare_runs()(_run(("wake_pool", False, 1.0)), _run(("wake_pool", False, 1.0)))
    out = capsys.readouterr().out
    assert "still failing" in out
    assert "✅ wake_pool" not in out


# ── Stage ORDER is decided by `group`, not by the command line ───────────

def test_a_readback_stage_must_not_sit_in_a_group_that_runs_before_agents():
    """`main` runs groups in a fixed order and filters by `Stage.group`:

        for group in ("context", "compute", "gate", "agent"):
            rows = [s for s in ordered if s.group == group]

    So a "gate" stage ALWAYS runs before every agent, whatever order the
    command line asks for. `substitute_ask` reads back what the bear left; it
    shipped in the gate group and on its first live run could only report
    `bear_ran=False` — it was structurally incapable of observing the thing it
    exists to observe.

    Pinned against the source so the next read-back stage does not repeat it.
    """
    import ast

    src = _SRC.read_text()
    tree = ast.parse(src)

    # The group order `main` actually iterates.
    orders = [
        [e.value for e in node.elts if isinstance(e, ast.Constant)]
        for node in ast.walk(tree)
        if isinstance(node, ast.Tuple)
        and [e.value for e in node.elts if isinstance(e, ast.Constant)][:1] == ["context"]
    ]
    assert orders, "could not find the group-order tuple in bench_stage.py"
    order = orders[0]
    assert order.index("agent") == len(order) - 1, (
        f"agents are no longer last in the run order ({order}) — re-check every "
        "stage that reads back an agent's output")

    # substitute_ask must be registered in the agent group.
    call = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Stage"
        and n.args and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "substitute_ask")
    group = call.args[1].value
    assert group == "agent", (
        f"substitute_ask is registered in group {group!r}. It reads back the "
        f"bear's answer, and group order is {order} — anything before 'agent' "
        "runs first and can only ever see bear_ran=False.")
