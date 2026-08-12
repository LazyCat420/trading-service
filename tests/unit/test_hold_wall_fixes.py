"""The four 2026-08-11 HOLD-wall fixes, each pinned to the defect it answers.

Measured before the fixes (chapters 53/54): 301 of 307 August decisions were
final HOLDs; 80.7% board-reasoned; debates judged with no bull defense went to
the bear 79% of the time against 50% with it; the Board overrode a bear win
0 times in 102; 41 named substitutes were written and never read back.

Every assertion here is written to fail if the corresponding mechanism is
removed — the AST tests exist because a callee-only test passes while the
feature is dead, which is the failure this repo has already paid for twice
(`test_substitute.py:6-11`).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (REPO / rel).read_text()


def _code_only(text: str) -> str:
    """Strip comments so an assertion cannot be satisfied by prose.

    The bull_defense branch documents `_check_abort` in a comment explaining
    why it is absent; a substring check against the raw text would read that
    comment as the call it forbids.
    """
    out = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        if stripped.strip():
            out.append(stripped)
    return "\n".join(out)


def _call_line(src: str, func_name: str, needle: str) -> int:
    """Line of the first real Call to `func_name` (never a comment or string)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name == func_name and needle in ast.unparse(node):
                return node.lineno
    return -1


# ── FIX 1 — the bull defense retries before the debate is conceded ──────────

def test_defense_retry_precedes_the_fail_open():
    """A failed defense must be re-queued before the judge is chained.

    The fail-open itself stays (a stranded desk is strictly worse than an
    incomplete debate) — what must not survive is reaching it on the FIRST
    miss, which silently restored the pre-fix two-turn debate for 18% of desks.
    """
    src = _src("app/v3/orchestrator.py")
    branch = src.split('elif name == "bull_defense":', 1)[1]
    branch = branch.split('elif name == "debate_judge":', 1)[0]

    assert "DEFENSE_MAX_ATTEMPTS" in branch, (
        "the defense branch no longer bounds its attempts")
    retry_at = branch.find('_queue_agent("bull_defense"')
    concede_at = branch.find('_queue_agent("debate_judge"')
    assert retry_at != -1, "the defense is never retried — fail-open on first miss"
    assert concede_at != -1, "the fail-open was removed; a failed defense now strands the desk"
    assert retry_at < concede_at, (
        "the judge is chained before the retry — the retry can never run")


def test_defense_attempts_stay_within_the_run_counter_cap():
    """DEFENSE_MAX_ATTEMPTS must be reachable under MAX_RUNS_PER_AGENT.

    `_queue_agent` refuses at the cap, so a retry budget at or above it is a
    retry that silently never happens.
    """
    src = _src("app/v3/orchestrator.py")
    tree = ast.parse(src)
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in (
                    "MAX_RUNS_PER_AGENT", "DEFENSE_MAX_ATTEMPTS"
                ):
                    found[t.id] = node.value.value
    assert "DEFENSE_MAX_ATTEMPTS" in found, "the retry bound is gone"
    assert found["DEFENSE_MAX_ATTEMPTS"] < found["MAX_RUNS_PER_AGENT"], (
        f"defense retries ({found['DEFENSE_MAX_ATTEMPTS']}) cannot be queued "
        f"under the cap ({found['MAX_RUNS_PER_AGENT']})")


def test_the_defense_branch_still_never_aborts_the_desk():
    """A retried defense that fails again must not trip the circuit breaker.

    `_check_abort` on bull_defense would send the desk to ABORTED — trading a
    bear bias for no decision at all.
    """
    src = _src("app/v3/orchestrator.py")
    branch = src.split('elif name == "bull_defense":', 1)[1]
    branch = branch.split('elif name == "debate_judge":', 1)[0]
    assert "_check_abort" not in _code_only(branch), (
        "the defense branch now aborts the desk on failure")


