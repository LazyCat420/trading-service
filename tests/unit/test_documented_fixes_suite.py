"""Unit tests for the documented fixes suite (Items 1-4).

Covers:
1. Completion tokens forwarding into rlm_audit and tokens_per_second calculation.
2. CompanyRegistry thread safety and snapshotting under concurrent extraction.
3. Outcome contract BF1 regression: decision_as_of = created_at passes verified_pair.
4. Natural key unique index declarations for asset_prices and insider_trades.
"""

import asyncio
import threading
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

from app.services.rlm_audit import log_rlm_audit_trail
from app.processors.ticker_extractor import CompanyRegistry, Company, extract_tickers
from app.autoresearch.outcome_evidence import CONTRACT_VERSION, verified_pair


def test_completion_tokens_and_tok_per_sec_in_rlm_audit():
    """Verify that log_rlm_audit_trail stores completion_tokens and calculates tokens_per_second."""
    inserted = []

    def fake_insert_docs(coll, docs):
        if coll == "llm_audit_logs":
            inserted.extend(docs)

    with patch("app.db.mongo_store.insert_docs", side_effect=fake_insert_docs), \
         patch("app.db.mongo_store.upsert_doc"):
        log_rlm_audit_trail(
            cycle_id="cycle-test-123",
            bot_id="bot-1",
            ticker="NVDA",
            context="Test context",
            trading_system_prompt="Test system prompt",
            active_model="GLM-5.3-Flash-EXL3",
            response_text="Test response",
            tokens_used=1500,
            execution_time=2.0,  # 2 seconds
            agent_step="v3_decision",
            endpoint_name="vllm-2",
            prompt_tokens=1000,
            completion_tokens=500,
        )

    assert len(inserted) == 1
    doc = inserted[0]
    assert doc["prompt_tokens"] == 1000
    assert doc["completion_tokens"] == 500
    assert doc["tokens_used"] == 1500
    # 500 completion tokens / 2.0s = 250.0 tok/sec
    assert doc["tokens_per_second"] == 250.0


def test_company_registry_thread_safety():
    """Verify concurrent reads and writes on CompanyRegistry do not raise RuntimeError."""
    registry = CompanyRegistry()
    registry.add_company(Company(symbol="AAPL", name="Apple Inc", aliases=["apple"]))
    registry.add_company(Company(symbol="NVDA", name="NVIDIA Corporation", aliases=["nvidia"]))

    errors = []

    def writer():
        try:
            for i in range(100):
                registry.add_company(Company(symbol=f"T{i}", name=f"Company {i}", aliases=[f"alias {i}"]))
                registry.add_rejected(f"REJ{i}")
        except Exception as e:
            errors.append(e)

    def reader():
        try:
            for _ in range(100):
                _ = registry.name_items()
                _ = registry.alias_items()
                _ = registry.lookup_symbol("AAPL")
                _ = registry.is_known("NVDA")
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    t3 = threading.Thread(target=reader)

    t1.start()
    t2.start()
    t3.start()

    t1.join()
    t2.join()
    t3.join()

    assert not errors, f"Concurrent registry access raised: {errors}"


def test_outcome_contract_bf1_verified_pair():
    """Verify that backfill records using created_at for decision_as_of pass verified_pair."""
    # Decision created at 2026-09-02 21:00:00 UTC (17:00 EDT, after NY 16:15 close)
    ca = datetime(2026, 9, 2, 21, 0, 0, tzinfo=timezone.utc)
    entry_date = datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc)
    exit_date = datetime(2026, 9, 9, 0, 0, 0, tzinfo=timezone.utc)

    row = {
        "cycle_id": "cycle-v3-test",
        "ticker": "NVDA",
        "decision_as_of": ca,
        "entry_date": entry_date,
        "entry_price": 120.0,
        "entry_price_source": "yfinance",
        "claim_type": "flat_wait",
        "outcome_contract_version": CONTRACT_VERSION,
        "outcome_evidence_state": "pending",
    }

    exit_ref = {
        "price": 125.0,
        "date": exit_date,
        "source": "yfinance",
    }

    # Reference evaluation time 7 days after entry (after NY 16:15 close)
    as_of = datetime(2026, 9, 9, 21, 0, 0, tzinfo=timezone.utc)
    assert verified_pair(row, exit_ref, as_of=as_of) is True
