"""Price triggers: creation, supersede rules, firing, cancel and list.

These used to patch `order_triggers.get_db` and assert on SQL text
("INSERT INTO price_triggers" in the executed string) and on positional
`execute` arguments (`args[8] == 200.0` for highest_price). The module calls
`mongo_store`/`mongo_query` now, so the patched `get_db` intercepted nothing
and the assertions ran against whatever the live database returned.

Rewritten against the Mongo layer. The supersede assertions are better off for
it: they used to match a SQL fragment, and now they read the actual filter the
update was issued with, so a supersede that targeted the wrong bot or the wrong
trigger_type would fail here instead of passing on a substring match.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from app.trading.order_triggers import (
    create_trigger,
    check_triggers,
    cancel_trigger,
    list_triggers
)


@pytest.fixture
def mongo():
    """Patch the module's Mongo read and write helpers together."""
    store = MagicMock()
    query = MagicMock()
    query.find_rows.return_value = []
    query.find_row.return_value = None
    with patch("app.trading.order_triggers.mongo_store", store), \
         patch("app.trading.order_triggers.mongo_query", query):
        yield store, query


def _inserted(store):
    """The single document handed to insert_docs."""
    collection, docs = store.insert_docs.call_args[0][:2]
    assert collection == "price_triggers"
    return docs[0]


def _supersede_filters(store):
    """Filters of every update that deactivates rows."""
    return [
        c[0][1] for c in store.update_docs.call_args_list
        if c[0][0] == "price_triggers" and c[0][2].get("$set", {}).get("active") is False
    ]


def _consumed_ids(store):
    """Ids of triggers this sweep CONSUMED — deactivated by id, or stamped fired.

    Deliberately narrower than `_supersede_filters`: `check_triggers` opens with
    `_expire_stale_dynamic_triggers()` and `retire_inert_dynamic_triggers()`,
    which legitimately deactivate rows by shape (`created_at < cutoff`,
    `trigger_type: dynamic`). Those are housekeeping, not consumption, and an
    assertion that cannot tell them apart fails for the wrong reason.
    """
    ids = set()
    for c in store.update_docs.call_args_list:
        if c[0][0] != "price_triggers":
            continue
        filt, update = c[0][1], c[0][2].get("$set", {})
        if not isinstance(filt, dict):
            continue
        tid = filt.get("id")
        # The inert sweeper targets a SET (`{"id": {"$in": [...]}}`); only a
        # scalar id is this sweep consuming one specific trigger.
        if not isinstance(tid, str):
            continue
        if update.get("active") is False or "triggered_at" in update:
            ids.add(tid)
    return ids


@pytest.mark.asyncio
async def test_create_trigger_invalid(mongo):
    res1 = await create_trigger("bot1", "AAPL", "invalid_type", 100.0)
    assert "error" in res1
    assert "Invalid trigger_type" in res1["error"]

    res2 = await create_trigger("bot1", "AAPL", "stop_loss", -10.0)
    assert "error" in res2
    assert "trigger_price must be positive" in res2["error"]

    res3 = await create_trigger("bot1", "AAPL", "trailing_stop", 100.0, trailing_pct=-0.1)
    assert "error" in res3
    assert "trailing_stop requires a positive trailing_pct" in res3["error"]


@pytest.mark.asyncio
@patch("app.trading.order_triggers._get_current_price")
async def test_create_trigger_success(mock_get_current_price, mongo):
    store, _ = mongo
    mock_get_current_price.return_value = (150.0, None)

    res = await create_trigger("bot1", "AAPL", "stop_loss", 100.0)

    assert "id" in res
    assert res["ticker"] == "AAPL"
    # Protective triggers supersede prior active same-type rows: a dedupe
    # update runs before the insert (one active stop_loss per position).
    filters = _supersede_filters(store)
    assert len(filters) == 1
    assert filters[0] == {
        "bot_id": "bot1", "ticker": "AAPL", "trigger_type": "stop_loss", "active": True
    }
    assert _inserted(store)["trigger_type"] == "stop_loss"


