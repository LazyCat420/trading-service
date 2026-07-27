"""Invariants that must hold for ANY completed trading cycle.

Unit tests check that a function does what it was written to do. These check
that the cycle's own RECORD is internally consistent — the thing nobody was
checking when cycle-v3-1785107795 reported `collector_ok=49, collector_error=0,
collector_failures=[]` for a cycle in which every price provider failed.

Run against the live database when it is reachable (the audit use), and against
synthetic rows otherwise (the CI use), so the same invariants are enforced in
both places. The synthetic half never skips.

    python -m pytest tests/unit/test_cycle_invariants.py            # CI
    TRADING_BOT_LIVE_AUDIT=1 python -m pytest ... -k live           # audit
"""

from __future__ import annotations

import os

import pytest


# ── Pure invariant checks, no I/O ────────────────────────────────────────
#
# Each takes a summary dict shaped like cycle_run_summaries and returns a list
# of violations. Written as plain functions so the live audit and the
# synthetic tests run IDENTICAL logic — an invariant that only runs in one
# place is an invariant that will drift.


def _as_list(val) -> list:
    """Normalise a jsonb column that may arrive as list, str, or None.

    psycopg returns jsonb as a parsed list, but the same field arrives as a
    JSON STRING through summary_json and through the API. `len("[]")` is 2, so
    an un-normalised check reported "0 errors but 2 failures named" on every
    healthy cycle — a false positive on 15 of 15, which is how an invariant
    trains people to ignore it.
    """
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        import json

        try:
            parsed = json.loads(val)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def check_counts_reconcile(s: dict) -> list[str]:
    """Decisions cannot exceed tickers, and the buckets must sum."""
    bad = []
    n_tickers = len(s.get("tickers_final") or [])
    decisions = (s.get("buy_count", 0) + s.get("sell_count", 0)
                 + s.get("hold_count", 0))
    if n_tickers and decisions > n_tickers:
        bad.append(f"{decisions} decisions for {n_tickers} tickers")
    if s.get("analysis_results_count", 0) > n_tickers and n_tickers:
        bad.append(
            f"analysis_results_count={s['analysis_results_count']} > {n_tickers} tickers"
        )
    return bad


def check_trades_reconcile(s: dict) -> list[str]:
    """Executed + failed can never exceed attempted."""
    bad = []
    attempted = s.get("trade_attempted", 0) or 0
    executed = s.get("trade_executed", 0) or 0
    failed = s.get("trade_failed", 0) or 0
    if executed + failed > attempted:
        bad.append(f"executed({executed})+failed({failed}) > attempted({attempted})")
    if executed > (s.get("buy_count", 0) + s.get("sell_count", 0)):
        bad.append("more trades executed than BUY/SELL decisions were made")
    return bad


def check_failure_is_reported(s: dict) -> list[str]:
    """THE 2026-07-26 invariant.

    A cycle that lost data must not report a clean collector record. If
    anything failed, `collector_failures` must name it — an empty list beside
    a non-zero error count is the exact shape of the outage that read as
    healthy.
    """
    bad = []
    err = s.get("collector_error", 0) or 0
    failures = _as_list(s.get("collector_failures"))
    if err and not failures:
        bad.append(f"collector_error={err} but collector_failures is empty")
    if len(failures) != err:
        bad.append(f"collector_error={err} but {len(failures)} failure(s) named")
    return bad


def check_status_matches_evidence(s: dict) -> list[str]:
    """A 'done' cycle with no analysis is a failure wearing a success label."""
    bad = []
    if s.get("status") == "done":
        if s.get("tickers_final") and not s.get("analysis_results_count"):
            bad.append("status=done with tickers but zero analysis results")
        if s.get("primary_failure_reason"):
            bad.append(
                f"status=done but primary_failure_reason={s['primary_failure_reason']!r}"
            )
    return bad


ALL_CHECKS = (
    check_counts_reconcile,
    check_trades_reconcile,
    check_failure_is_reported,
    check_status_matches_evidence,
)


def violations(summary: dict) -> list[str]:
    out = []
    for check in ALL_CHECKS:
        out.extend(check(summary))
    return out


# ── Synthetic: these always run ──────────────────────────────────────────

def _healthy_summary(**over):
    s = {
        "status": "done",
        "tickers_final": ["AAA", "BBB", "CCC"],
        "collector_ok": 12, "collector_error": 0, "collector_skipped": 0,
        "collector_failures": [],
        "analysis_results_count": 3,
        "buy_count": 1, "sell_count": 0, "hold_count": 2,
        "trade_attempted": 1, "trade_executed": 1, "trade_failed": 0,
        "primary_failure_reason": None,
    }
    s.update(over)
    return s


class TestInvariantsAcceptHealthyCycles:
    def test_a_clean_cycle_has_no_violations(self):
        assert violations(_healthy_summary()) == []

    def test_an_all_hold_cycle_is_healthy(self):
        """No trades is a legitimate outcome — the desk self-selects HOLD
        about two thirds of the time."""
        s = _healthy_summary(buy_count=0, sell_count=0, hold_count=3,
                             trade_attempted=0, trade_executed=0)
        assert violations(s) == []

    def test_a_reported_failure_is_healthy_bookkeeping(self):
        s = _healthy_summary(collector_error=1,
                             collector_failures=["AAA:yfinance_price:error"])
        assert violations(s) == []


