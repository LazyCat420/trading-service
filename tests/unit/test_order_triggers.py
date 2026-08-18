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
    filters = _supersede_filters(store)
    assert len(filters) == 1
    assert filters[0]["dynamic_trigger_type"] == "sma_200_reclaim"
    assert filters[0]["trigger_type"] == "dynamic"
    doc = _inserted(store)
    assert doc["dynamic_trigger_type"] == "sma_200_reclaim"
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
    mock_start_cycle.return_value = {"cycle_id": "test_cycle"}

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
    mock_start_cycle.return_value = {"cycle_id": "test_cycle"}

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