@pytest.mark.asyncio
@patch("app.trading.order_triggers._get_current_price")
async def test_create_dynamic_trigger_supersedes_same_type(mock_get_current_price, mongo):
    store, _ = mongo
    mock_get_current_price.return_value = (150.0, None)

    res = await create_trigger(
        "bot1", "AAPL", "dynamic", 0.0,
        dynamic_trigger_type="sma_200_reclaim", dynamic_trigger_value=206.75,
    )
    assert "id" in res
    # Same-setup dynamic triggers dedupe: the supersede is keyed on
    # dynamic_trigger_type so re-arming the same setup doesn't stack rows,
    # while a DIFFERENT setup on the same ticker survives.
    #
    # `sma_200_reclaim` is NORMALISED to `sma_200_rise` on the way in (2026-08-20)
    # — this test used to assert it was stored verbatim, which is to say it
    # asserted that a watch which could never fire was written and left for the
    # inert sweeper to delete. Both the supersede key and the stored row take
    # the normalised setup, so re-arming the same condition under either
    # spelling dedupes against the other instead of stacking two rows for one
    # condition.
    filters = _supersede_filters(store)
    assert len(filters) == 1
    assert filters[0]["dynamic_trigger_type"] == "sma_200_rise"
    assert filters[0]["trigger_type"] == "dynamic"
    doc = _inserted(store)
    assert doc["dynamic_trigger_type"] == "sma_200_rise"
    assert doc["dynamic_trigger_value"] == 206.75


@pytest.mark.asyncio
@patch("app.trading.order_triggers._get_current_price")
async def test_create_buy_limit_does_not_supersede(mock_get_current_price, mongo):
    store, _ = mongo
    mock_get_current_price.return_value = (150.0, None)

    res = await create_trigger("bot1", "AAPL", "buy_limit", 140.0)
    assert "id" in res
    # Discrete limits can ladder — only the insert runs, no supersede.
    assert _supersede_filters(store) == []
    assert _inserted(store)["trigger_type"] == "buy_limit"


@pytest.mark.asyncio
@patch("app.trading.order_triggers._get_current_price")
async def test_create_trigger_trailing_stop(mock_get_current_price, mongo):
    store, _ = mongo
    mock_get_current_price.return_value = (200.0, None)

    res = await create_trigger("bot1", "AAPL", "trailing_stop", 100.0, trailing_pct=0.1)

    assert "id" in res
    assert res["trigger_type"] == "trailing_stop"
    # highest_price seeds from the current price, not the trigger price.
    assert _inserted(store)["highest_price"] == 200.0


@pytest.mark.asyncio
@patch("app.services.pipeline_service.PipelineService.start_cycle")
@patch("app.trading.order_triggers._get_current_price")
async def test_check_triggers_stop_loss_fired(mock_get_current_price, mock_start_cycle, mongo):
    store, query = mongo
    # (id, ticker, trigger_type, trigger_price, action, qty_pct, trailing_pct,
    #  highest_price, reason, dynamic_trigger_type, dynamic_trigger_value)
    query.find_rows.return_value = [
        ("trg1", "AAPL", "stop_loss", 100.0, "SELL", 1.0, None, None, "reason", None, None)
    ]
    # Current price is 95, so stop loss should fire
    mock_get_current_price.return_value = (95.0, None)
    mock_start_cycle.return_value = {"status": "starting", "cycle_id": "test_cycle"}

    results = await check_triggers("bot1")

    assert len(results) == 1
    assert results[0]["status"] == "cycle_started"
    assert results[0]["trigger_id"] == "trg1"

    mock_start_cycle.assert_called_once_with(
        tickers=["AAPL"],
        collect=True,
        analyze=True,
        trade=True,
        trigger_type="edge_case_stop_loss"
    )


@pytest.mark.asyncio
@patch("app.services.pipeline_service.PipelineService.start_cycle")
@patch("app.trading.order_triggers._get_current_price")
async def test_check_triggers_stop_loss_not_fired_above_the_line(
    mock_get_current_price, mock_start_cycle, mongo
):
    """A stop that has not been breached must not fire.

    The suite only ever exercised the firing side, so a check_triggers that
    fired unconditionally would have passed it.
    """
    store, query = mongo
    query.find_rows.return_value = [
        ("trg1", "AAPL", "stop_loss", 100.0, "SELL", 1.0, None, None, "reason", None, None)
    ]
    mock_get_current_price.return_value = (105.0, None)

    results = await check_triggers("bot1")

    assert results == []
    mock_start_cycle.assert_not_called()


