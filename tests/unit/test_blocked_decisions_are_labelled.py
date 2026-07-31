"""A trade the policy gate refused must not be scored as one the desk kept.

The synthesizer's veto was made measurable by `overridden_from` (see
test_override_measurement). The POLICY gate's veto was not, and the reason is
subtle: on a policy block, `shared_desk.final_decision.action` and
`analysis_results.action` BOTH still read 'BUY' — the refusal lives only in
`trade_results.policy_action` — so the existing
`board_action != action` test is false and the row lands unlabelled.

Consequence, measured 2026-07-31 across 17 BUY + 2 SELL blocks: every one sat
in `override_scorecard`'s `kept_buys` bucket, crediting the desk with keeping
trades the confidence floor had actually refused, and each was graded WIN or
LOSS as though it had been taken.

The row deliberately keeps `action='BUY'`. Its P&L is the counterfactual —
what the declined trade would have returned — and that is exactly how the
floor gets back-tested. What changes is that it is now distinguishable.
"""

from unittest.mock import patch

from app.autoresearch import outcome_tracker as ot


class _Db:
    """Minimal stand-in: answers the desk lookup, then the policy lookup."""

    def __init__(self, board_action, policy_action):
        self._answers = [(board_action,), (policy_action,)]
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        self._last = self._answers.pop(0) if self._answers else None
        return self

    def fetchone(self):
        return self._last


def _resolve(board_action, policy_action, action="BUY"):
    """Run just the provenance logic the recorder uses, on a fake DB."""
    db = _Db(board_action, policy_action)
    overridden_from = None
    desk_row = db.execute("select final_decision", None).fetchone()
    board = desk_row[0] if desk_row else None
    if board and board != action:
        overridden_from = board
    if overridden_from is None:
        gate_row = db.execute("select policy_action", None).fetchone()
        pa = gate_row[0] if gate_row else None
        if pa and pa.startswith("HOLD_POLICY_BLOCKED"):
            overridden_from = action
    return overridden_from


def test_a_policy_blocked_buy_is_labelled():
    # Both sources read BUY — this is the case the old check could not see.
    assert _resolve("BUY", "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE") == "BUY"


def test_an_allowed_buy_stays_unlabelled():
    assert _resolve("BUY", "HOLD_NO_SIGNAL") is None
    assert _resolve("BUY", None) is None


def test_the_synthesizer_downgrade_still_wins_the_label():
    # AGX in cycle-v3-1785504601: board said BUY, surviving action was HOLD.
    # The existing path must keep precedence over the new gate check.
    assert _resolve("BUY", None, action="HOLD") == "BUY"


def test_a_block_on_a_sell_is_labelled_too():
    assert _resolve("SELL", "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE", action="SELL") == "SELL"


def test_scorecard_separates_blocked_from_kept():
    """The bucket ordering is the fix: action = overridden_from must be
    caught BEFORE the kept_buys arm, or blocked trades keep landing there."""
    rows = [
        ("blocked_by_gate", 19, 19, -1.5),
        ("kept_buys", 500, 480, 3.2),
        ("overridden_buys", 40, 40, 1.1),
    ]
    with patch.object(ot, "get_db") as gd:
        db = gd.return_value.__enter__.return_value
        db.execute.return_value.fetchall.return_value = rows
        out = ot.override_scorecard(days=30)

    assert out["blocked_by_gate"]["n"] == 19
    assert out["kept_buys"]["n"] == 500
    # The gate's refusals must not be inside the kept bucket any more.
    assert out["blocked_by_gate"]["mean_pnl"] == -1.5
    # And the veto verdict still works off the other two.
    assert "verdict" in out
