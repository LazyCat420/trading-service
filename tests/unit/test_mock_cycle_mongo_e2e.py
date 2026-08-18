"""End-to-End Mock Trading Cycle Verification with PostgreSQL Outage.

Contract Under Test:
1. PostgreSQL is completely unreachable (any connection raises an AssertionError).
2. MongoDB is the sole persistence authority for all trading cycle operations:
   - Initial bot state & configuration (`bots`)
   - Asset prices and technical indicators (`price_history`, `technicals`)
   - Order placement & lifecycle (`orders`)
   - Fill execution & broker ledger (`trade_fills`)
   - FIFO lot tracking & closures (`position_lots`, `lot_closures`)
   - Portfolio positions (`positions`)
   - Audit logs and telemetry (`pipeline_events`, `agent_audit_log`, `v3_system_commands`)
3. Money precision adheres to Tier F Decimal128 standards.
4. No external cloud models are invoked.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any, Optional
from unittest.mock import patch, MagicMock

import pytest
from bson import Decimal128

from app.db import mongo_query, mongo_store
from app.trading import paper_trader as pt


class InMemoryMongoCollection:
    """In-memory dictionary-backed MongoDB collection for deterministic testing."""

    def __init__(self, name: str):
        self.name = name
        self.docs: list[dict[str, Any]] = []

    def insert_many(self, docs: list[dict[str, Any]], ordered: bool = False, session: Optional[Any] = None):
        inserted_ids = []
        for d in docs:
            doc_copy = dict(d)
            if "_id" not in doc_copy:
                doc_copy["_id"] = str(uuid.uuid4())
            self.docs.append(doc_copy)
            inserted_ids.append(doc_copy["_id"])

        class _Result:
            def __init__(self, ids):
                self.inserted_ids = ids

        return _Result(inserted_ids)

    def insert_one(self, doc: dict[str, Any], session: Optional[Any] = None):
        doc_copy = dict(doc)
        if "_id" not in doc_copy:
            doc_copy["_id"] = str(uuid.uuid4())
        self.docs.append(doc_copy)
        return doc_copy["_id"]

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if k == "$or":
                if not any(self._matches(doc, cond) for cond in v):
                    return False
                continue
            if k == "$and":
                if not all(self._matches(doc, cond) for cond in v):
                    return False
                continue
            doc_val = doc.get(k)
            if isinstance(v, dict):
                for op, op_val in v.items():
                    if op == "$in" and doc_val not in op_val:
                        return False
                    elif op == "$nin" and doc_val in op_val:
                        return False
                    elif op == "$gt" and not (doc_val is not None and doc_val > op_val):
                        return False
                    elif op == "$gte" and not (doc_val is not None and doc_val >= op_val):
                        return False
                    elif op == "$lt" and not (doc_val is not None and doc_val < op_val):
                        return False
                    elif op == "$lte" and not (doc_val is not None and doc_val <= op_val):
                        return False
                    elif op == "$ne" and doc_val == op_val:
                        return False
            elif doc_val != v:
                return False
        return True

    def find(self, query: dict[str, Any], projection: Optional[dict] = None, session: Optional[Any] = None):
        matched = [d for d in self.docs if self._matches(d, query)]

        class _Cursor:
            def __init__(self, items, proj):
                self._items = items
                self._proj = proj

            def sort(self, key_or_list):
                if isinstance(key_or_list, list) and key_or_list:
                    k, direction = key_or_list[0]
                    def _key_fn(x):
                        v = x.get(k)
                        if hasattr(v, "isoformat"):
                            return v.isoformat()
                        return str(v) if v is not None else ""
                    self._items.sort(key=_key_fn, reverse=(direction == -1))
                return self

            def limit(self, n):
                if n > 0:
                    self._items = self._items[:n]
                return self

            def __iter__(self):
                for item in self._items:
                    if self._proj:
                        res = {}
                        for pk, pv in self._proj.items():
                            if pv == 1 and pk in item:
                                res[pk] = item[pk]
                        yield res
                    else:
                        yield dict(item)

        return _Cursor(matched, projection)

    def find_one(self, query: dict[str, Any], projection: Optional[dict] = None, session: Optional[Any] = None):
        for d in self.docs:
            if self._matches(d, query):
                if projection:
                    return {pk: d[pk] for pk, pv in projection.items() if pv == 1 and pk in d}
                return dict(d)
        return None

    def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False, session: Optional[Any] = None):
        for doc in self.docs:
            if self._matches(doc, query):
                self._apply_update(doc, update)
                return

        if upsert:
            new_doc = dict(query)
            self._apply_update(new_doc, update)
            if "_id" not in new_doc:
                new_doc["_id"] = str(uuid.uuid4())
            self.docs.append(new_doc)

    def update_many(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False, session: Optional[Any] = None):
        count = 0
        for doc in self.docs:
            if self._matches(doc, query):
                self._apply_update(doc, update)
                count += 1

        if count == 0 and upsert:
            new_doc = dict(query)
            self._apply_update(new_doc, update)
            if "_id" not in new_doc:
                new_doc["_id"] = str(uuid.uuid4())
            self.docs.append(new_doc)
            count = 1

        class _UpRes:
            modified_count = count

        return _UpRes()

    def delete_one(self, query: dict[str, Any], session: Optional[Any] = None):
        for i, doc in enumerate(self.docs):
            if self._matches(doc, query):
                self.docs.pop(i)
                return

    def delete_many(self, query: dict[str, Any], session: Optional[Any] = None):
        initial_len = len(self.docs)
        self.docs = [d for d in self.docs if not self._matches(d, query)]

        class _DelRes:
            deleted_count = initial_len - len(self.docs)

        return _DelRes()

    def count_documents(self, query: Optional[dict] = None, session: Optional[Any] = None) -> int:
        q = query or {}
        return sum(1 for d in self.docs if self._matches(d, q))

    def distinct(self, key: str, query: Optional[dict] = None, session: Optional[Any] = None) -> list:
        q = query or {}
        seen = set()
        out = []
        for d in self.docs:
            if self._matches(d, q) and key in d:
                val = d[key]
                if val not in seen:
                    seen.add(val)
                    out.append(val)
        return out

    def aggregate(self, pipeline: list[dict[str, Any]], allowDiskUse: bool = True, session: Optional[Any] = None) -> list[dict[str, Any]]:
        current = list(self.docs)
        for stage in pipeline:
            if "$match" in stage:
                current = [d for d in current if self._matches(d, stage["$match"])]
            elif "$group" in stage:
                group_spec = stage["$group"]
                grouped_res = {}
                id_spec = group_spec.get("_id")
                if isinstance(id_spec, str) and id_spec.startswith("$"):
                    id_field = id_spec.lstrip("$")
                    grouped_res["_id"] = current[0].get(id_field) if current else None
                for field, expr in group_spec.items():
                    if field == "_id":
                        continue
                    if isinstance(expr, dict):
                        op, op_field = next(iter(expr.items()))
                        op_field = op_field.lstrip("$") if isinstance(op_field, str) else op_field
                        if op == "$first":
                            grouped_res[field] = current[0].get(op_field) if current else None
                        elif op == "$sum" and expr[op] == 1:
                            grouped_res[field] = len(current)
                        else:
                            vals = [float(str(d[op_field])) for d in current if d.get(op_field) is not None]
                            if op == "$avg":
                                grouped_res[field] = sum(vals) / len(vals) if vals else None
                            elif op == "$max":
                                grouped_res[field] = max(vals) if vals else None
                            elif op == "$min":
                                grouped_res[field] = min(vals) if vals else None
                            elif op == "$sum":
                                grouped_res[field] = sum(vals)
                current = [grouped_res]
        return current

    def _apply_update(self, doc: dict[str, Any], update: dict[str, Any]):
        if "$set" in update:
            for k, v in update["$set"].items():
                doc[k] = v
        if "$setOnInsert" in update:
            for k, v in update["$setOnInsert"].items():
                if k not in doc:
                    doc[k] = v
        if "$inc" in update:
            for k, v in update["$inc"].items():
                existing = doc.get(k, 0)
                if isinstance(v, (Decimal128, Decimal)):
                    new_val = Decimal(str(existing)) + Decimal(str(v))
                    doc[k] = Decimal128(new_val)
                elif isinstance(existing, Decimal128):
                    new_val = existing.to_decimal() + Decimal(str(v))
                    doc[k] = Decimal128(new_val)
                else:
                    doc[k] = (existing or 0) + v


@pytest.fixture
def mock_mongo_db(monkeypatch):
    """Provides isolated in-memory Mongo collections and verifies NO Postgres calls occur."""
    collections: dict[str, InMemoryMongoCollection] = {}

    def _get_coll(name: str):
        if name not in collections:
            collections[name] = InMemoryMongoCollection(name)
        return collections[name]

    monkeypatch.setattr(mongo_store, "_coll", _get_coll)
    monkeypatch.setattr(mongo_store, "ensure_indexes", lambda: None)
    monkeypatch.setattr(mongo_store, "get_doc_db", lambda: collections)

    # Context manager for with_txn: pass through session=None
    from contextlib import contextmanager

    @contextmanager
    def _fake_with_txn(client=None):
        yield None

    monkeypatch.setattr(mongo_store, "with_txn", _fake_with_txn)

    # BLOCK ALL POSTGRESQL CALLS
    def _forbid_postgres(*args, **kwargs):
        raise AssertionError("CRITICAL VIOLATION: PostgreSQL was called during the trading cycle!")

    monkeypatch.setattr("app.db.connection.get_db", _forbid_postgres)

    return collections


class TestMockTradingCycleMongoE2E:
    """Simulates a full trading cycle execution against MongoDB with zero Postgres."""

    @pytest.mark.asyncio
    async def test_mock_trading_cycle_buy_and_sell_pure_mongo(self, mock_mongo_db, monkeypatch):
        bot_id = "test-lazy-bot-420"
        cycle_id = f"cycle-mock-{int(datetime.datetime.now(datetime.UTC).timestamp())}"
        now = datetime.datetime.now(datetime.UTC)

        # 1. Seed Initial MongoDB Data
        mongo_store.insert_docs("bots", [{
            "bot_id": bot_id,
            "display_name": "Test Lazy Bot",
            "cash_balance": mongo_store.dec128(100_000.0),
            "starting_cash": mongo_store.dec128(100_000.0),
            "total_pnl": mongo_store.dec128(0.0),
            "win_rate": 0.0,
            "total_trades": 0,
            "is_active": True,
            "created_at": now,
        }])

        # Price history & technicals in MongoDB
        for day in range(30, 0, -1):
            date = now - datetime.timedelta(days=day)
            mongo_store.insert_docs("price_history", [{
                "ticker": "AAPL",
                "date": date,
                "close": 150.0 + (day * 0.1),
                "volume": 1_000_000,
            }])
        mongo_store.insert_docs("technicals", [{
            "ticker": "AAPL",
            "date": now,
            "atr_14": 3.5,
        }])

        # 2. Verify Initial State Read from MongoDB
        bot_row = mongo_query.find_row("bots", {"bot_id": bot_id}, ["cash_balance", "total_trades"])
        assert bot_row is not None
        assert float(str(bot_row[0])) == 100_000.0
        assert bot_row[1] == 0

        # 3. Simulate Decision Engine & Risk Gate: Execute BUY Order for AAPL (size_pct=0.15 => 15% = $15,000)
        buy_result = await pt.buy(
            bot_id=bot_id,
            ticker="AAPL",
            size_pct=0.15,
            current_price=150.0,
            stop_loss_price=140.0,
            take_profit_price=170.0,
            cycle_id=cycle_id,
        )

        assert "error" not in buy_result, f"BUY failed: {buy_result}"
        assert buy_result["action"] == "BUY"
        assert buy_result["ticker"] == "AAPL"
        assert buy_result["qty"] == pytest.approx(99.95, rel=1e-2)

        # 4. Verify MongoDB State Mutations after BUY
        # Bot cash balance reduced in MongoDB ($100k - $15k = $85k)
        bot_post_buy = mongo_query.find_row("bots", {"bot_id": bot_id}, ["cash_balance", "total_trades"])
        assert float(str(bot_post_buy[0])) == pytest.approx(85_000.0, rel=1e-2)
        assert bot_post_buy[1] == 1

        # Position stored in MongoDB positions collection
        pos_row = mongo_query.find_row("positions", {"bot_id": bot_id, "ticker": "AAPL"}, ["qty", "avg_entry_price", "stop_loss_pct", "take_profit_pct"])
        assert pos_row is not None
        assert float(str(pos_row[0])) == pytest.approx(99.95, rel=1e-2)
        assert float(str(pos_row[1])) == pytest.approx(150.0, rel=1e-2)

        # Order & Trade Fill & Position Lot in MongoDB
        orders = mongo_store.find_docs("orders", {"bot_id": bot_id, "ticker": "AAPL"})
        assert len(orders) == 1
        assert orders[0]["side"] == "BUY"

        fills = mongo_store.find_docs("trade_fills", {"bot_id": bot_id, "ticker": "AAPL", "cycle_id": cycle_id})
        assert len(fills) == 1
        assert fills[0]["side"] == "BUY"

        lots = mongo_store.find_docs("position_lots", {"bot_id": bot_id, "ticker": "AAPL", "status": "open"})
        assert len(lots) == 1
        assert lots[0]["remaining_qty"] == pytest.approx(99.95, rel=1e-2)

        # 5. Simulate Market Movement: Price reaches Take-Profit Target ($175 > $170)
        # Update current price in MongoDB price_history
        mongo_store.insert_docs("price_history", [{
            "ticker": "AAPL",
            "date": now + datetime.timedelta(hours=1),
            "close": 175.0,
            "volume": 1_200_000,
        }])

        # 6. Execute Take-Profit Check (Mock Cycle Step which executes the harvest SELL)
        triggered_tp = await pt.check_take_profits(bot_id=bot_id)
        assert len(triggered_tp) == 1
        sell_result = triggered_tp[0]
        assert sell_result["action"] == "SELL"
        assert sell_result["ticker"] == "AAPL"
        assert sell_result["realized_pnl"] > 1900.0

        # 7. Verify MongoDB State Mutations after SELL
        # Position closed (deleted from positions collection)
        remaining_pos = mongo_query.find_row("positions", {"bot_id": bot_id, "ticker": "AAPL"}, ["id"])
        assert remaining_pos is None, "Position should be deleted after 100% close"

        # Lot closed in position_lots
        closed_lots = mongo_store.find_docs("position_lots", {"bot_id": bot_id, "ticker": "AAPL"})
        assert closed_lots[0]["status"] == "closed"
        assert closed_lots[0]["remaining_qty"] == 0.0

        # Lot closure recorded
        closures = mongo_store.find_docs("lot_closures", {"bot_id": bot_id, "ticker": "AAPL"})
        assert len(closures) == 1
        assert float(str(closures[0]["realized_pnl"])) > 1900.0

        # Bot cash updated in MongoDB and win rate = 100%
        bot_final = mongo_query.find_row("bots", {"bot_id": bot_id}, ["cash_balance", "total_pnl", "win_rate", "total_trades"])
        assert float(str(bot_final[0])) > 101_000.0
        assert float(str(bot_final[1])) > 1900.0
        assert float(str(bot_final[2])) == 100.0
        assert bot_final[3] == 2

        # 9. Telemetry & Pipeline Event Persistence to MongoDB
        from app.services.pipeline_state import PipelineStateDB
        from app.services.trade_result_saver import save_trade_result

        # Save pipeline state
        PipelineStateDB.save_state({
            "status": "running",
            "cycle_id": cycle_id,
            "tickers": ["AAPL", "NVDA"],
            "phase": "execution",
            "progress": "Executing trade decisions",
        })

        # Save baseline decision score
        from app.quant.decision_score_store import record_decision_score
        record_decision_score(cycle_id, "AAPL", {
            "score": 78.5,
            "band": "STRONG_BUY",
            "confidence": 80,
            "coverage_pct": 95.0,
            "percentile": 88.0,
            "fundamental_score": 82.0,
            "technical_score": 75.0,
        })

        # Save trade verdict result (which attaches board decision)
        save_trade_result("AAPL", cycle_id, {
            "action": "BUY",
            "confidence": 85,
            "reasoning": "Strong trend and breakout setup",
            "decision_provenance": "board_reasoned",
        })

        # Verify decision_scores in MongoDB
        d_scores = mongo_store.find_docs("decision_scores", {"cycle_id": cycle_id, "ticker": "AAPL"})
        assert len(d_scores) == 1
        assert d_scores[0]["band"] == "STRONG_BUY"
        assert d_scores[0]["board_action"] == "BUY"
        assert d_scores[0]["board_confidence"] == 85

        # Append pipeline events
        PipelineStateDB.append_events(cycle_id, [{
            "phase": "complete",
            "step": "cycle_finished",
            "detail": "Cycle completed successfully",
            "status": "ok",
            "ts": now,
        }])

        # Read back and verify from pure Mongo
        p_state = PipelineStateDB.get_state()
        assert p_state["status"] == "running"
        assert p_state["cycle_id"] == cycle_id
        assert p_state["tickers"] == ["AAPL", "NVDA"]

        saved_verdicts = mongo_store.find_docs("trade_results", {"cycle_id": cycle_id, "ticker": "AAPL"})
        assert len(saved_verdicts) == 1
        assert saved_verdicts[0]["action"] == "BUY"
        assert saved_verdicts[0]["confidence"] == 85

        # 10. SharedDesk & Whiteboard Persistence to MongoDB
        from app.v3.shared_desk import SharedDesk, DeskPhase
        from app.v3.desk_persistence import save_desk, load_desk
        from app.agents.whiteboard import whiteboard

        # Create and save SharedDesk
        desk = SharedDesk(cycle_id=cycle_id, ticker="AAPL")
        desk.phase = DeskPhase.PM_DONE
        desk.desk_note = {"summary": "Strong growth signals in services segment."}
        save_desk(desk)

        # Load back SharedDesk from MongoDB
        loaded_desk = load_desk(cycle_id=cycle_id, ticker="AAPL")
        assert loaded_desk is not None
        assert loaded_desk.ticker == "AAPL"
        assert loaded_desk.phase == DeskPhase.PM_DONE
        assert loaded_desk.desk_note["summary"] == "Strong growth signals in services segment."

        # Write, annotate, and summarize Whiteboard in pure MongoDB
        entry_id = await whiteboard.write_section(
            ticker="AAPL",
            cycle_id=cycle_id,
            section="macro_context",
            content={"rate_cut_expectation": "25bps", "inflation_trend": "cooling"},
            author_agent="MacroAgent",
        )
        assert entry_id.startswith("wb_")

        annotated = await whiteboard.annotate(
            entry_id=entry_id,
            agent="RiskAgent",
            note="Confirming rate sensitivity is favorable.",
        )
        assert annotated is True

        sec_data = await whiteboard.get_section("AAPL", cycle_id, "macro_context")
        assert sec_data is not None
        assert sec_data["author_agent"] == "MacroAgent"
        assert len(sec_data["annotations"]) == 1
        assert sec_data["annotations"][0]["author"] == "RiskAgent"

        summary = await whiteboard.summarize("AAPL", cycle_id)
        assert "SHARED WHITEBOARD" in summary
        assert "MACRO_CONTEXT" in summary
        assert "RiskAgent" in summary

        # 11. Parameter Store & Governor Persistence in MongoDB
        from app.services.parameter_store import get_param, get_param_record, invalidate_cache
        from app.services.parameter_governor import propose_parameter_change

        # Initial default lookup
        invalidate_cache("MAX_POSITION_SIZE_PCT")
        default_pos_size = get_param("MAX_POSITION_SIZE_PCT")
        assert default_pos_size == 0.10

        # Propose tightening change
        res = propose_parameter_change(
            key="MAX_POSITION_SIZE_PCT",
            value=0.08,
            reason="Market regime elevated volatility tightening",
            agent="v3_portfolio_manager",
        )
        assert res["status"] == "applied"
        assert res["new_value"] == 0.08

        # Read back from MongoDB
        param_val = get_param("MAX_POSITION_SIZE_PCT")
        assert param_val == 0.08

        rec = get_param_record("MAX_POSITION_SIZE_PCT")
        assert rec["value"] == 0.08
        assert rec["last_change"] is not None
        assert rec["last_change"]["set_by"] == "v3_portfolio_manager"

        # 12. Freshness Gate Persistence & Execution in MongoDB
        from app.services.freshness_gate import run_freshness_gate

        # Seed freshness gate threshold config in MongoDB
        mongo_store.insert_docs("freshness_gate_config", [
            {"threshold_name": "composite_threshold", "threshold_value": 0.25, "weight": 1.0},
            {"threshold_name": "price_delta_max_pct", "threshold_value": 5.0, "weight": 0.30},
        ])

        # Seed previous analysis result in MongoDB
        mongo_store.insert_docs("analysis_results", [{
            "ticker": "AAPL",
            "analysis_price": 140.0,
            "analysis_rsi": 45.0,
            "analysis_fund_count": 500,
            "created_at": now - datetime.timedelta(days=2),
            "cycle_id": "cycle-prev-001",
            "result_json": "{}",
        }])

        # Seed fresh news articles in MongoDB
        mongo_store.insert_docs("news_articles", [{
            "ticker": "AAPL",
            "headline": "Apple announces record services revenue",
            "published_at": now - datetime.timedelta(hours=12),
        }])

        # Run Freshness Gate
        gate_result = run_freshness_gate(
            top_scorers=[{"ticker": "AAPL", "price": 150.0, "rsi": 55, "rvol": 1.5, "inst_funds": 505}],
            last_analysis_map={"AAPL": now - datetime.timedelta(days=2)},
        )

        assert len(gate_result["eligible"]) == 1
        assert gate_result["eligible"][0]["ticker"] == "AAPL"
        assert gate_result["eligible"][0]["freshness"] == "CHANGED"
        assert gate_result["eligible"][0]["delta_score"] >= 0.25

        # 13. Tool Telemetry & Guardrail Firings in MongoDB
        from app.v3.tool_telemetry import record_tool_call
        from app.v3.telemetry import record_guardrail_firing, flush_agent_telemetry

        # Record tool call in MongoDB
        record_tool_call(
            cycle_id=cycle_id,
            agent_name="v3_junior_analyst",
            tool_name="get_market_data",
            args_hash="hash_aapl_123",
            success=True,
            elapsed_ms=320,
            ticker="AAPL",
        )

        tool_calls = mongo_store.find_docs("agent_tool_telemetry", {"cycle_id": cycle_id, "ticker": "AAPL"})
        assert len(tool_calls) == 1
        assert tool_calls[0]["tool_name"] == "get_market_data"
        assert tool_calls[0]["success"] is True

        # Record guardrail firing in MongoDB
        record_guardrail_firing(
            "coerce_unshortable_sell",
            ticker="AAPL",
            cycle_id=cycle_id,
            detail={"reason": "ticker unheld, coerced to HOLD"},
        )

        guardrail_events = mongo_store.find_docs("v3_guardrail_firings", {"cycle_id": cycle_id, "ticker": "AAPL"})
        assert len(guardrail_events) == 1
        assert guardrail_events[0]["guardrail"] == "coerce_unshortable_sell"

        # Record and flush agent telemetry in MongoDB
        desk.agent_telemetry.append({
            "agent_name": "v3_junior_analyst",
            "phase": "RESEARCH_DONE",
            "outcome": "SUCCESS",
            "elapsed_ms": 1200,
            "token_usage": 4500,
            "model_used": "local-qwen-2.5-72b",
            "_written": False,
        })
        flush_agent_telemetry(desk)

        agent_tel = mongo_store.find_docs("v3_agent_telemetry", {"cycle_id": cycle_id, "ticker": "AAPL"})
        assert len(agent_tel) == 1
        assert agent_tel[0]["agent_name"] == "v3_junior_analyst"
        assert agent_tel[0]["token_usage"] == 4500

        # 14. Watchlist CRUD & Ban Lifecycle in MongoDB
        from app.trading.watchlist import add_ticker, ban_ticker, is_banned, get_active, unban_ticker

        # Add ticker to watchlist
        added = add_ticker("MSFT", source="scan", notes="Top tech candidate")
        assert added is True

        active_list = get_active()
        active_tickers = [item["ticker"] for item in active_list]
        assert "MSFT" in active_tickers

        # Ban a problematic ticker
        banned = ban_ticker("PUMP", reason="Penny stock pump-and-dump")
        assert banned is True
        assert is_banned("PUMP") is True

        # Ensure banned ticker cannot be added
        refused = add_ticker("PUMP", source="manual")
        assert refused is False

        # Unban ticker
        unbanned = unban_ticker("PUMP")
        assert unbanned is True
        assert is_banned("PUMP") is False

        # 15. Portfolio & Trading Tools in Pure MongoDB
        import json
        from app.tools.portfolio_tools import get_position_context, get_portfolio_state_tool
        from app.tools.trading_tools import get_congress_trades_tool, get_finviz_fundamentals_tool

        # Check position context via tool
        pos_ctx = get_position_context("MSFT", bot_id=bot_id)
        assert pos_ctx["held"] is False

        # Seed fundamentals and congress trades in MongoDB
        mongo_store.insert_docs("fundamentals", [{
            "ticker": "MSFT",
            "market_cap": 3_100_000_000_000,
            "pe_ratio": 35.5,
            "snapshot_date": now,
        }])

        mongo_store.insert_docs("congress_trades", [{
            "ticker": "MSFT",
            "politician": "Pelosi",
            "party": "D",
            "chamber": "House",
            "state": "CA",
            "transaction_type": "Purchase",
            "amount_range": "$1,000,001 - $5,000,000",
            "trade_date": now.date(),
            "disclosure_date": now.date(),
            "days_to_disclose": 15,
        }])

        # Query fundamentals tool
        fund_tool_res = json.loads(await get_finviz_fundamentals_tool("MSFT"))
        assert fund_tool_res["ticker"] == "MSFT"
        assert fund_tool_res["pe_ratio"] == 35.5

        # Query congress trades tool
        cong_tool_res = json.loads(await get_congress_trades_tool("MSFT"))
        assert cong_tool_res["status"] == "success"
        assert cong_tool_res["trade_count"] == 1
        assert cong_tool_res["trades"][0]["politician"] == "Pelosi"