# ── FIX 2 — the Board has to answer a bear win ──────────────────────────────

def test_board_prompt_confronts_the_bear_reflex_in_every_persona():
    from app.v3.agents.board_of_directors import PERSONA_MAP

    for regime, prompt in PERSONA_MAP.items():
        assert "WHEN THE BEAR WINS THE DEBATE" in prompt, regime
        assert "bear_verdict_response" in prompt, (
            f"{regime} never shows the field in its output example")


def test_bear_verdict_response_is_optional_not_required():
    """Requiring it would turn a non-answer into a degraded artifact — i.e.
    another HOLD, which is the defect being measured."""
    from app.v3.artifacts import FINAL_DECISION_SCHEMA as S

    assert "bear_verdict_response" in S["properties"]
    assert "bear_verdict_response" not in S["required"]
    props = S["properties"]["bear_verdict_response"]["properties"]
    assert set(props) == {"decisive_claim", "claim_type", "overrode_bear"}
    assert props["claim_type"]["enum"] == [
        "thesis_broken", "size_or_timing", "unproven"]


def test_an_incomplete_debate_is_labelled_for_the_board():
    """A verdict reached with no defense must not render identically to a
    contested one — the Board could not previously tell them apart."""
    from app.v3.shared_desk import SharedDesk

    def _ctx(with_defense: bool) -> str:
        desk = SharedDesk(desk_id="d", cycle_id="c", ticker="AAPL")
        desk.bull_argument = {"summary": "cheap", "confidence": 60}
        desk.bear_rebuttal = {"summary": "levered", "confidence": 70}
        desk.debate_judge = {"summary": "bear carried it", "winner": "bear",
                             "final_confidence": 66}
        if with_defense:
            desk.bull_defense = {"summary": "answered", "thesis_survives": True,
                                 "final_confidence": 62}
        return desk.get_compressed_context(include_debate=True)

    incomplete, contested = _ctx(False), _ctx(True)
    assert "INCOMPLETE DEBATE" in incomplete
    assert "unanswered by construction" in incomplete
    assert "INCOMPLETE DEBATE" not in contested, (
        "a complete debate is being labelled incomplete")


# ── FIX 3 — the named substitute reaches the next cycle ────────────────────

def test_only_named_substitutes_are_carried():
    """OFF_POOL names are unscored and unpriced; carrying one would admit a
    ticker no screen ever saw."""
    from app.v3 import substitute_demand as sd

    fn = ast.parse(inspect.getsource(sd.recent_substitute_demand)).body[0]
    if (isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]          # drop the docstring; prose is not code
    q = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))

    assert "NAMED" in q
    for bad in ("OFF_POOL", "UNANSWERED", "NOT_ASKED", "DECLINED"):
        assert bad not in q, f"{bad} is being carried into the pool"


def test_merge_into_pool_adds_only_missing_names():
    from app.v3.substitute_demand import merge_into_pool

    pool = {"AAPL": {"label": "Watchlist", "source_count": 0, "total_mentions": 0}}
    added = merge_into_pool(pool, {"AAPL": 3, "MA": 7})

    assert added == ["MA"], "an existing name's discovery evidence was overwritten"
    assert pool["AAPL"]["label"] == "Watchlist"
    assert pool["MA"] == {
        "label": "BearSubstitute", "source_count": 1, "total_mentions": 7}
    assert set(pool["MA"]) == set(pool["AAPL"]), (
        "carried entries must match the pool's shape or the scoring loop skips them")


def test_merge_into_pool_survives_junk():
    from app.v3.substitute_demand import merge_into_pool

    pool: dict = {}
    assert merge_into_pool(pool, {}) == []
    assert merge_into_pool(pool, None) == []
    assert pool == {}


