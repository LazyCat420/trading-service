"""Regression tests for the 2026-07-16 issues-only audit wave.

Covers:
- no_trade_reason bucketing in cycle summaries (policy gates / breaker /
  watch-only / confidence were previously indistinguishable from HOLD)
- ticker-scoped whiteboard subscriptions (bus used to fan every event out
  to every concurrent ticker's subscriber)
- SharedDesk artifact tag harvesting + persistence round-trip
- <thought_process> stripping in _parse_artifact (braces inside the block
  used to poison the first-{/last-} extraction)
"""
import asyncio

from app.services.pipeline_service import (
    summarize_ticker_results,
    resolve_no_trade_reason,
    REASON_DRAWDOWN_BREAKER,
    TRADE_ERROR_PREFIX,
)
from app.agents.whiteboard import Whiteboard
from app.v3.shared_desk import SharedDesk
from app.v3.agent_runner import _parse_artifact


# ── summarize_ticker_results buckets ──────────────────────────────────────

def _result(action, reason=None, attempted=False, executed=False):
    r = {
        "action": action,
        "confidence": 70,
        "trade_attempted": attempted,
        "trade_executed": executed,
    }
    if reason:
        r["no_trade_reason"] = reason
    return r


def test_summary_buckets_blocked_reasons():
    results = [
        _result("BUY", reason="HOLD_POLICY_BLOCKED_JURY_VETO"),
        _result("BUY", reason="HOLD_POLICY_BLOCKED_UNMITIGATED_RISK"),
        _result("BUY", reason="DRAWDOWN_BREAKER", attempted=True),
        _result("BUY", reason="AGENT_SIZE_ZERO_WATCH_ONLY"),
        _result("SELL", reason="CONFIDENCE_BELOW_THRESHOLD"),
        _result("SELL", reason="TRADE_ERROR: no position", attempted=True),
        _result("BUY", attempted=True, executed=True),
        _result("HOLD"),
    ]
    s = summarize_ticker_results(results)
    assert s["policy_blocked"] == 2
    assert s["breaker_blocked"] == 1
    assert s["watch_only"] == 1
    assert s["confidence_blocked"] == 1
    assert s["trade_errors"] == 1
    # action counts unchanged: blocked BUYs still count as BUY decisions
    assert s["buy_count"] == 5
    assert s["sell_count"] == 2
    assert s["hold_count"] == 1
    assert s["trade_executed"] == 1


def test_summary_buckets_absent_when_clean():
    s = summarize_ticker_results([_result("HOLD"), _result("BUY", attempted=True, executed=True)])
    assert s["policy_blocked"] == 0
    assert s["breaker_blocked"] == 0
    assert s["watch_only"] == 0
    assert s["trade_errors"] == 0


def test_resolve_no_trade_reason_discriminates_breaker():
    breaker = {"error": "Portfolio drawdown breaker: ...", "reason_code": "DRAWDOWN_BREAKER", "drawdown_pct": -26.0}
    assert resolve_no_trade_reason(breaker) == REASON_DRAWDOWN_BREAKER
    # Legacy shape without reason_code still classifies via drawdown_pct
    assert resolve_no_trade_reason({"error": "x", "drawdown_pct": -30}) == REASON_DRAWDOWN_BREAKER
    generic = resolve_no_trade_reason({"error": "Insufficient funds"})
    assert generic.startswith(TRADE_ERROR_PREFIX) and "Insufficient funds" in generic


# ── ticker-scoped whiteboard subscriptions ────────────────────────────────

