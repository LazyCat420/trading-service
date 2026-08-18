"""A trade the policy gate refused must not be scored as one the desk kept.

The synthesizer's veto was made measurable by `overridden_from` (see
test_override_measurement). The POLICY gate's veto was not, and the reason is
subtle: on a policy block, `shared_desk.final_decision.action` and
`analysis_results.action` BOTH still read 'BUY' — the refusal lives only in
`trade_results.policy_action` — so the existing `board_action != action` test
is false and the row lands unlabelled.

Consequence, measured 2026-07-31 across 17 BUY + 2 SELL blocks: every one sat
in `override_scorecard`'s `kept_buys` bucket, crediting the desk with keeping
trades the confidence floor had actually refused, and each was graded WIN or
LOSS as though it had been taken.

The row deliberately keeps `action='BUY'`. Its P&L is the counterfactual —
what the declined trade would have returned — and that is exactly how the
floor gets back-tested. What changes is that it is now distinguishable.

These tests call `resolve_overridden_from` — the function the recorder itself
uses. An earlier version of this file re-implemented the branch logic inline
and asserted against the copy, so it could not see production diverge from it,
and it did: `_resolve("BUY", None) is None` was asserted as correct, which is
exactly the shape a block with no `trade_results` row takes. Eight of the 25
blocks on record had no such row and were labelled NULL.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.autoresearch import outcome_tracker as ot
from app.autoresearch.outcome_tracker import resolve_overridden_from


class _Db:
    """Stand-in answering the three lookups the resolver makes, by COLLECTION.

    Keyed by collection rather than by call order so that a test states what
    the database holds, not how many times the resolver reads it. The reads go
    through `mongo_query.find_row`, which returns a TUPLE in the requested
    column order — `shared_desk` is asked for `desk_data`, a nested document,
    so the board action is wrapped the way production stores it.

    `db_unused` is still threaded through `resolve_overridden_from`'s signature;
    the instance doubles as that argument so the wiring stays visible.
    """

    def __init__(self, board_action=None, policy_action=None, guardrail=False,
                 raises: set[str] | None = None):
        self.rows = {
            "shared_desk": (
                ({"final_decision": {"action": board_action}},)
                if board_action is not None else None
            ),
            "trade_results": (policy_action,) if policy_action is not None else None,
            "v3_guardrail_firings": (1,) if guardrail else None,
        }
        self.raises = raises or set()
        self.seen: list[str] = []

    def find_row(self, collection, *a, **k):
        self.seen.append(collection)
        if collection in self.raises:
            raise RuntimeError(f"{collection} unavailable")
        return self.rows[collection]

    @contextmanager
    def patched(self):
        q = MagicMock()
        q.find_row.side_effect = self.find_row
        with patch.object(ot, "mongo_query", q):
            yield self


def test_a_policy_blocked_buy_is_labelled():
    # Both sources read BUY — this is the case the board check cannot see.
    db = _Db(board_action="BUY", policy_action="HOLD_POLICY_BLOCKED_LOW_CONFIDENCE")
    with db.patched():
        assert resolve_overridden_from(db, "c1", "FCF", "BUY") == "BUY"


def test_a_block_with_no_trade_results_row_is_still_labelled():
    """BLK, cycle-v3-1785991713: the guardrail fired, `trade_results` had no
    row, and the row landed NULL — indistinguishable from a trade the gate
    allowed. Eight of 25 blocks took this shape; five were already graded."""
    db = _Db(board_action="BUY", policy_action=None, guardrail=True)
    with db.patched():
        assert resolve_overridden_from(db, "cycle-v3-1785991713", "BLK", "BUY") == "BUY"


def test_an_allowed_buy_stays_unlabelled():
    db = _Db("BUY", "HOLD_NO_SIGNAL")
    with db.patched():
        assert resolve_overridden_from(db, "c", "T", "BUY") is None
    # No block in EITHER table — the only shape that may read as permission.
    db = _Db("BUY")
    with db.patched():
        assert resolve_overridden_from(db, "c", "T", "BUY") is None


def test_the_synthesizer_downgrade_still_wins_the_label():
    # AGX in cycle-v3-1785504601: board said BUY, surviving action was HOLD.
    # The board check must keep precedence over the gate check.
    db = _Db(board_action="BUY")
    with db.patched():
        assert resolve_overridden_from(db, "c", "AGX", "HOLD") == "BUY"
    assert "trade_results" not in db.seen


def test_a_block_on_a_sell_is_labelled_too():
    db = _Db("SELL", "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE")
    with db.patched():
        assert resolve_overridden_from(db, "c", "T", "SELL") == "SELL"


def test_either_source_alone_carries_the_block():
    """Neither lookup may be load-bearing on its own: an unreadable table must
    not turn a refusal into permission."""
    db = _Db(board_action="BUY", guardrail=True, raises={"trade_results"})
    with db.patched():
        assert resolve_overridden_from(db, "c", "T", "BUY") == "BUY"

    db = _Db(board_action="BUY", policy_action="HOLD_POLICY_BLOCKED_LOW_CONFIDENCE",
             raises={"v3_guardrail_firings"})
    with db.patched():
        assert resolve_overridden_from(db, "c", "T", "BUY") == "BUY"


def test_the_recorder_uses_this_resolver():
    """Pins the wiring. The bug this file guards is only caught if the
    recorder calls the function these tests exercise."""
    import inspect
    src = inspect.getsource(ot.record_cycle_decisions)
    assert "resolve_overridden_from(" in src


def test_scorecard_separates_blocked_from_kept():
    """The bucket ordering is the fix: action = overridden_from must be
    caught BEFORE the kept_buys arm, or blocked trades keep landing there.

    The bucketing used to be a SQL CASE and this test handed back its finished
    output — three pre-labelled group rows — so it asserted that a dict
    round-trips, never that a blocked row is classified as blocked. The
    bucketing is Python now (`override_scorecard` reads raw
    `decision_outcomes` docs via `mongo_store.find_docs`), so the fixture is
    raw rows and the classification itself is under test: a blocked BUY carries
    `action == overridden_from == 'BUY'`, which is ALSO the shape the
    `kept_buys` arm would swallow if the ordering regressed.
    """
    docs = (
        # 19 gate-blocked BUYs: action and overridden_from both 'BUY'.
        [{"action": "BUY", "overridden_from": "BUY", "pnl_pct": -1.5}] * 19
        # 500 kept BUYs, 480 of them resolved (20 still carry a NULL pnl).
        + [{"action": "BUY", "overridden_from": None, "pnl_pct": 3.2}] * 480
        + [{"action": "BUY", "overridden_from": None, "pnl_pct": None}] * 20
        # 40 synthesizer vetoes: the surviving action is HOLD.
        + [{"action": "HOLD", "overridden_from": "BUY", "pnl_pct": 1.1}] * 40
    )
    store = MagicMock()
    store.find_docs.side_effect = lambda coll, *a, **k: (
        docs if coll == "decision_outcomes" else []
    )
    with patch.object(ot, "mongo_store", store):
        out = ot.override_scorecard(days=30)

    assert out["blocked_by_gate"]["n"] == 19
    assert out["kept_buys"]["n"] == 500
    assert out["overridden_buys"]["n"] == 40
    # The gate's refusals must not be inside the kept bucket any more.
    assert out["blocked_by_gate"]["mean_pnl"] == -1.5
    # ...and the kept bucket keeps only its own, unresolved rows excluded
    # from the mean but not from the count.
    assert out["kept_buys"]["scored"] == 480
    assert out["kept_buys"]["mean_pnl"] == 3.2
    # And the veto verdict still works off the other two.
    assert "verdict" in out
