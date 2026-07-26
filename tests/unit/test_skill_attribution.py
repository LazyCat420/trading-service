"""Can we tell which skill version governed a trade, and does a version live
long enough to be judged?

Both questions were answerable "no" on 2026-07-25, and measurably so:

  - `agent_skills` carried 145 versions and `decision_outcomes` 2028 resolved
    rows. Rows joining the two: **ZERO**. `agent_skills.cycle_id` is the cycle
    that PRODUCED an edit, not the cycles that edit later governed.
  - The board agent took **20 versions in ~5 days** while outcomes need a 7-day
    horizon to resolve, so every version was replaced before a single one of its
    trades matured. n=0 per version, forever.

Until both are fixed the SkillOpt loop is unfalsifiable — it cannot be shown to
help OR to hurt, which is the state that lets a cost keep being paid. These
tests pin the two mechanisms that make the question askable.
"""
from __future__ import annotations

from unittest.mock import patch

from app.autoresearch import skill_loader as L
from app.autoresearch import skill_optimizer as S


def _fake_db(row):
    """Minimal get_db() stand-in: one fetchone() result."""
    class _Cur:
        def fetchone(self_inner):
            return row

        def fetchall(self_inner):
            return [row] if row else []

    class _DB:
        def execute(self_inner, *a, **k):
            return _Cur()

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

    return _DB()


# ── The loader must report the version it SERVED ────────────────────

def test_version_is_reported_alongside_the_prefix():
    L.invalidate_skill_cache()
    with patch("app.db.connection.get_db", lambda: _fake_db(("- **A**: Always cap size.", 7))):
        prefix = L.load_skill_prefix("v3_board_of_directors")
        version = L.active_skill_version("v3_board_of_directors")
    assert "Always cap size" in prefix
    assert version == 7
    L.invalidate_skill_cache()


def test_version_comes_from_the_same_cache_entry_as_the_prompt():
    """The recorded version must be the one the agent RAN under, not whatever
    is newest in the DB when someone later asks. Those differ: the optimizer can
    accept a new version mid-cycle while this process serves a cached older one
    for up to the TTL."""
    L.invalidate_skill_cache()
    with patch("app.db.connection.get_db", lambda: _fake_db(("- **A**: Always cap size.", 7))):
        L.load_skill_prefix("v3_board_of_directors")
    # DB now advertises v8; the cache still holds v7 and must keep reporting it.
    with patch("app.db.connection.get_db", lambda: _fake_db(("- **A**: Always cap size.", 8))):
        assert L.active_skill_version("v3_board_of_directors") == 7
    L.invalidate_skill_cache()


def test_no_skill_doc_is_none_not_zero():
    """Absent and "version zero" are different claims; NULL must survive."""
    L.invalidate_skill_cache()
    with patch("app.db.connection.get_db", lambda: _fake_db(None)):
        assert L.active_skill_version("v3_board_of_directors") is None
    L.invalidate_skill_cache()


def test_a_load_failure_never_raises():
    """An agent run must never block on skills."""
    L.invalidate_skill_cache()

    def _boom():
        raise RuntimeError("db down")

    with patch("app.db.connection.get_db", _boom):
        assert L.load_skill_prefix("v3_board_of_directors") == ""
        assert L.active_skill_version("v3_board_of_directors") is None
    L.invalidate_skill_cache()


def test_version_snapshot_omits_agents_with_no_doc():
    L.invalidate_skill_cache()
    with patch("app.db.connection.get_db", lambda: _fake_db(None)):
        assert L.active_skill_versions() == {}
    L.invalidate_skill_cache()


def test_version_snapshot_covers_the_target_roster():
    L.invalidate_skill_cache()
    with patch("app.db.connection.get_db", lambda: _fake_db(("- **A**: Always cap size.", 3))):
        snap = L.active_skill_versions()
    assert set(snap) == set(S.TARGET_AGENTS), "snapshot must cover every target agent"
    assert all(v == 3 for v in snap.values())
    L.invalidate_skill_cache()


# ── A version must mature before it is replaced ─────────────────────

def test_unknown_governed_count_does_not_freeze_the_agent():
    """None means "cannot tell" — most likely a deployment predating the
    skill_versions column. Treating unknown as 0 would freeze every agent
    forever, which is worse than one extra edit."""
    assert S._decisions_governed("v3_board_of_directors", 0) is None


def test_governed_count_returns_none_when_the_column_is_missing():
    def _boom():
        raise RuntimeError('column "skill_versions" does not exist')

    with patch("app.autoresearch.skill_optimizer.get_db", _boom):
        assert S._decisions_governed("v3_board_of_directors", 5) is None


def test_maturity_threshold_exceeds_a_single_cycle():
    """The whole point: at ~7 decisions/cycle, a threshold at or below that
    permits one edit per cycle and reproduces the 20-versions-in-5-days churn
    that made every version unmeasurable."""
    assert S.MIN_DECISIONS_BEFORE_REEDIT > 7


def test_immature_is_counted_separately_from_skipped():
    """A held version is the system working as designed, not a failed proposal.
    Rolled into `skipped` it would read as the loop having stalled."""
    import inspect

    src = inspect.getsource(S.propose_and_validate_skill_edits)
    assert '"immature"' in src, "immature outcome is not surfaced in the summary"


# ── The maturity gate must hold, but must never freeze ──────────────

def test_an_immature_version_is_held():
    """A version that has governed too few resolved decisions is not replaced —
    and the LLM call is skipped, since a proposal that cannot be evaluated is
    not worth paying for."""
    import asyncio
    from unittest.mock import AsyncMock

    called = AsyncMock()
    with patch.object(S, "_load_skill", return_value=("old doc", 4)), \
         patch.object(S, "_decisions_governed", return_value=3), \
         patch.object(S, "_call_optimizer_llm", new=called):
        out = asyncio.run(
            S._optimize_one_agent("v3_bull_agent", "role", {}, "cyc-1", 0.55)
        )
    assert out == "immature"
    called.assert_not_awaited(), "an immature agent should not cost an LLM call"


def test_a_mature_version_is_eligible_again():
    """The complement — the gate must let go once the sample exists, or the
    loop stops learning entirely."""
    import asyncio
    from unittest.mock import AsyncMock

    proposal = {"action": "REPLACE", "rationale": "x",
                "updated_skill": "- **New**: Always veto entries below a 1.2 to 1 reward ratio."}
    with patch.object(S, "_load_skill", return_value=("- **Old**: Always cap size at 5%.", 4)), \
         patch.object(S, "_decisions_governed", return_value=S.MIN_DECISIONS_BEFORE_REEDIT), \
         patch.object(S, "_call_optimizer_llm", new=AsyncMock(return_value=proposal)), \
         patch.object(S, "_save_skill"):
        out = asyncio.run(
            S._optimize_one_agent("v3_bull_agent", "role", {}, "cyc-1", 0.55)
        )
    assert out != "immature", "a matured version was still held"