class TestJsonbNormalisation:
    """The invariant itself must not produce false positives.

    First live run flagged 15 of 15 healthy cycles with "0 errors but 2
    failures named" — `len("[]")` is 2. An invariant that cries wolf on every
    cycle is worse than no invariant, because it teaches people to skip it.
    """

    def test_empty_json_string_is_an_empty_list(self):
        assert _as_list("[]") == []
        assert check_failure_is_reported(_healthy_summary(collector_failures="[]")) == []

    def test_populated_json_string_is_counted_correctly(self):
        s = _healthy_summary(collector_error=1,
                             collector_failures='["AAA:yfinance_price:error"]')
        assert check_failure_is_reported(s) == []

    def test_none_and_garbage_do_not_raise(self):
        assert _as_list(None) == []
        assert _as_list("not json") == []
        assert _as_list(42) == []


class TestInvariantsCatchTheRealOutage:
    def test_the_2026_07_26_shape_is_rejected(self):
        """Every price provider failed for all 12 tickers, and the summary
        said collector_error=0 with an empty failure list. If this ever
        passes again, the telemetry has regressed."""
        s = _healthy_summary(collector_error=5, collector_failures=[])
        bad = violations(s)
        assert any("collector_failures is empty" in b for b in bad), bad

    def test_unnamed_failures_are_rejected(self):
        s = _healthy_summary(collector_error=3,
                             collector_failures=["AAA:yfinance_price:error"])
        assert any("failure(s) named" in b for b in violations(s))

    def test_more_decisions_than_tickers_is_rejected(self):
        s = _healthy_summary(hold_count=99)
        assert any("decisions for" in b for b in violations(s))

    def test_phantom_trades_are_rejected(self):
        s = _healthy_summary(trade_attempted=1, trade_executed=5)
        assert violations(s)

    def test_trades_without_directional_decisions_are_rejected(self):
        """An execution with no BUY or SELL behind it means the trade path
        ran on something the desk never decided."""
        s = _healthy_summary(buy_count=0, sell_count=0, hold_count=3,
                             trade_attempted=2, trade_executed=2)
        assert any("more trades executed" in b for b in violations(s))

    def test_done_with_no_analysis_is_rejected(self):
        s = _healthy_summary(analysis_results_count=0)
        assert any("zero analysis results" in b for b in violations(s))

    def test_done_carrying_a_failure_reason_is_rejected(self):
        s = _healthy_summary(primary_failure_reason="Cycle stopped/cancelled")
        assert any("primary_failure_reason" in b for b in violations(s))


# ── Live audit: opt-in, skips cleanly when the DB is unreachable ─────────

pytestmark_live = pytest.mark.skipif(
    not os.environ.get("TRADING_BOT_LIVE_AUDIT"),
    reason="live audit — set TRADING_BOT_LIVE_AUDIT=1 to check real cycles",
)


def _recent_summaries(limit=10):
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            """
            SELECT cycle_id, status, tickers_final, collector_ok, collector_error,
                   collector_skipped, collector_failures, analysis_results_count,
                   buy_count, sell_count, hold_count, trade_attempted,
                   trade_executed, trade_failed, primary_failure_reason
            FROM cycle_run_summaries
            WHERE status = 'done'
            ORDER BY finished_at DESC NULLS LAST LIMIT %s
            """,
            [limit],
        ).fetchall()

    cols = ("cycle_id", "status", "tickers_final", "collector_ok", "collector_error",
            "collector_skipped", "collector_failures", "analysis_results_count",
            "buy_count", "sell_count", "hold_count", "trade_attempted",
            "trade_executed", "trade_failed", "primary_failure_reason")
    return [dict(zip(cols, r)) for r in rows]


@pytestmark_live
class TestLiveCyclesUpholdInvariants:
    def test_recent_completed_cycles_are_self_consistent(self):
        summaries = _recent_summaries()
        if not summaries:
            pytest.skip("no completed cycles on record")

        failures = {
            s["cycle_id"]: violations(s) for s in summaries if violations(s)
        }
        assert not failures, f"invariant violations: {failures}"

    def test_no_live_outcome_is_scored_at_zero_confidence(self):
        """Pipeline crashes must not re-enter decision_outcomes as trades."""
        from app.db.connection import get_db

        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM decision_outcomes "
                "WHERE confidence = 0 AND outcome IN ('WIN','LOSS','FLAT')"
            ).fetchone()
        assert row[0] == 0, f"{row[0]} crash artifact(s) scored as trades"

    def test_no_analysis_price_of_zero_when_prices_exist(self):
        """analysis_price feeds the Freshness Gate's next-cycle delta. A zero
        baseline beside real price_history rows is the NaN-bar bug."""
        from app.db.connection import get_db

        with get_db() as db:
            row = db.execute(
                """
                SELECT COUNT(*) FROM analysis_results ar
                WHERE ar.analysis_price = 0
                  AND ar.created_at > NOW() - INTERVAL '2 days'
                  AND EXISTS (SELECT 1 FROM price_history p WHERE p.ticker = ar.ticker)
                """
            ).fetchone()
        assert row[0] == 0, f"{row[0]} zero-price snapshot(s) despite stored prices"
