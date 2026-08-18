"""collector_stats: late (blew the 45s report deadline, still collecting) must
not be reported as failure. Folding the two together made every cold-ticker
cycle read as "15/15 collectors failed" when nothing had failed."""
from app.v3 import collector_stats


def test_late_collectors_are_not_errors():
    cid = "cycle-test-late"
    collector_stats.record(
        cid, "TSM",
        ok=["yfinance_price", "yfinance_fund", "finnhub_news"],
        errored=[],
        timed_out=["multi_api_news", "reddit", "youtube"],
        skipped=[],
    )
    agg = collector_stats.consume(cid)
    assert agg["ok"] == 3
    assert agg["error"] == 0, "late is not failure"
    assert agg["late"] == 3
    assert agg["failures"] == []
    assert agg["late_names"] == ["TSM:multi_api_news", "TSM:reddit", "TSM:youtube"]


def test_real_errors_still_count_as_failures():
    cid = "cycle-test-err"
    collector_stats.record(cid, "PG", ok=["yfinance_price"], errored=["reddit"],
                           timed_out=["youtube"], skipped=[])
    agg = collector_stats.consume(cid)
    assert agg["error"] == 1
    assert agg["failures"] == ["PG:reddit:error"]
    assert agg["late"] == 1


def test_consume_returns_fresh_zeroes_and_clears():
    cid = "cycle-test-clear"
    collector_stats.record(cid, "A", ok=["x"], errored=[], timed_out=[], skipped=[])
    first = collector_stats.consume(cid)
    assert first["ok"] == 1
    second = collector_stats.consume(cid)
    assert second["ok"] == 0 and second["failures"] == [] and second["late"] == 0
    # The empty default must be a fresh copy, not a shared mutable.
    second["failures"].append("mutation")
    assert collector_stats.consume("nonexistent")["failures"] == []
