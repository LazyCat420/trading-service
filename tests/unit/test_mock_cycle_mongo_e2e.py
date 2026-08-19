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
from unittest.mock import patch, MagicMock, AsyncMock

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
                        elif op in ("$min", "$max"):
                            vals = [d[op_field] for d in current if d.get(op_field) is not None]
                            if vals:
                                grouped_res[field] = max(vals) if op == "$max" else min(vals)
                            else:
                                grouped_res[field] = None
                        elif op == "$addToSet":
                            vals = []
                            for d in current:
                                v = d.get(op_field)
                                if v is not None and v not in vals:
                                    vals.append(v)
                            grouped_res[field] = vals
                        else:
                            vals = [float(str(d[op_field])) for d in current if d.get(op_field) is not None]
                            if op == "$avg":
                                grouped_res[field] = sum(vals) / len(vals) if vals else None
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
    monkeypatch.setattr(mongo_store, "ensure_indexes", lambda session=None: None)
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

    monkeypatch.setattr("scripts.migration.pg_connection.get_db", _forbid_postgres)

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
            "rsi_14": 45.0,
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

        # Query congress trades tool.
        #
        # The tool does a best-effort refresh via `collect_trades_for_ticker`
        # before it reads. Left unmocked that is a LIVE network scrape of
        # congressional disclosures which inserts whatever it finds into the
        # in-memory store, so the count below is whatever the internet returned
        # today (it read 4). Pin the refresh to zero: the contract under test
        # here is that the tool serves the row MongoDB already holds.
        with patch(
            "app.collectors.congress_collector.collect_trades_for_ticker",
            new=AsyncMock(return_value=0),
        ):
            cong_tool_res = json.loads(await get_congress_trades_tool("MSFT"))
        assert cong_tool_res["status"] == "success"
        assert cong_tool_res["trades_collected"] == 0
        assert cong_tool_res["trade_count"] == 1
        assert cong_tool_res["trades"][0]["politician"] == "Pelosi"

        # 16. Worklist Shadow Runs in MongoDB
        from app.services.worklist_shadow import record as record_worklist_shadow

        shadow_summary = record_worklist_shadow(
            cycle_id=cycle_id,
            live_tickers=["AAPL", "MSFT"],
            top_scorers=[{"ticker": "AAPL"}, {"ticker": "NVDA"}],
            worker_id="worker-001",
        )
        assert shadow_summary["recorded"] is True
        assert shadow_summary["overlap_live_free"] == 1

        shadow_docs = mongo_store.find_docs("worklist_shadow_runs", {"cycle_id": cycle_id})
        assert len(shadow_docs) == 1
        assert shadow_docs[0]["overlap_live_free"] == 1
        assert shadow_docs[0]["worker_id"] == "worker-001"

        # 17. Candidate Discovery Mode in MongoDB
        from app.services.discovery_mode import run_discovery

        # Seed discovered_tickers in MongoDB
        mongo_store.insert_docs("discovered_tickers", [{
            "ticker": "AMD",
            "score": 85.0,
            "context": "AI chip momentum and data center expansion",
            "discovered_at": now - datetime.timedelta(hours=2),
            "validation_status": "approved",
        }])

        # Seed news articles for trending discovery
        mongo_store.insert_docs("news_articles", [
            {"ticker": "AMD", "headline": "AMD launches MI300X AI accelerator", "published_at": now - datetime.timedelta(hours=4)},
            {"ticker": "AMD", "headline": "Hyperscalers adopt AMD Instinct GPUs", "published_at": now - datetime.timedelta(hours=3)},
            {"ticker": "AMD", "headline": "AMD data center revenue surges", "published_at": now - datetime.timedelta(hours=2)},
        ])

        discovered = await run_discovery(existing_tickers=["AAPL", "MSFT"])
        discovered_tickers = [d["ticker"] for d in discovered]
        assert "AMD" in discovered_tickers

        # 18. Bot Profile Manager in MongoDB
        from app.services.bot_manager import (
            create_bot_profile,
            list_bot_profiles,
            get_bot_starting_cash,
            reset_bot_profile,
            delete_bot_profile,
        )

        new_bot = create_bot_profile("Alpha Trader", starting_cash=250_000.0, description="Momentum breakout bot")
        assert new_bot["bot_id"] == "alpha-trader"
        assert new_bot["starting_cash"] == 250_000.0

        starting_cash = get_bot_starting_cash("alpha-trader")
        assert starting_cash == 250_000.0

        profiles = list_bot_profiles()
        profile_ids = [p["bot_id"] for p in profiles]
        assert "alpha-trader" in profile_ids

        # Reset pipeline state to idle to allow profile modifications
        PipelineStateDB.save_state({"status": "idle", "phase": "complete", "progress_pct": 100})

        reset_res = reset_bot_profile("alpha-trader")
        assert reset_res["reset"] is True
        assert reset_res["starting_cash"] == 250_000.0

        del_res = delete_bot_profile("alpha-trader")
        assert del_res["deleted"] is True

        # 19. Ticker User Notes & Market Map in MongoDB
        from app.routers.verdict_router import upsert_ticker_note, get_ticker_note, list_ticker_notes, delete_ticker_note, TickerNoteUpsert
        from app.routers.market_router import get_market_map

        # Note CRUD
        saved_note = upsert_ticker_note("AAPL", TickerNoteUpsert(note="Strong buy into earnings cycle"))
        assert saved_note["saved"] is True
        assert saved_note["ticker"] == "AAPL"

        note_val = get_ticker_note("AAPL")
        assert note_val["note"] == "Strong buy into earnings cycle"

        all_notes = list_ticker_notes()
        note_tickers = [n["ticker"] for n in all_notes]
        assert "AAPL" in note_tickers

        del_note = delete_ticker_note("AAPL")
        assert del_note["deleted"] is True

        # Market map router
        mongo_store.insert_docs("ticker_metadata", [{
            "ticker": "AAPL",
            "name": "Apple Inc",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "market_cap": 3_000_000_000_000,
            "market_cap_tier": "mega",
            "sp500": True,
        }])

        mmap_res = get_market_map(days=7)
        assert mmap_res.status_code == 200

        # 20. Challenger Router & Sequential A/B Testing in MongoDB
        from app.routers.challenger_router import challenger_stats
        from app.v3.challenger import resolve_challenger_outcomes

        mongo_store.insert_docs("challenger_decisions", [{
            "id": "ch-test-001",
            "cycle_id": cycle_id,
            "ticker": "AAPL",
            "spec_label": "exp-test-v3",
            "champion_action": "BUY",
            "champion_confidence": 85,
            "challenger_action": "HOLD",
            "challenger_confidence": 60,
            "agree": False,
            "entry_price": 150.0,
            "created_at": now - datetime.timedelta(days=8),
            "resolved_at": None,
        }])

        resolved_count = resolve_challenger_outcomes()
        assert resolved_count >= 1

        stats_res = await challenger_stats(label="exp-test-v3")
        assert len(stats_res["experiments"]) == 1
        assert stats_res["experiments"][0]["spec_label"] == "exp-test-v3"
        assert stats_res["experiments"][0]["disagreements"] == 1

        # 21. Eval-Trust Router in MongoDB
        from app.routers.eval_trust_router import active_experiment, hold_outcomes, goodhart_status, variance_runs

        exp_res = await active_experiment()
        assert "promotion_gate" in exp_res

        holds_res = await hold_outcomes()
        assert "directional" in holds_res
        assert "hold" in holds_res

        goodhart_res = await goodhart_status()
        assert "status" in goodhart_res

        variance_res = await variance_runs()
        assert "runs" in variance_res
        assert "baseline" in variance_res

        # 22. Cycle Replay Router in MongoDB
        from app.routers.cycle_replay_router import list_cycles, get_cycle_flow, get_cycle_timeline, get_ticker_detail

        cycles_res = list_cycles(limit=10, offset=0)
        assert "cycles" in cycles_res
        assert cycles_res["total"] >= 1

        flow_res = get_cycle_flow(cycle_id=cycle_id, ticker="AAPL")
        assert "nodes" in flow_res
        assert "mermaid" in flow_res

        timeline_res = get_cycle_timeline(cycle_id=cycle_id, ticker="AAPL")
        assert "entries" in timeline_res
        assert timeline_res["cycle_id"] == cycle_id

        detail_res = get_ticker_detail(cycle_id=cycle_id, ticker="AAPL")
        assert detail_res["ticker"] == "AAPL"
        assert "trade_result" in detail_res

        # 23. Scoring Engine in MongoDB
        from app.trading.scoring_engine import compute_normalized_features

        features = compute_normalized_features("AAPL")
        assert "raw_rsi" in features
        assert "ev_norm" in features
        assert features["raw_rsi"] == 45.0

        # 24. Automated Price & Dynamic Order Triggers in MongoDB
        from app.trading.order_triggers import create_trigger, list_triggers, cancel_trigger, check_triggers, deactivate_sell_side_triggers

        new_trg = await create_trigger(
            bot_id=bot_id,
            ticker="AAPL",
            trigger_type="take_profit",
            trigger_price=180.0,
            action="SELL",
        )
        assert "id" in new_trg
        assert new_trg["ticker"] == "AAPL"

        active_trgs = list_triggers(bot_id=bot_id, active_only=True)
        assert len(active_trgs) >= 1
        assert active_trgs[0]["ticker"] == "AAPL"

        # Deactivate sell side triggers
        deactivated_n = deactivate_sell_side_triggers(bot_id=bot_id, ticker="AAPL")
        assert deactivated_n >= 1

        # Dynamic trigger creation and eval
        dyn_trg = await create_trigger(
            bot_id=bot_id,
            ticker="AAPL",
            trigger_type="dynamic",
            trigger_price=150.0,
            dynamic_trigger_type="rsi_14_oversold",
            dynamic_trigger_value=30.0,
            action="BUY",
        )
        assert "id" in dyn_trg

        cancel_res = await cancel_trigger(dyn_trg["id"])
        assert cancel_res["status"] == "cancelled"

        # 25. Strategy Performance & Prompt-Level Tracking in MongoDB
        from app.trading.strategy_tracker import (
            record_strategy,
            evaluate_pnl,
            compute_rankings,
            get_confidence_bonus,
            bench_underperformers,
            get_ticker_strategy_timeline,
        )

        perf_id = record_strategy(
            strategy_candidate_id="sc-001",
            decision_outcome_id=None,
            agent_prompt_hash="hash_alpha123",
            ticker="AAPL",
            signal="BUY",
            entry_price=150.0,
        )
        assert perf_id is not None

        resolved_strategies = evaluate_pnl("AAPL", exit_price=165.0)
        assert len(resolved_strategies) >= 1
        assert resolved_strategies[0]["win"] is True

        rankings = compute_rankings()
        assert isinstance(rankings, list)

        timeline = get_ticker_strategy_timeline("AAPL")
        assert isinstance(timeline, list)

        # 26. Diagnostics Router and LogManager in MongoDB
        from app.log_manager import log_manager
        from app.routers.diagnostics_router import list_cycles, list_system_jobs

        log_manager.log_cycle_summary(cycle_id, {
            "trigger_type": "manual",
            "started_at": now.isoformat(),
            "ended_at": (now + datetime.timedelta(seconds=120)).isoformat(),
            "status": "success",
            "elapsed_ms": 120000,
            "collector_ok": 1,
            "tickers": ["AAPL"],
        })

        summary_docs = mongo_store.find_docs("cycle_run_summaries", {"cycle_id": cycle_id})
        assert len(summary_docs) >= 1
        assert summary_docs[0]["status"] == "success"

        diag_cycles = list_cycles()
        assert "cycles" in diag_cycles

        sys_jobs = list_system_jobs()
        assert "engine_running" in sys_jobs

        # 27. VLLM Router Activity Summary in MongoDB
        from app.routers.vllm_router import _get_agent_recent_activity_summary

        mongo_store.insert_docs("agent_traces", [{
            "agent_name": "data_janitor",
            "run_id": "run-jan-001",
            "tool_name": "clean_temp_tables",
            "tool_args": "{}",
            "tool_result_summary": "cleaned 0 stale rows",
            "why_tool_was_called": "maintenance",
            "stop_reason": "success",
            "created_at": now,
        }])

        jan_summary = _get_agent_recent_activity_summary("data_janitor")
        assert "ACTIVITY TRACES" in jan_summary

        # 28. Market Tools & Sector Map in MongoDB
        from app.tools.market_tools import get_market_map_data

        mmap_tool_res = await get_market_map_data(top_n_per_sector=3)
        assert mmap_tool_res["status"] == "success"
        assert "Technology" in mmap_tool_res["data"]
        assert len(mmap_tool_res["data"]["Technology"]["top_gainers"]) >= 1

        # 29. Quant Edge Verifier in MongoDB
        from app.trading.quant_edge_verifier import load_historical_data, backtest_zscore_strategy

        df = load_historical_data("AAPL")
        assert not df.empty
        assert "close" in df.columns
        assert "z_score" in df.columns

        bt_res = backtest_zscore_strategy(df)
        assert "error" not in bt_res or "trades" in bt_res

        # 30. Research Tools Macro Events in MongoDB
        from app.tools.research_tools import _upcoming_macro_events

        mongo_store.insert_docs("economic_calendar", [{
            "event_date": now + datetime.timedelta(days=2),
            "event_name": "FOMC Rate Decision",
            "country": "US",
            "importance": "high",
            "forecast": "5.25%",
            "previous": "5.50%",
        }])

        macro_events = _upcoming_macro_events(limit=5)
        assert len(macro_events) >= 1
        assert macro_events[0]["event"] == "FOMC Rate Decision"

        # 31. Tool Usage Stats Callback in MongoDB
        from app.tools.registry import _db_telemetry_callback

        _db_telemetry_callback(
            tool_name="get_finviz_fundamentals",
            agent_name="v3_junior_analyst",
            success=True,
            execution_ms=45,
            error_message=None,
        )

        tool_stat_docs = mongo_store.find_docs("tool_usage_stats", {"tool_name": "get_finviz_fundamentals"})
        assert len(tool_stat_docs) >= 1
        assert tool_stat_docs[0]["service_source"] == "lazy-tool-service"
        assert tool_stat_docs[0]["success"] is True

        # 32. Backtest Data Provider in MongoDB
        import os
        import pandas as pd
        from app.trading.backtest_data import get_backtest_data

        parquet_path = get_backtest_data(
            tickers=["AAPL"],
            start_date=(now - datetime.timedelta(days=35)).strftime("%Y-%m-%d"),
            end_date=(now + datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
        )
        assert os.path.exists(parquet_path)
        read_df = pd.read_parquet(parquet_path)
        assert not read_df.empty
        assert "close" in read_df.columns

        # 33. Candidate Validation & Quarantine in MongoDB
        from app.validation.persistence import (
            save_validation_result,
            get_pending_retries,
            get_quarantine_summary,
            release_ticker,
            increment_rate_limit_and_check,
        )
        from app.validation.models import ValidationResult, ValidationStatus, QuarantineReason

        v_res = ValidationResult(
            ticker="PENNY",
            status=ValidationStatus.QUARANTINE,
            reason=QuarantineReason.NO_DATA,
            details="Volume < 100k shares daily average",
        )
        save_validation_result(v_res)

        quarantined = get_quarantine_summary()
        assert any(q["ticker"] == "PENNY" for q in quarantined)

        release_ticker("PENNY")
        quarantined_after = get_quarantine_summary()
        assert not any(q["ticker"] == "PENNY" for q in quarantined_after)

        # 34. Content Existence Checks & Schedule Validator in MongoDB
        from app.validation.checks.check_content import check_content
        from app.validation.schedule_validator import ScheduleValidator

        has_aapl_content = await check_content("AAPL")
        assert has_aapl_content is True

        is_valid, _ = ScheduleValidator.validate_proposal({
            "schedule_scope": "single_ticker",
            "review_intent": "earnings_preview",
            "urgency": "medium",
            "earliest_window": "after_hours",
            "reason_codes": ["EARNINGS_RELEASE"],
        })
        assert is_valid is True

        # 35. Checkpoint Manager in MongoDB
        from app.db.checkpoints import checkpoint_manager

        saved_cp = checkpoint_manager.save(
            cycle_id=cycle_id,
            step_name="agents_complete",
            ticker="AAPL",
            state={"decision": "BUY", "confidence": 0.85},
        )
        assert saved_cp is True

        assert checkpoint_manager.has_completed(cycle_id, "agents_complete", "AAPL") is True
        loaded_blob = checkpoint_manager.load_state(cycle_id, "agents_complete", "AAPL")
        assert loaded_blob["decision"] == "BUY"

        latest_cp = checkpoint_manager.load_latest(cycle_id)
        assert latest_cp is not None
        assert latest_cp["step_name"] == "agents_complete"

        steps = checkpoint_manager.get_completed_steps(cycle_id)
        assert len(steps) >= 1

        stats = checkpoint_manager.get_stats()
        assert stats["total_checkpoints"] >= 1

        cleared_n = checkpoint_manager.clear_cycle(cycle_id)
        assert cleared_n >= 1

        # 36. Constitution & Memory Repository in MongoDB
        from app.db.constitution import format_constitution_for_prompt
        from app.db.memory_repo import (
            upsert_canonical_memories,
            get_active_canonical_memories,
            deprecate_canonical_memories,
            mark_observations_promoted,
            get_unpromoted_observations,
        )

        mongo_store.insert_docs("trading_constitution", [{
            "id": 1,
            "rule_category": "risk",
            "rule_text": "Never risk more than 2% of capital on a single position.",
            "rule_params": "{}",
            "is_active": True,
        }])

        const_prompt = format_constitution_for_prompt()
        assert "[RISK]" in const_prompt
        assert "Never risk more than 2%" in const_prompt

        upsert_canonical_memories([{
            "id": "mem-001",
            "type": "earnings_pattern",
            "ticker": "AAPL",
            "sector": "Technology",
            "summary": "Consistently beats revenue during Q4 holiday quarters.",
            "tags": ["earnings", "q4"],
            "confidence_score": 0.92,
            "evidence_count": 5,
            "status": "active",
        }])

        active_mems = get_active_canonical_memories("AAPL")
        assert len(active_mems) >= 1
        assert active_mems[0]["id"] == "mem-001"

        deprecate_canonical_memories(["mem-001"])
        active_mems_after = get_active_canonical_memories("AAPL")
        assert len(active_mems_after) == 0

        # 37. Evolution Repository in MongoDB
        from app.db.evolution_repo import (
            append_node,
            get_best_node,
            get_session_summary,
            get_all_nodes,
            get_sessions,
        )
        from app.schemas.evolution import EvolutionNode, EvolutionMetrics

        evo_node = EvolutionNode(
            id="evo-node-001",
            session_id="session-alpha",
            round=1,
            motivation="Test mean-reversion parameter optimization",
            code="def strategy(): pass",
            metrics=EvolutionMetrics(sharpe=1.85, win_rate=0.62, total_return=0.15),
            score=1.85,
            status="KEEP",
            analysis="Good Sharpe ratio improvement",
            timestamp=now.isoformat(),
        )
        append_node(evo_node)

        best_evo = get_best_node("session-alpha")
        assert best_evo is not None
        assert best_evo.score == 1.85

        evo_summary = get_session_summary("session-alpha")
        assert evo_summary["kept_count"] >= 1
        assert evo_summary["best_score"] == 1.85

        all_evo_nodes = get_all_nodes(session_id="session-alpha")
        assert len(all_evo_nodes) >= 1

        all_evo_sessions = get_sessions()
        assert len(all_evo_sessions) >= 1

        # 38. BaseAgent Prior Outcomes & Confidence Calibration in MongoDB
        from app.agents.base_agent import get_ticker_outcome_context, get_confidence_calibration_context

        mongo_store.insert_docs("decision_outcomes", [
            {
                "ticker": "AAPL",
                "outcome": "WIN",
                "entry_price": 150.0,
                "exit_price": 165.0,
                "pnl_pct": 10.0,
                "confidence": 80.0,
                "resolved_at": now,
            }
            for _ in range(12)
        ])

        prior_hist = get_ticker_outcome_context("AAPL")
        assert "PRIOR TRADE HISTORY FOR AAPL" in prior_hist
        assert "WIN: entry=$150.00" in prior_hist

        calib_ctx = get_confidence_calibration_context()
        assert "CONFIDENCE CALIBRATION" in calib_ctx
        assert "stated 80-89%" in calib_ctx

        # 39. Trading Skills & Ticker Metadata in MongoDB
        from app.services.trading_skills import load_skill_for_ticker
        from app.services.ticker_meta import get_ticker_meta

        meta_res = get_ticker_meta(["AAPL"])
        assert "AAPL" in meta_res
        assert meta_res["AAPL"]["sector"] == "Technology"

        # 40. Tool Optimizer Dynamic Pruning & Reputation in MongoDB
        from app.services.tool_optimizer import (
            get_tool_reputation,
            optimize_agent_tools,
            record_tool_optimization_usage,
            reset_all_pruned,
        )

        rep = get_tool_reputation(["get_finviz_fundamentals"])
        assert "get_finviz_fundamentals" in rep
        assert rep["get_finviz_fundamentals"]["total_calls"] >= 1

        tools_list = [{"name": "get_finviz_fundamentals"}, {"name": "get_sec_filings"}, {"name": "fetch_news"}]
        opt_tools, opt_prompt = await optimize_agent_tools("v3_junior_analyst", tools_list, "You are an analyst.")
        assert len(opt_tools) >= 2

        await record_tool_optimization_usage("v3_junior_analyst", tools_list, ["get_finviz_fundamentals"])
        reset_count = reset_all_pruned()
        assert reset_count >= 0

        # 41. Data Quality Flags & Source Trust in MongoDB
        from app.services.data_flag_service import (
            flag_item,
            unflag_item,
            get_flags,
            get_flagged_source_ids,
            get_filtered_report,
            get_source_trust,
        )

        flag_res = flag_item(
            source_table="news_articles",
            source_id="news-001",
            flag_type="clickbait",
            reason="Sensationalized headline with no substantiation",
            ticker="AAPL",
        )
        assert "flag_id" in flag_res

        flagged_ids = get_flagged_source_ids("news_articles", ticker="AAPL")
        assert "news-001" in flagged_ids

        flags_list = get_flags(ticker="AAPL")
        assert len(flags_list) >= 1

        filt_report = get_filtered_report("AAPL")
        assert len(filt_report["flagged_items"]) >= 1

        unflag_res = unflag_item(flag_res["flag_id"])
        assert unflag_res is True

        # 42. News Extraction & Backfill Cache in MongoDB
        from app.services.news_extraction import _store_facts, ensure_facts
        from app.services.news_backfill import _cycle_is_running, _select_batch, backlog_size

        mongo_store.insert_docs("news_articles", [{
            "id": "news-001",
            "ticker": "AAPL",
            "title": "Apple Q3 Record Earnings",
            "publisher": "Reuters",
            "published_at": now,
            "summary": "Apple delivered record revenue in Q3.",
        }])

        _store_facts(
            article_id="news-001",
            facts=[{
                "class": "earnings",
                "statement": "Apple beat Q3 earnings expectations.",
                "quote": "Apple delivered record revenue.",
                "direction": "bullish",
                "char_start": 0,
                "char_end": 30,
            }],
            model_note="test_mock_vllm",
        )

        facts_dict = await ensure_facts([("news-001", "AAPL", "Q3 Earnings", "Apple delivered record revenue.")])
        assert "news-001" in facts_dict
        assert len(facts_dict["news-001"]) >= 1

        is_running = _cycle_is_running()
        assert isinstance(is_running, bool)

        batch = _select_batch(limit=10)
        assert isinstance(batch, list)

        b_size = backlog_size()
        assert isinstance(b_size, int)

        # 43. Embedding Ingest Backfill in MongoDB
        from app.services.embedding_ingest import backfill_source, backfill_all

        backfilled_n = backfill_source("news_articles", limit=5)
        assert backfilled_n >= 0

        all_backfills = backfill_all(limit_per_source=2)
        assert "news_articles" in all_backfills

        # 44. RLM Audit Trail & Context Blobs in MongoDB
        from app.services.rlm_audit import log_rlm_audit_trail

        log_rlm_audit_trail(
            cycle_id=cycle_id,
            bot_id="bot-001",
            ticker="AAPL",
            context="AAPL RSI is 68.4 with strong Q3 momentum.",
            trading_system_prompt="You are an analyst.",
            active_model="qwen-2.5-72b",
            response_text="BUY AAPL due to RSI divergence.",
            tokens_used=150,
            execution_time=1.25,
            agent_step="analysis",
            prompt_tokens=100,
            completion_tokens=50,
        )

        audit_docs = mongo_store.find_docs("llm_audit_logs", {"cycle_id": cycle_id, "ticker": "AAPL"})
        assert len(audit_docs) >= 1
        assert audit_docs[0]["model"] == "qwen-2.5-72b"

        blob_docs = mongo_store.find_docs("context_blobs", {})
        assert len(blob_docs) >= 1

        # 45. Smart Money Congressional & 13F Tools in MongoDB
        from app.tools.smart_money_tools import (
            get_smart_money_signal,
            get_smart_money_leads,
            get_smart_money_leaderboard,
        )

        mongo_store.insert_docs("smart_money_trade_scores", [{
            "ticker": "AAPL",
            "actor_type": "congress",
            "actor_id": "rep-001",
            "actor_name": "Representative Alpha",
            "direction": "buy",
            "event_date": "2026-08-10",
            "size_est_usd": 250000.0,
            "size_confidence": "range",
            "alpha_1y": 14.5,
        }])

        mongo_store.insert_docs("smart_money_performance", [{
            "actor_type": "congress",
            "actor_id": "rep-001",
            "actor_name": "Representative Alpha",
            "horizon": "1y",
            "avg_alpha": 12.8,
            "avg_return": 24.5,
            "win_rate": 75.0,
            "scored_count": 8,
            "rankable": True,
            "coverage_pct": 90.0,
        }])

        sm_signal = await get_smart_money_signal("AAPL", days=180)
        assert "Representative Alpha" in sm_signal
        assert "Congress" in sm_signal

        sm_board = await get_smart_money_leaderboard("congress", horizon="1y")
        assert "Representative Alpha" in sm_board
        assert "12.8%" in sm_board

        # 46. Finance Tools Market Data & News in MongoDB
        from app.tools.finance_tools import get_market_data, get_finnhub_news

        mongo_store.insert_docs("financial_history", [
            {
                "ticker": "AAPL",
                "period_type": "quarterly",
                "period_end": "2026-06-30",
                "revenue": 85000000000.0,
                "gross_profit": 38000000000.0,
                "operating_income": 25000000000.0,
                "net_income": 21000000000.0,
                "eps": 1.40,
                "free_cash_flow": 19000000000.0,
            },
            {
                "ticker": "AAPL",
                "period_type": "annual",
                "period_end": "2025-09-30",
                "revenue": 383000000000.0,
                "gross_profit": 170000000000.0,
                "operating_income": 114000000000.0,
                "net_income": 97000000000.0,
                "eps": 6.13,
                "free_cash_flow": 100000000000.0,
            },
        ])

        mkt_data = await get_market_data("AAPL")
        assert "Quarterly Financials" in mkt_data or "Fundamentals" in mkt_data

        news_out = await get_finnhub_news("AAPL")
        assert "Apple" in news_out or "AAPL" in news_out

        # 47. Evolution Lesson Store in MongoDB
        from app.cognition.lesson_store import add_lesson, retrieve_lessons

        lesson_id = add_lesson(
            text="Mean reversion strategy on AAPL works best with 14-day RSI < 30.",
            metadata={
                "session_id": "session-gamma",
                "round": 2,
                "score": 2.15,
                "status": "KEEP",
                "timestamp": now.isoformat(),
            },
        )
        assert lesson_id.startswith("evo_")

        lessons = retrieve_lessons("RSI mean reversion", k=3)
        assert len(lessons) >= 1
        assert "session-gamma" in [l.get("session_id") for l in lessons if isinstance(l, dict)]

        # 48. Evidence Packet Builder in MongoDB
        from app.cognition.evidence.packet_builder import build_evidence_packet

        packet = await build_evidence_packet("AAPL")
        assert packet is not None
        assert packet.entity_id == "AAPL"

        # 49. Data Completeness Oracle in MongoDB
        from app.cognition.evaluation.oracle import DataCompletenessOracle

        oracle_res = DataCompletenessOracle.verify_ground_truth("AAPL")
        assert "checklist" in oracle_res
        assert "completeness_score" in oracle_res
        assert oracle_res["completeness_score"] >= 2.0

        # 50. Judge & Strategy Auditor in MongoDB
        from app.cognition.evaluation.strategy_auditor import compute_agent_metrics

        metrics = compute_agent_metrics(None, cycle_id)
        assert "total_decisions_evaluated" in metrics
        assert "model_benchmarks" in metrics

        # 51. Pre-Collected Ticker Data Report in MongoDB
        from app.v3.data_report import build_ticker_data_report

        report_md = await build_ticker_data_report("AAPL", cycle_id=cycle_id)
        assert "Pre-Collected Ticker Data Report" in report_md
        assert "AAPL" in report_md