@pytest.mark.asyncio
@patch("app.services.pipeline_service.PipelineService.start_cycle")
@patch("app.trading.order_triggers._get_current_price")
async def test_check_triggers_trailing_stop(mock_get_current_price, mock_start_cycle, mongo):
    store, query = mongo
    # highest price 200, trail 10%
    query.find_rows.return_value = [
        ("trg1", "AAPL", "trailing_stop", 0.0, "SELL", 1.0, 0.1, 200.0, "reason", None, None)
    ]
    # Trigger fires at 200 * 0.9 = 180. Current price = 175
    mock_get_current_price.return_value = (175.0, None)
    mock_start_cycle.return_value = {"status": "starting", "cycle_id": "test_cycle"}

    results = await check_triggers("bot1")

    assert len(results) == 1
    assert results[0]["trigger_id"] == "trg1"


@pytest.mark.asyncio
async def test_cancel_trigger(mongo):
    store, query = mongo
    query.find_row.return_value = ("trg1", "AAPL", "stop_loss")

    res = await cancel_trigger("trg1")

    assert res["status"] == "cancelled"
    assert res["id"] == "trg1"
    store.update_docs.assert_called_once()
    collection, filt, update = store.update_docs.call_args[0][:3]
    assert collection == "price_triggers"
    assert filt == {"id": "trg1"}
    assert update["$set"]["active"] is False


@pytest.mark.asyncio
async def test_cancel_unknown_trigger_reports_not_found(mongo):
    """Cancelling something that does not exist must not report success."""
    store, query = mongo
    query.find_row.return_value = None

    res = await cancel_trigger("nope")

    assert "error" in res
    store.update_docs.assert_not_called()


def test_list_triggers(mongo):
    store, query = mongo
    now = datetime.now(timezone.utc)
    query.find_rows.return_value = [
        ("trg1", "AAPL", "stop_loss", 100.0, "SELL", 1.0, None, None,
         "reason", True, None, now, "bot")
    ]

    res = list_triggers("bot1")

    assert len(res) == 1
    assert res[0]["id"] == "trg1"
    # active_only defaults to True and must reach the query
    assert query.find_rows.call_args[0][1] == {"bot_id": "bot1", "active": True}


@pytest.mark.asyncio
@patch("app.services.pipeline_service.PipelineService.start_cycle")
@patch("app.trading.order_triggers._get_current_price")
async def test_a_busy_pipeline_does_not_consume_the_trigger(
    mock_get_current_price, mock_start_cycle, mongo
):
    """start_cycle refuses BY RETURN VALUE ("deduplicated"), never by raising.

    The fire branch used to treat any return as a spawn: active=False,
    triggered_at stamped, "cycle_started" appended — while no cycle ran.
    Measured 2026-08-14..20 that swallowed 18 of 22 dynamic fires (82%),
    because single-ticker cycles run ~20-40 min and every fire during one hit
    the dedup path. A busy answer must leave the trigger ACTIVE for the next
    sweep.
    """
    store, query = mongo
    query.find_rows.return_value = [
        ("trg1", "AAPL", "stop_loss", 100.0, "SELL", 1.0, None, None, "reason", None, None)
    ]
    mock_get_current_price.return_value = (95.0, None)
    mock_start_cycle.return_value = {"status": "deduplicated", "message": "Cycle already running"}

    results = await check_triggers("bot1")

    assert results == []
    # The row must NOT be consumed — that is the whole fix.
    assert _consumed_ids(store) == set()


