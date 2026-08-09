"""Tests for the news fact-extraction backfill worker (the Jetson's job).

The load-bearing property is the HARD PIN. This worker exists to use idle
capacity on the spare box; a version of it that fails over onto Gold Spark
would quietly point 42,000 low-priority extractions at the box the trading
cycle depends on — strictly worse than not running at all. So "the Jetson is
down" must mean "stop", never "use the other one".
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.services import news_backfill as nb
from app.services import news_extraction as ne

SPARK = ("vllm-2", "deepseek-v4-flash-0731", "http://spark:8000")
JETSON = ("vllm", "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit", "http://jetson:8000")

ROWS = [
    ("a1", "MSFT", "MSFT Q2", "x" * 900),
    ("a2", "NVDA", "NVDA guide", "y" * 900),
]


@pytest.fixture(autouse=True)
def _no_live_cycle_check():
    """`backfill_once` consults pipeline_state before doing anything. Without
    this, every test below silently opens a connection to the LIVE database and
    its result depends on whether a cycle happens to be running — passing for a
    reason that has nothing to do with what it asserts."""
    with patch.object(nb, "_cycle_is_running", return_value=False):
        yield


# ── the pin ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_pinned_host_that_is_down_yields_no_targets_not_a_substitute():
    """The whole point of the worker. If this ever returns Gold Spark, the
    backfill has silently become a tax on the trading cycle."""
    with patch("app.services.vllm_hosts.vllm_targets",
               new=AsyncMock(return_value=[SPARK])):
        targets = await ne._chat_targets(only=("jetson",))
    assert targets == [], "a pin must remove hosts, not reorder them"


@pytest.mark.asyncio
async def test_no_target_within_the_pin_fails_soft_without_calling_any_host():
    with patch("app.services.vllm_hosts.vllm_targets",
               new=AsyncMock(return_value=[SPARK])), \
         patch("httpx.AsyncClient") as client:
        facts, provider = await ne.extract_article_facts_with_source(
            "z" * 900, "MSFT", only=("jetson",))
    assert facts is None and provider == ""
    client.assert_not_called()


@pytest.mark.asyncio
async def test_the_backfill_asks_for_its_configured_box():
    extract = AsyncMock(return_value=([{"class": "macro"}], "jetson"))
    with patch.object(nb, "_select_batch", return_value=list(ROWS)), \
         patch.object(nb, "extract_article_facts_with_source", extract), \
         patch.object(nb, "_store_facts"):
        await nb.backfill_once()

    assert extract.await_count == len(ROWS)
    for call in extract.await_args_list:
        assert call.kwargs["only"] == nb.ENDPOINT


# ── retry semantics ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failed_extraction_stores_nothing_so_the_row_is_retried():
    """`facts_extracted_at` is the only record that an article was attempted.
    Writing it after a transport failure would mark 42,000 articles done while
    extracting none of them — and nothing downstream could tell."""
    with patch.object(nb, "_select_batch", return_value=list(ROWS)), \
         patch.object(nb, "extract_article_facts_with_source",
                      new=AsyncMock(return_value=(None, ""))), \
         patch.object(nb, "_store_facts") as store:
        counts = await nb.backfill_once()

    store.assert_not_called()
    assert counts["selected"] == 2 and counts["failed"] == 2
    assert counts["extracted"] == 0 and counts["facts"] == 0


@pytest.mark.asyncio
async def test_an_empty_fact_list_is_a_result_and_is_stored():
    """[] means "this article has nothing" — storing it is what stops the
    worker re-extracting the same junk forever."""
    with patch.object(nb, "_select_batch", return_value=[ROWS[0]]), \
         patch.object(nb, "extract_article_facts_with_source",
                      new=AsyncMock(return_value=([], "jetson"))), \
         patch.object(nb, "_store_facts") as store:
        counts = await nb.backfill_once()

    store.assert_called_once()
    assert counts["extracted"] == 1 and counts["facts"] == 0


@pytest.mark.asyncio
async def test_stored_rows_are_attributed_to_the_box_that_answered():
    with patch.object(nb, "_select_batch", return_value=[ROWS[0]]), \
         patch.object(nb, "extract_article_facts_with_source",
                      new=AsyncMock(return_value=([{"class": "macro"}], "jetson"))), \
         patch.object(nb, "_store_facts") as store:
        await nb.backfill_once()

    assert store.call_args.args[2] == "jetson"


# ── switches ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disabling_extraction_also_disables_the_backfill():
    """The backfill is a second consumer of the same feature. If the master
    extraction switch is off, a backfill that kept writing would repopulate the
    column the switch was flipped to stop trusting."""
    with patch.object(nb, "_EXTRACTION_ENABLED", False), \
         patch.object(nb, "_select_batch") as select:
        counts = await nb.backfill_once()
    select.assert_not_called()
    assert counts["selected"] == 0


@pytest.mark.asyncio
async def test_an_empty_backlog_is_not_an_error():
    with patch.object(nb, "_select_batch", return_value=[]), \
         patch.object(nb, "extract_article_facts_with_source") as extract:
        counts = await nb.backfill_once()
    extract.assert_not_called()
    assert counts["selected"] == 0


# ── yielding to the cycle ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_backfill_stands_down_while_a_cycle_runs():
    """Not for CPU: the cycle extracts on Gold Spark. It is to keep the Jetson
    responsive for the one gatekeeper shadow call per cycle, which is still
    accruing toward n>=10 and records a queueing timeout as AGENT_ERROR."""
    with patch.object(nb, "_cycle_is_running", return_value=True), \
         patch.object(nb, "_select_batch") as select:
        counts = await nb.backfill_once()
    select.assert_not_called()
    assert counts["yielded"] is True


@pytest.mark.asyncio
async def test_an_unreadable_pipeline_state_does_not_stop_the_worker_forever():
    """Fail-open. A cycle check that errors closed would silently retire the
    job, and the only symptom would be a backlog that stops shrinking."""
    with patch.object(nb, "get_db", side_effect=RuntimeError("db down")):
        assert nb._cycle_is_running() is False
