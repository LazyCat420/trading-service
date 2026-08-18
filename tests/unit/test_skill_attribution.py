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

from unittest.mock import MagicMock, patch

from app.autoresearch import skill_loader as L
from app.autoresearch import skill_optimizer as S


def _skills(row):
    """Patch the loader's Mongo read to answer `agent_skills` with `row`.

    `mongo_query.find_row` returns a TUPLE in the requested column order — the
    loader asks for `['skill_text', 'version']` — so the fixtures are tuples,
    not documents. Dispatch is on the COLLECTION name so a read of anything
    else fails loudly rather than being handed a skill doc.
    """
    q = MagicMock()
    q.find_row.side_effect = lambda coll, *a, **k: (
        row if coll == "agent_skills" else None
    )
    return patch.object(L, "mongo_query", q)


def _skills_raise(exc_factory):
    """The loader's read blows up — an agent run must survive it."""
    q = MagicMock()
    q.find_row.side_effect = lambda *a, **k: exc_factory()
    return patch.object(L, "mongo_query", q)


# ── The loader must report the version it SERVED ────────────────────

def test_version_is_reported_alongside_the_prefix():
    L.invalidate_skill_cache()
    with _skills(("- **A**: Always cap size.", 7)):
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
    with _skills(("- **A**: Always cap size.", 7)):
        L.load_skill_prefix("v3_board_of_directors")
    # DB now advertises v8; the cache still holds v7 and must keep reporting it.
    with _skills(("- **A**: Always cap size.", 8)):
        assert L.active_skill_version("v3_board_of_directors") == 7
    L.invalidate_skill_cache()


def test_no_skill_doc_is_none_not_zero():
    """Absent and "version zero" are different claims; NULL must survive."""
    L.invalidate_skill_cache()
    with _skills(None):
        assert L.active_skill_version("v3_board_of_directors") is None
    L.invalidate_skill_cache()


def test_a_load_failure_never_raises():
    """An agent run must never block on skills."""
    L.invalidate_skill_cache()

    def _boom():
        raise RuntimeError("db down")

    with _skills_raise(_boom):
        assert L.load_skill_prefix("v3_board_of_directors") == ""
        assert L.active_skill_version("v3_board_of_directors") is None
    L.invalidate_skill_cache()


def test_version_snapshot_omits_agents_with_no_doc():
    L.invalidate_skill_cache()
    with _skills(None):
        assert L.active_skill_versions() == {}
    L.invalidate_skill_cache()


def test_version_snapshot_covers_the_target_roster():
    L.invalidate_skill_cache()
    with _skills(("- **A**: Always cap size.", 3)):
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
    q = MagicMock()
    # The count itself is what fails — an absent `skill_versions` field is the
    # Mongo shape of the missing column this test was written for.
    q.count.side_effect = RuntimeError("skill_versions is not indexed")
    with patch.object(S, "mongo_query", q):
        assert S._decisions_governed("v3_board_of_directors", 5) is None
    q.count.assert_called_once()
    assert q.count.call_args[0][0] == "decision_outcomes"


def test_maturity_threshold_exceeds_a_single_cycle():
    """The whole point: at ~7 decisions/cycle, a threshold at or below that
    permits one edit per cycle and reproduces the 20-versions-in-5-days churn
    that made every version unmeasurable.

    The threshold moved to scorecard.MATURITY_N when the gate stopped being a
    raw sample count. It also went UP: bootstrapping 1500 real resolved
    decisions put the 95% noise band at ±0.207 for n=25, so the old threshold
    sat inside its own noise and a comparison there decides nothing.
    """
    from app.autoresearch.scorecard import MATURITY_N, REGRESSION_MARGIN

    assert MATURITY_N > 7
    assert MATURITY_N >= S._SUPERSEDED_MIN_DECISIONS_BEFORE_REEDIT
    assert 0 < REGRESSION_MARGIN < 0.2


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

    from app.autoresearch.scorecard import VERDICT_IMMATURE, VersionScorecard

    immature = VersionScorecard(
        agent_name="v3_bull_agent", version=4, n_governed=3,
        verdict=VERDICT_IMMATURE, detail="3/100 resolved decisions",
    )
    called = AsyncMock()
    with patch.object(S, "_load_skill", return_value=("old doc", 4)), \
         patch.object(S, "_decisions_governed", return_value=3), \
         patch.object(S, "regression_verdict", return_value=immature), \
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

    from app.autoresearch.scorecard import (
        MATURITY_N, VERDICT_HEALTHY, VersionScorecard,
    )

    healthy = VersionScorecard(
        agent_name="v3_bull_agent", version=4, combined=0.6,
        n_governed=MATURITY_N, verdict=VERDICT_HEALTHY,
    )
    proposal = {"action": "REPLACE", "rationale": "x",
                "updated_skill": "- **New**: Always veto entries below a 1.2 to 1 reward ratio."}
    with patch.object(S, "_load_skill", return_value=("- **Old**: Always cap size at 5%.", 4)), \
         patch.object(S, "_decisions_governed", return_value=MATURITY_N), \
         patch.object(S, "regression_verdict", return_value=healthy), \
         patch.object(S, "_call_optimizer_llm", new=AsyncMock(return_value=proposal)), \
         patch.object(S, "_save_skill"):
        out = asyncio.run(
            S._optimize_one_agent("v3_bull_agent", "role", {}, "cyc-1", 0.55)
        )
    assert out != "immature", "a matured version was still held"