@pytest.mark.asyncio
@patch("app.services.pipeline_service.PipelineService.start_cycle")
@patch("app.trading.order_triggers._get_current_price")
async def test_a_stuck_pipeline_does_not_consume_the_trigger(
    mock_get_current_price, mock_start_cycle, mongo
):
    """The other refusal shape — {"status": "error"} from a stuck state —
    takes the same deferral path as "deduplicated"."""
    store, query = mongo
    query.find_rows.return_value = [
        ("trg1", "AAPL", "stop_loss", 100.0, "SELL", 1.0, None, None, "reason", None, None)
    ]
    mock_get_current_price.return_value = (95.0, None)
    mock_start_cycle.return_value = {"status": "error", "message": "stuck at starting"}

    results = await check_triggers("bot1")

    assert results == []
    assert _consumed_ids(store) == set()


@pytest.mark.asyncio
@patch("app.services.pipeline_service.PipelineService.start_cycle")
@patch("app.trading.order_triggers._get_current_price")
async def test_second_fire_in_a_sweep_survives_the_first_ones_cycle(
    mock_get_current_price, mock_start_cycle, mongo
):
    """Batch fires: the first spawn wins, the second hits dedup and must
    stay active — it used to be consumed alongside the first."""
    store, query = mongo
    query.find_rows.return_value = [
        ("trg1", "AAPL", "stop_loss", 100.0, "SELL", 1.0, None, None, "r", None, None),
        ("trg2", "MSFT", "stop_loss", 300.0, "SELL", 1.0, None, None, "r", None, None),
    ]
    mock_get_current_price.return_value = (50.0, None)
    mock_start_cycle.side_effect = [
        {"status": "starting", "cycle_id": "c1"},
        {"status": "deduplicated", "message": "Cycle already starting"},
    ]

    results = await check_triggers("bot1")

    assert [r["trigger_id"] for r in results] == ["trg1"]
    # trg1 spawned and is consumed; trg2 hit dedup and must survive.
    assert _consumed_ids(store) == {"trg1"}


@pytest.mark.asyncio
@patch("app.services.pipeline_service.PipelineService.start_cycle")
@patch("app.trading.order_triggers._get_current_price")
async def test_check_triggers_pipeline_cooldown_suppresses_immediate_refire(
    mock_get_current_price, mock_start_cycle, mongo
):
    """Triggers generated by pipeline HOLD decisions must have a 30-min cooldown."""
    from datetime import timedelta
    store, query = mongo
    now = datetime.now(timezone.utc)
    recent_created = now - timedelta(minutes=2)

    def mock_find_rows(coll, filter_dict, cols):
        if filter_dict.get("trigger_type") == "dynamic":
            return []
        return [
            ("trg_pipe", "CRDO", "stop_loss", 234.0, "SELL", 1.0, None, None, "drop", None, None, recent_created, "pipeline")
        ]
    query.find_rows.side_effect = mock_find_rows

    # Price is 200, below 234, but within 30-min cooldown
    mock_get_current_price.return_value = (200.0, None)

    results = await check_triggers("bot1")

    assert len(results) == 0
    mock_start_cycle.assert_not_called()
    assert _consumed_ids(store) == set()


@pytest.mark.asyncio
@patch("app.services.pipeline_service.PipelineService.start_cycle")
@patch("app.trading.order_triggers._get_current_price")
async def test_check_triggers_pipeline_cooldown_fires_after_expiry(
    mock_get_current_price, mock_start_cycle, mongo
):
    """Triggers generated by pipeline HOLD decisions fire after 30-min cooldown expires."""
    from datetime import timedelta
    store, query = mongo
    now = datetime.now(timezone.utc)
    expired_created = now - timedelta(minutes=35)

    def mock_find_rows(coll, filter_dict, cols):
        if filter_dict.get("trigger_type") == "dynamic":
            return []
        return [
            ("trg_pipe_old", "CRDO", "stop_loss", 234.0, "SELL", 1.0, None, None, "drop", None, None, expired_created, "pipeline")
        ]
    query.find_rows.side_effect = mock_find_rows

    mock_get_current_price.return_value = (200.0, None)
    mock_start_cycle.return_value = {"status": "starting", "cycle_id": "c_pipe"}

    results = await check_triggers("bot1")

    assert len(results) == 1
    assert results[0]["trigger_id"] == "trg_pipe_old"
    mock_start_cycle.assert_called_once()
    assert _consumed_ids(store) == {"trg_pipe_old"}

