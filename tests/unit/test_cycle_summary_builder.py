"""Regression tests for cycle_run_summaries counting.

Bug history (2026-07-14, cycle-v3-1784026688): _process_ticker returned None,
so asyncio.gather produced [None, ...] and every count in the run summary
collapsed to 0 even though 3 decisions were persisted and 3 paper orders
executed. summarize_ticker_results now aggregates the returned result dicts.
"""
from app.services.pipeline_service import summarize_ticker_results


def _result(action, attempted=False, executed=False, failed=False):
    return {
        "action": action,
        "confidence": 75,
        "trade_attempted": attempted,
        "trade_executed": executed,
        "trade_failed": failed,
    }


def test_counts_actions_and_trades():
    results = [
        _result("BUY", attempted=True, executed=True),
        _result("SELL", attempted=True, executed=True),
        _result("SELL", attempted=True, failed=True),
        _result("HOLD"),
    ]
    s = summarize_ticker_results(results)
    assert s["analysis_results_count"] == 4
    assert s["buy_count"] == 1
    assert s["sell_count"] == 2
    assert s["hold_count"] == 1
    assert s["trade_attempted"] == 3
    assert s["trade_executed"] == 2
    assert s["trade_failed"] == 1


def test_ignores_none_and_exceptions():
    # gather(return_exceptions=True) mixes Nones (stopped tickers) and
    # Exceptions (crashed tickers) into the result list.
    results = [None, RuntimeError("boom"), _result("BUY")]
    s = summarize_ticker_results(results)
    assert s["analysis_results_count"] == 1
    assert s["buy_count"] == 1


def test_empty_and_none_input():
    assert summarize_ticker_results([])["analysis_results_count"] == 0
    assert summarize_ticker_results(None)["analysis_results_count"] == 0


def test_lowercase_actions_counted():
    s = summarize_ticker_results([_result("buy"), _result("hold")])
    assert s["buy_count"] == 1
    assert s["hold_count"] == 1


def test_missing_action_not_counted_as_hold():
    s = summarize_ticker_results([{"confidence": 10}])
    assert s["analysis_results_count"] == 1
    assert s["hold_count"] == 0
