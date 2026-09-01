"""A failed technical baseline must not silently disarm the stale-price gate.

THE REGRESSION. 22c95d8 made the per-ticker context builders concurrent and,
in doing so, folded the staleness probe INTO `build_technical_baseline_block`'s
try/except:

    try:
        tech_block = await wait_for(build_technical_baseline_block, 15)   # (1)
        ...
        _b = await wait_for(compute_technical_baseline, 10)               # (2)
        ... stamp stale_price_age_trading_days ...
    except Exception:
        log("technical baseline failed (non-fatal)")

so any failure OR 15s timeout in (1) skipped (2) entirely. `_apply_policy_gates`
reads an absent `stale_price_age_trading_days` as FRESH (its own comment says
"absent means fresh (or detection failed …)"), so
HOLD_POLICY_BLOCKED_STALE_PRICE_DATA stopped firing precisely when the price
data was degraded — the situation the gate exists for. The old code ran the
probe in its own try for exactly this reason, and its dedicated
"staleness detection skipped" log line disappeared with it, so the failure was
also invisible.

Why the gate matters: cycle-v3-1785504601 shadow-fired on RBLX at 10 trading
days stale, and that desk emitted a 75-confidence thesis with a stop-loss ABOVE
the real spot, priced 24% off.

WHAT THIS FILE PINS. (1) the two probes live in SIBLING try blocks, checked by
parsing the source rather than by driving the pipeline (no test drives the
gathered path — `test_desk_stall_invariant.py` says so itself); (2) a failed
probe now RECORDS that the age is unknown instead of leaving it
indistinguishable from fresh; (3) the gate's fail-open policy is UNCHANGED —
pinned here so that promoting it to a hard block is a deliberate edit to a
test, not a silent drift.
"""

import ast
import pathlib

import pytest

ORCH = pathlib.Path(__file__).resolve().parents[2] / "app" / "v3" / "orchestrator.py"


def _technical_task_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(ORCH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_technical_task":
            return node
    raise AssertionError("_build_technical_task not found — did the builder get renamed?")


def _try_containing(node: ast.AST, callee: str) -> ast.Try | None:
    """The innermost Try whose BODY contains a call to `callee`."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Try):
            continue
        for stmt in sub.body:
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Name) and inner.id == callee:
                    return sub
                if isinstance(inner, ast.Attribute) and inner.attr == callee:
                    return sub
    return None


class TestTheTwoProbesAreIndependent:
    def test_both_probes_are_still_present(self):
        """A guard that cannot find its subject passes for the wrong reason."""
        task = _technical_task_node()

        assert _try_containing(task, "build_technical_baseline_block") is not None
        assert _try_containing(task, "compute_technical_baseline") is not None

    def test_a_failed_baseline_block_cannot_skip_staleness_detection(self):
        """The assertion that fails on 22c95d8..bed708d."""
        task = _technical_task_node()

        block_try = _try_containing(task, "build_technical_baseline_block")
        stale_try = _try_containing(task, "compute_technical_baseline")

        assert block_try is not stale_try, (
            "the staleness probe shares a try/except with the technical baseline "
            "block, so a baseline failure or 15s timeout skips detection and the "
            "stale-price gate reads 'absent' as 'fresh' — fail-open in exactly "
            "the case the gate exists for"
        )

    def test_the_staleness_failure_is_recorded_not_swallowed(self):
        """'Unknown age' and 'fresh' must be distinguishable on the desk."""
        task = _technical_task_node()
        stale_try = _try_containing(task, "compute_technical_baseline")
        handler_src = "\n".join(
            ast.unparse(h) for h in stale_try.handlers
        )

        assert "stale_price_detection_failed" in handler_src
        assert "staleness detection skipped" in handler_src


# ── The gate itself: behaviour, not source ────────────────────────────────
def _desk_with(**metadata):
    from app.v3.shared_desk import SharedDesk

    desk = SharedDesk(cycle_id="cycle-test", ticker="TEST")
    desk.final_decision = {
        "action": "BUY",
        "confidence": 88,
        "conviction_vector": {"data_quality": 90},
    }
    desk.trade_decision = dict(desk.final_decision)
    desk.cycle_metadata.update({"held": False, **metadata})
    return desk


class TestTheGateStillWorks:
    """Positive control: the gate must be able to fire, or the tests above
    are pinning the plumbing of something already dead."""

    def test_a_stale_age_blocks_a_buy(self):
        from app.v3.orchestrator import _apply_policy_gates

        desk = _desk_with(stale_price_age_trading_days=10, stale_price_as_of="2026-08-18")

        assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_STALE_PRICE_DATA"

    def test_a_float_age_blocks_too(self):
        """A float age disarmed the guard at BOTH ends once; _stale_age fixed it."""
        from app.v3.orchestrator import _apply_policy_gates

        desk = _desk_with(stale_price_age_trading_days=10.0)

        assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_STALE_PRICE_DATA"

    def test_a_fresh_age_does_not_block(self):
        from app.v3.orchestrator import _apply_policy_gates

        desk = _desk_with(stale_price_age_trading_days=1)

        assert _apply_policy_gates(desk) != "HOLD_POLICY_BLOCKED_STALE_PRICE_DATA"


class TestFailOpenIsStillThePolicy:
    """Unchanged since 2026-07-31, and pinned so a change is deliberate.

    An unreadable baseline must not block the cycle; the executor-side position
    and price checks are the backstop. The fix above makes the failure VISIBLE
    (a desk stamp plus a shadow guardrail firing) without changing what trades.
    Promoting this to a hard block is an operator decision — and it starts by
    editing this test.
    """

    def test_a_failed_detection_does_not_block(self):
        from app.v3.orchestrator import _apply_policy_gates

        desk = _desk_with(stale_price_detection_failed="TimeoutError: ")

        assert _apply_policy_gates(desk) != "HOLD_POLICY_BLOCKED_STALE_PRICE_DATA"

    def test_an_absent_age_does_not_block(self):
        from app.v3.orchestrator import _apply_policy_gates

        desk = _desk_with()

        assert _apply_policy_gates(desk) != "HOLD_POLICY_BLOCKED_STALE_PRICE_DATA"