def test_subscriber_ticker_scoping():
    wb = Whiteboard()
    seen_aapl, seen_all = [], []

    async def aapl_cb(event):
        seen_aapl.append(event)

    def global_cb(event):
        seen_all.append(event)

    wb.subscribe(aapl_cb, ticker="aapl")   # lowercase on purpose — normalized
    wb.subscribe(global_cb)                 # unscoped: sees everything

    async def run():
        await wb._notify_subscribers({"type": "whiteboard_update", "ticker": "AAPL", "cycle_id": "c1"})
        await wb._notify_subscribers({"type": "whiteboard_update", "ticker": "NVDA", "cycle_id": "c1"})

    asyncio.run(run())
    assert len(seen_aapl) == 1 and seen_aapl[0]["ticker"] == "AAPL"
    assert len(seen_all) == 2

    wb.unsubscribe(aapl_cb)
    wb.unsubscribe(global_cb)
    assert wb._subscribers == []


def test_subscriber_exception_does_not_break_fanout():
    wb = Whiteboard()
    seen = []

    def bad_cb(event):
        raise RuntimeError("boom")

    def good_cb(event):
        seen.append(event)

    wb.subscribe(bad_cb, ticker="TSLA")
    wb.subscribe(good_cb, ticker="TSLA")
    asyncio.run(wb._notify_subscribers({"ticker": "TSLA"}))
    assert len(seen) == 1


def test_duplicate_subscribe_ignored():
    wb = Whiteboard()

    def cb(event):
        pass

    wb.subscribe(cb, ticker="AAPL")
    wb.subscribe(cb, ticker="AAPL")
    assert len(wb._subscribers) == 1


def test_bound_method_subscribers_dedup_and_unsubscribe():
    # Bound methods are == across accesses but never `is` — the bus must use
    # equality or these subscribers double-fire and leak forever.
    class Listener:
        def on_event(self, event):
            pass

    listener = Listener()
    wb = Whiteboard()
    wb.subscribe(listener.on_event, ticker="AAPL")
    wb.subscribe(listener.on_event, ticker="AAPL")
    assert len(wb._subscribers) == 1
    wb.unsubscribe(listener.on_event)
    assert wb._subscribers == []


# ── SharedDesk artifact tags ──────────────────────────────────────────────

def test_desk_harvests_and_normalizes_tags():
    desk = SharedDesk(cycle_id="c1", ticker="AAPL")
    desk.append_artifact("desk_note", {
        "summary": "notes",
        "tags": ["#catalyst", "Earnings Risk", "#catalyst", "", 42],
    })
    tags = desk.artifact_tags["desk_note"]
    assert "#catalyst" in tags
    assert "#earnings_risk" in tags
    assert len([t for t in tags if t == "#catalyst"]) == 1
    assert all(t.startswith("#") for t in tags)


def test_desk_tags_roundtrip_and_render():
    desk = SharedDesk(cycle_id="c1", ticker="AAPL")
    desk.append_artifact("quant_report", {"summary": "q", "tags": ["#verify_later"]})

    restored = SharedDesk.from_dict(desk.to_dict())
    assert restored.artifact_tags == {"quant_report": ["#verify_later"]}
    assert "#verify_later" in restored.get_all_tags()

    ctx = desk.get_compressed_context()
    assert "Desk Tags" in ctx and "#verify_later" in ctx

    brief = desk.get_handoff_brief()
    assert "#verify_later" in brief


def test_desk_without_tags_unchanged():
    desk = SharedDesk(cycle_id="c1", ticker="AAPL")
    desk.append_artifact("desk_note", {"summary": "no tags here"})
    assert desk.artifact_tags == {}
    assert "Desk Tags" not in desk.get_compressed_context()


# ── thought_process stripping in _parse_artifact ──────────────────────────

def test_parse_artifact_strips_thought_process_with_braces():
    text = (
        "<thought_process>\n"
        "Considering {volatility: high} and {regime: unclear}...\n"
        "</thought_process>\n"
        '{"action": "BUY", "confidence": 72, "reasoning": "clean"}'
    )
    parsed = _parse_artifact(text, "final_decision", "board_of_directors")
    assert parsed == {"action": "BUY", "confidence": 72, "reasoning": "clean"}


def test_parse_artifact_plain_json_still_works():
    parsed = _parse_artifact('{"action": "HOLD"}', "final_decision", "board")
    assert parsed == {"action": "HOLD"}