def test_the_carry_is_wired_into_the_pool_before_selection():
    """The reader existing proves nothing — it has to run where `all_pool` is
    still open, because `admit_gatekeeper_selection` admits only from it."""
    src = _src("app/services/pipeline_service.py")

    call_at = _call_line(src, "recent_substitute_demand", "")
    merge_at = _call_line(src, "merge_into_pool", "all_pool")
    admit_at = _call_line(src, "admit_gatekeeper_selection", "all_pool")

    assert call_at != -1, "nothing reads the bear's named alternative back"
    assert merge_at != -1, "the demand is read and never merged into the pool"
    assert admit_at != -1, "the admission gate moved; re-verify this ordering"
    assert merge_at < admit_at, (
        f"substitutes merged at line {merge_at}, after admission at "
        f"{admit_at} — they could never be selected")
    assert "substitute_demand.get(t, 0)" in _code_only(src), (
        "carried names get no score bonus and cannot survive the top-20 cap")


def test_substitute_demand_is_declared_unconditionally():
    """An explicit-ticker run (Watch Desk wake) skips the discovery branch, so
    a conditional binding is an UnboundLocalError at the scoring loop."""
    src = _src("app/services/pipeline_service.py")
    fn_body = src.split("async def _run_all_v3", 1)[1].split("\n        # The cycle's cross-ticker", 1)
    assert "substitute_demand: dict[str, int] = {}" in src
    decl_at = src.find("substitute_demand: dict[str, int] = {}")
    use_at = src.find("substitute_demand.get(t, 0)")
    assert decl_at < use_at


# ── FIX 4 — the confidence shadow ──────────────────────────────────────────

def test_recalibration_is_monotone_and_clamped():
    from app.quant.confidence_calibration import empirical_win_rate as f

    xs = [40, 50, 55, 60, 65, 70, 75, 80, 85, 90, 99]
    ys = [f(x) for x in xs]
    assert all(b >= a for a, b in zip(ys, ys[1:])), f"map is not monotone: {ys}"
    assert f(10) == f(50), "extrapolates below the fitted range"
    assert f(120) == f(90), "extrapolates above the fitted range"
    assert f(72) is not None and 0.650 <= f(72) <= 0.917


def test_a_missing_confidence_does_not_become_a_number():
    from app.quant.confidence_calibration import empirical_win_rate, shadow_record

    assert empirical_win_rate(None) is None
    assert empirical_win_rate("N/A") is None
    assert empirical_win_rate(True) is None, "a bool is not a confidence"
    assert shadow_record(None, 70) is None


def test_shadow_compares_against_the_floors_own_fitted_value():
    """The floor of 70 is calibrated on the RAW scale. Comparing a fitted
    probability against 0.70 would silently impose a stricter, different bar."""
    from app.quant.confidence_calibration import shadow_record

    rec = shadow_record(70, 70)
    assert rec["would_clear_recalibrated"] is True
    assert rec["floor_equivalent"] == pytest.approx(0.650, abs=1e-6)
    assert rec["shadow_only"] is True
    assert shadow_record(65, 70)["would_clear_recalibrated"] is False
    assert shadow_record(80, 70)["would_clear_recalibrated"] is True


def test_the_shadow_reaches_both_decision_exits():
    """The delta tier returns ~1,600 lines above the full panel; a helper wired
    into one exit measures one route (the 2026-08-08 hold_reason lesson)."""
    src = _src("app/v3/orchestrator.py")
    calls = src.count("_attach_confidence_shadow(result, ticker=ticker)")
    assert calls == 2, f"expected both decision exits, found {calls}"


def test_the_shadow_gates_nothing():
    """If this ever gates, it violates the one-change-per-window rule that the
    confidence-rebuild design is built on."""
    gates = _src("app/v3/orchestrator.py").split("def _apply_policy_gates", 1)[1]
    gates = gates.split("\ndef ", 1)[0]
    for banned in ("confidence_shadow", "empirical_win_rate", "shadow_record"):
        assert banned not in gates, f"{banned} is being read by a policy gate"
