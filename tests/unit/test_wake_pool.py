"""The wake pool — a candidate list for a name the desk already OWNS.

MEASURED 2026-08-12 over 149 desks: 31 of 33 held desks reached the bear with
an EMPTY pool, so `substitute.read_substitute` recorded NOT_ASKED on 21 of the
23 that produced an artifact. The bear then won 0 of 26 held debates against 54
of 78 (69%) unheld. The substitute axis — `hold_reason`'s stated PRIMARY axis —
was unavailable on exactly the population where an exit is the decision.

These tests are about the CALL SITE as much as the callee. The 2026-08-08
lesson on this module's neighbour is that a correct helper wired into the wrong
place measures nothing, so the orchestrator wiring is asserted by AST here
rather than trusted.
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.v3.wake_pool import (
    MAX_TICKERS,
    build_wake_pool,
    build_wake_pool_block,
)

_ORCH = Path(__file__).resolve().parents[2] / "app" / "v3" / "orchestrator.py"


def _row(cycle_id, tickers, *, age_hours=3.0):
    ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return (cycle_id, ts,
            {"cycle_metadata": {"cycle_candidate_tickers": list(tickers)}})


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return self

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _with_rows(rows):
    return patch("app.db.connection.get_db", return_value=_FakeDB(rows))


# ── The pool itself ──────────────────────────────────────────────────────

def test_the_most_recent_full_cycle_supplies_the_pool():
    with _with_rows([_row("cycle-a", ["MSFT", "GOOG", "AMZN"])]):
        rec = build_wake_pool("NVDA")
    assert rec["tickers"] == ["MSFT", "GOOG", "AMZN"]
    assert rec["cycle_id"] == "cycle-a"
    assert rec["reason"] == "ok"


def test_the_name_being_relooked_is_excluded():
    """A "substitute" that is the position itself is not an answer, and
    `substitute.read_substitute` would reject it as OFF_POOL — a rejection the
    bear can neither see nor fix."""
    with _with_rows([_row("cycle-a", ["NVDA", "MSFT", "nvda"])]):
        rec = build_wake_pool("nvda")
    assert rec["tickers"] == ["MSFT"]


def test_a_cycle_with_no_pool_is_skipped_not_returned_empty():
    """Wakes outnumber full cycles, so the newest row is usually pool-less.
    Stopping at the first row would make this feature almost never fire."""
    rows = [_row("wake-1", []), _row("wake-2", []),
            _row("cycle-full", ["KO", "PEP"])]
    with _with_rows(rows):
        rec = build_wake_pool("NVDA")
    assert rec["tickers"] == ["KO", "PEP"]
    assert rec["cycle_id"] == "cycle-full"


def test_the_pool_is_capped():
    with _with_rows([_row("c", [f"T{i}" for i in range(40)])]):
        rec = build_wake_pool("NVDA")
    assert len(rec["tickers"]) == MAX_TICKERS


@pytest.mark.parametrize("rows,reason", [
    ([], "no_recent_desks"),
    ([_row("wake-1", [])], "no_pool_in_window"),
])
def test_every_empty_outcome_names_its_reason(rows, reason):
    """`NOT_ASKED` pooled "no pool existed", "the pool was stale" and "the bear
    ignored the question" into one value, which is why this was invisible for
    four days. Every path states which one it was."""
    with _with_rows(rows):
        rec = build_wake_pool("NVDA")
    assert rec["tickers"] == []
    assert rec["reason"] == reason


def test_a_db_failure_is_non_fatal_and_named():
    with patch("app.db.connection.get_db", side_effect=RuntimeError("boom")):
        rec = build_wake_pool("NVDA")
    assert rec["tickers"] == []
    assert rec["reason"] == "lookup_failed"


def test_the_current_cycle_is_excluded_by_the_query():
    """Borrowing this cycle's own pool would be circular, and on a wake there
    is nothing to borrow anyway. Asserted on the SQL parameters, because the
    fake DB cannot enforce a WHERE clause."""
    captured = {}

    class _Spy(_FakeDB):
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return self

    with patch("app.db.connection.get_db", return_value=_Spy([])):
        build_wake_pool("NVDA", exclude_cycle_id="cycle-now")
    assert "cycle_id <> %s" in captured["sql"]
    assert "cycle-now" in captured["params"]


# ── The rendered block ───────────────────────────────────────────────────

def test_an_empty_pool_renders_nothing():
    """A header that says "here are the alternatives" and then lists none
    actively misleads — the same rule `build_candidate_block` follows."""
    assert build_wake_pool_block({"tickers": []}) == ""
    assert build_wake_pool_block({}) == ""


def test_the_block_says_the_names_are_from_a_PREVIOUS_cycle():
    block = build_wake_pool_block(
        {"tickers": ["MSFT", "KO"], "age_hours": 7.5}, self_ticker="NVDA")
    assert "LAST FULL CYCLE" in block
    assert "7.5h ago" in block
    assert "MSFT" in block and "KO" in block


def test_the_block_omits_screen_numbers():
    """`chg` and `rvol` are intraday; these rows are not. Re-rendering stale
    relative volume under a current-looking header is a freshness defect
    wearing a data table."""
    block = build_wake_pool_block({"tickers": ["MSFT"], "age_hours": 5})
    for stale in ("rvol", "chg%", "| screen |"):
        assert stale not in block


def test_the_block_asks_the_same_question_as_the_live_one():
    """Both populations must be answering the same question, or the comparison
    between them measures the prompt instead of the pool."""
    from app.v3.cycle_candidates import build_candidate_block

    live = build_candidate_block(
        [{"ticker": "MSFT", "score": 1, "chg": 1, "rvol": 1, "sector": "Tech"}],
        self_ticker="NVDA")
    wake = build_wake_pool_block({"tickers": ["MSFT"], "age_hours": 3},
                                 self_ticker="NVDA")
    ask = "only actionable on this book if it names something better"
    assert ask in live
    assert ask in wake


# ── The CALL SITE, which is where the 2026-08-08 defect actually lived ───

def _orchestrator_src():
    return _ORCH.read_text()


def test_the_wake_pool_is_wired_into_the_pipeline():
    src = _orchestrator_src()
    assert "build_wake_pool(" in src, (
        "the helper exists but nothing calls it — the exact shape that left "
        "the delta tier unlabelled for a week")


def test_the_wake_pool_only_fires_for_HELD_names():
    """Unheld pool-less desks are the CONTROL GROUP. If held NAMED/DECLINED
    moves and the unheld pool-less rate does not, the pool is what did it.
    Widening this to every wake buys coverage and loses the comparison."""
    src = _orchestrator_src()
    tree = ast.parse(src)
    guard_lines = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Compare)
        and "cycle_metadata" in ast.unparse(n)
        and "held" in ast.unparse(n)
        and ast.unparse(n).endswith("is True")
    ]
    assert guard_lines, "no `cycle_metadata.get('held') is True` guard found"


def test_the_wake_pool_does_not_overwrite_a_live_pool():
    """A cycle that ran discovery has a real, current pool. Replacing it with
    yesterday's names would be a downgrade, silently."""
    src = _orchestrator_src()
    assert "not desk.cycle_metadata.get(POOL_KEY)" in src, (
        "the wake pool must only fill an EMPTY pool")


def test_the_skip_reason_is_always_recorded():
    src = _orchestrator_src()
    assert 'desk.cycle_metadata["substitute_ask_skipped"]' in src
