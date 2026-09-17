"""Unit test suite for Step 09: Fix Realized Fee and Lot Accounting.

Verifies:
1. Benchmark specimen: 10 @ 100 + fee 1, sell @ 110 + fee 1 -> P&L 98, return 98/1001 (~9.7902%).
2. Winning, losing, and flat lot economic fixtures.
3. Strict fee conservation across sequential partial closures.
4. Strict fee and cash conservation across multi-lot FIFO closures.
5. Fees embedded in fills mode avoiding double deduction.
6. Fail-closed provenance gating for missing or incomplete lots.
7. Canonical Pydantic schema validation for PositionLot and LotClosureRecordV4.
"""

import datetime
import secrets
import pytest

from app.trading.attribution.evaluator import LotAlphaEvaluator
from app.trading.attribution.models import (
    BenchmarkSpec,
    LotClosureRecordV4,
    PositionLot,
    PriceObservation,
    AdjustmentConvention,
)
from app.trading.attribution.worker import (
    COLL_LOT_CLOSURES,
    COLL_POSITION_LOTS,
    evaluate_closed_lot_alpha_iteration,
)


def _gen_token() -> str:
    """Generate in-memory token for zero credential leakage compliance."""
    return f"token_{secrets.token_hex(8)}"


# ─────────────────────────────────────────────────────────────────────────────
# 1. Benchmark Specimen Test (Exit Gate Target)
# ─────────────────────────────────────────────────────────────────────────────
def test_benchmark_specimen_exact_reconciliation():
    """Buying 10 at 100 with fee 1, selling at 110 with fee 1 yields net P&L 98;
    return on total initial cost is 98/1001 (~9.7902%).
    """
    res = LotAlphaEvaluator.evaluate_lot_closure(
        lot_entry_price=100.0,
        lot_exit_price=110.0,
        benchmark_entry=100.0,
        benchmark_exit=105.0,
        allocated_entry_fee=1.0,
        exit_fee=1.0,
        qty=10.0,
    )

    expected_invested_capital = 1001.0  # 10 * 100 + 1
    expected_dollar_pnl = 98.0         # 10 * (110 - 100) - 1 - 1
    expected_return = round((98.0 / 1001.0) * 100.0, 4)  # 9.7902%
    expected_gross_return = 10.0
    expected_fee_drag = round(expected_gross_return - expected_return, 4)  # 0.2098%

    assert res["status"] == "MATURE"
    assert res["invested_capital_denominator"] == expected_invested_capital
    assert res["dollar_pnl"] == expected_dollar_pnl
    assert res["gross_pnl"] == 100.0
    assert res["allocated_entry_fee"] == 1.0
    assert res["exit_fee"] == 1.0
    assert res["total_fees"] == 2.0
    assert res["net_return"] == expected_return
    assert res["gross_return"] == expected_gross_return
    assert res["fee_drag_pct"] == expected_fee_drag
    assert res["benchmark_return"] == 5.0
    assert res["net_alpha"] == round(expected_return - 5.0, 4)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Winning, Losing, and Flat Lot Economic Fixtures
# ─────────────────────────────────────────────────────────────────────────────
def test_flat_trade_with_fees():
    """Flat trade (sell price == entry price): dollar P&L equals negative total fees."""
    res = LotAlphaEvaluator.evaluate_lot_closure(
        lot_entry_price=100.0,
        lot_exit_price=100.0,
        benchmark_entry=100.0,
        benchmark_exit=100.0,
        allocated_entry_fee=1.50,
        exit_fee=1.50,
        qty=10.0,
    )

    invested_cap = 1001.50
    expected_dollar_pnl = -3.0
    expected_net_return = round((-3.0 / invested_cap) * 100.0, 4)

    assert res["invested_capital_denominator"] == invested_cap
    assert res["gross_pnl"] == 0.0
    assert res["dollar_pnl"] == expected_dollar_pnl
    assert res["net_return"] == expected_net_return
    assert res["gross_return"] == 0.0
    assert res["fee_drag_pct"] == round(0.0 - expected_net_return, 4)


def test_losing_trade_with_fees():
    """Losing trade (sell price < entry price): loss compounds with fees."""
    res = LotAlphaEvaluator.evaluate_lot_closure(
        lot_entry_price=100.0,
        lot_exit_price=90.0,
        benchmark_entry=100.0,
        benchmark_exit=95.0,
        allocated_entry_fee=1.0,
        exit_fee=1.0,
        qty=10.0,
    )

    invested_cap = 1001.0
    expected_gross_pnl = -100.0
    expected_dollar_pnl = -102.0
    expected_net_return = round((-102.0 / invested_cap) * 100.0, 4)

    assert res["invested_capital_denominator"] == invested_cap
    assert res["gross_pnl"] == expected_gross_pnl
    assert res["dollar_pnl"] == expected_dollar_pnl
    assert res["net_return"] == expected_net_return
    assert res["gross_return"] == -10.0
    assert res["benchmark_return"] == -5.0
    assert res["net_alpha"] == round(expected_net_return - (-5.0), 4)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Sequential FIFO Partial Closures Fee Conservation
# ─────────────────────────────────────────────────────────────────────────────
def test_sequential_partial_fifo_fee_and_cost_conservation():
    """Verify that multiple partial sales against a single buy lot conserve
    allocated fees, remaining fees, and invested capital denominator.
    """
    total_qty = 10.0
    entry_price = 100.0
    total_entry_fee = 1.00
    initial_cost = (total_qty * entry_price) + total_entry_fee  # 1001.00

    # Step A: First partial closure (4 shares sold @ 110, exit fee 0.40)
    close_qty_1 = 4.0
    alloc_entry_fee_1 = round((close_qty_1 / total_qty) * total_entry_fee, 4)  # 0.4000
    rem_entry_fee_after_1 = round(total_entry_fee - alloc_entry_fee_1, 4)       # 0.6000

    res_1 = LotAlphaEvaluator.evaluate_lot_closure(
        lot_entry_price=entry_price,
        lot_exit_price=110.0,
        benchmark_entry=100.0,
        benchmark_exit=102.0,
        allocated_entry_fee=alloc_entry_fee_1,
        exit_fee=0.40,
        qty=close_qty_1,
    )

    assert res_1["invested_capital_denominator"] == 400.40
    assert res_1["gross_pnl"] == 40.0
    assert res_1["dollar_pnl"] == 39.20
    assert res_1["net_return"] == round((39.20 / 400.40) * 100.0, 4)

    # Step B: Second final closure (remaining 6 shares sold @ 120, exit fee 0.60)
    close_qty_2 = 6.0
    alloc_entry_fee_2 = rem_entry_fee_after_1  # Exact remaining fee: 0.6000
    rem_entry_fee_after_2 = round(rem_entry_fee_after_1 - alloc_entry_fee_2, 4)  # 0.0000

    res_2 = LotAlphaEvaluator.evaluate_lot_closure(
        lot_entry_price=entry_price,
        lot_exit_price=120.0,
        benchmark_entry=100.0,
        benchmark_exit=105.0,
        allocated_entry_fee=alloc_entry_fee_2,
        exit_fee=0.60,
        qty=close_qty_2,
    )

    assert res_2["invested_capital_denominator"] == 600.60
    assert res_2["gross_pnl"] == 120.0
    assert res_2["dollar_pnl"] == 118.80
    assert res_2["net_return"] == round((118.80 / 600.60) * 100.0, 4)

    # Invariant checks across closures
    total_alloc_entry_fees = alloc_entry_fee_1 + alloc_entry_fee_2
    assert abs(total_alloc_entry_fees - total_entry_fee) < 1e-6
    assert rem_entry_fee_after_2 == 0.0

    total_invested_capital = res_1["invested_capital_denominator"] + res_2["invested_capital_denominator"]
    assert abs(total_invested_capital - initial_cost) < 1e-6

    # Cash ledger reconciliation:
    # Outflow: 1001.00
    # Inflow 1: 4 * 110 - 0.40 = 439.60
    # Inflow 2: 6 * 120 - 0.60 = 719.40
    # Total Inflow: 1159.00
    # Net Cash P&L: 1159.00 - 1001.00 = 158.00
    total_dollar_pnl = res_1["dollar_pnl"] + res_2["dollar_pnl"]
    assert abs(total_dollar_pnl - 158.00) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# 4. Multi-Lot FIFO Closure Fee and Cost Conservation
# ─────────────────────────────────────────────────────────────────────────────
def test_multi_lot_fifo_closure_fee_and_cost_conservation():
    """Verify that a single SELL order spanning multiple BUY lots conserves
    total exit fees, apportions entry fees pro-rata, and reconciles cash flows.
    """
    # Lot 1: 5 @ 100 + fee 1.0 (cost = 501.0)
    # Lot 2: 5 @ 105 + fee 1.0 (cost = 526.0)
    # Total Outflow = 1027.0
    # Sell: 8 @ 110 + fee 2.0
    sell_qty = 8.0
    sell_fee = 2.0

    # Closure 1 (Lot 1): 5 shares (full closure of Lot 1)
    c1_qty = 5.0
    c1_alloc_entry_fee = 1.00
    c1_alloc_exit_fee = round((c1_qty / sell_qty) * sell_fee, 4)  # 1.2500
    rem_sell_fee = round(sell_fee - c1_alloc_exit_fee, 4)          # 0.7500

    res_c1 = LotAlphaEvaluator.evaluate_lot_closure(
        lot_entry_price=100.0,
        lot_exit_price=110.0,
        benchmark_entry=100.0,
        benchmark_exit=102.0,
        allocated_entry_fee=c1_alloc_entry_fee,
        exit_fee=c1_alloc_exit_fee,
        qty=c1_qty,
    )

    # Closure 2 (Lot 2): 3 shares (partial closure of Lot 2)
    c2_qty = 3.0
    c2_alloc_entry_fee = round((c2_qty / 5.0) * 1.00, 4)  # 0.6000
    c2_rem_entry_fee_lot2 = round(1.00 - c2_alloc_entry_fee, 4)  # 0.4000
    c2_alloc_exit_fee = rem_sell_fee  # Final slice absorbs exact remaining exit fee: 0.7500

    res_c2 = LotAlphaEvaluator.evaluate_lot_closure(
        lot_entry_price=105.0,
        lot_exit_price=110.0,
        benchmark_entry=100.0,
        benchmark_exit=102.0,
        allocated_entry_fee=c2_alloc_entry_fee,
        exit_fee=c2_alloc_exit_fee,
        qty=c2_qty,
    )

    # Conservation 1: Total exit fee conserved across closure records
    assert abs((c1_alloc_exit_fee + c2_alloc_exit_fee) - sell_fee) < 1e-6

    # Conservation 2: Invested capital denominator reconciliation
    assert res_c1["invested_capital_denominator"] == 501.00
    assert res_c2["invested_capital_denominator"] == 315.60
    closed_invested_capital = res_c1["invested_capital_denominator"] + res_c2["invested_capital_denominator"]
    rem_lot2_cost = (2.0 * 105.0) + c2_rem_entry_fee_lot2  # 210.40
    assert abs((closed_invested_capital + rem_lot2_cost) - 1027.00) < 1e-6

    # Conservation 3: Cash ledger reconciliation
    # Total sell proceeds: 8 * 110 - 2.0 = 878.00
    # Closed shares initial cost: 501.00 + 315.60 = 816.60
    # Expected net dollar P&L: 878.00 - 816.60 = 61.40
    total_dollar_pnl = res_c1["dollar_pnl"] + res_c2["dollar_pnl"]
    assert abs(total_dollar_pnl - 61.40) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# 5. Fees Embedded in Fills Mode
# ─────────────────────────────────────────────────────────────────────────────
def test_fees_embedded_in_fills_mode():
    """Verify that fees_embedded_in_fills=True prevents double deduction."""
    res = LotAlphaEvaluator.evaluate_lot_closure(
        lot_entry_price=100.0,
        lot_exit_price=110.0,
        benchmark_entry=100.0,
        benchmark_exit=105.0,
        allocated_entry_fee=5.0,  # Should be ignored if embedded
        exit_fee=5.0,             # Should be ignored if embedded
        qty=10.0,
        fees_embedded_in_fills=True,
    )

    assert res["invested_capital_denominator"] == 1000.0
    assert res["dollar_pnl"] == 100.0
    assert res["total_fees"] == 0.0
    assert res["net_return"] == 10.0
    assert res["fee_drag_pct"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 6. Fail-Closed Provenance Gating
# ─────────────────────────────────────────────────────────────────────────────
class MockMongoCollection:
    def __init__(self, data=None):
        self.docs = data or []
        self.updates = []

    def find(self, query=None):
        return self

    def sort(self, key, direction=1):
        return self

    def limit(self, n):
        return self.docs[:n]

    def find_one(self, query):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return doc
        return None

    def update_one(self, query, update, upsert=False):
        self.updates.append({"query": query, "update": update, "upsert": upsert})


class MockDatabase:
    def __init__(self):
        self.colls = {}

    def __getitem__(self, name):
        if name not in self.colls:
            self.colls[name] = MockMongoCollection()
        return self.colls[name]


def test_missing_lot_provenance_fails_closed_in_worker(monkeypatch):
    """If lot document is missing from COLL_POSITION_LOTS, the worker must
    fail closed: mark is_attributable=False, provenance='MISSING_LOT',
    and set exclusion_reason='MISSING_LOT_PROVENANCE'.
    """
    fake_db = MockDatabase()
    monkeypatch.setattr("app.trading.attribution.worker._get_benchmark_price", lambda s, d: 100.0)

    now = datetime.datetime.now(datetime.timezone.utc)
    closure_doc = {
        "closure_id": "close-orphan-test",
        "lot_id": "non-existent-lot-999",
        "bot_id": "bot-test",
        "ticker": "AAPL",
        "closed_qty": 10.0,
        "entry_price": 100.0,
        "exit_price": 110.0,
        "allocated_entry_fee": 1.0,
        "exit_fee": 1.0,
        "closed_at": now,
    }
    fake_db[COLL_LOT_CLOSURES].docs = [closure_doc]

    count = evaluate_closed_lot_alpha_iteration(limit=10, now=now, db=fake_db)
    assert count == 1  # Evaluated economically

    # Check lot_closure_evaluations
    eval_updates = fake_db["lot_closure_evaluations"].updates
    assert len(eval_updates) == 1
    eval_record = eval_updates[0]["update"]["$set"]

    assert eval_record["provenance"] == "MISSING_LOT"
    assert eval_record["provenance_complete"] is False
    assert eval_record["is_attributable"] is False
    assert eval_record["exclusion_reason"] == "MISSING_LOT_PROVENANCE"


def test_migration_lot_provenance_marked_unattributable_in_worker(monkeypatch):
    """If lot origin is MIGRATION or provenance_complete=False, the worker
    must mark is_attributable=False.
    """
    fake_db = MockDatabase()
    monkeypatch.setattr("app.trading.attribution.worker._get_benchmark_price", lambda s, d: 100.0)

    now = datetime.datetime.now(datetime.timezone.utc)
    lot_id = "lot-mig-test-1"
    fake_db[COLL_POSITION_LOTS].docs = [
        {
            "lot_id": lot_id,
            "bot_id": "bot-test",
            "ticker": "MSFT",
            "origin": "MIGRATION",
            "provenance_complete": False,
            "opened_at": now,
        }
    ]

    closure_doc = {
        "closure_id": "close-mig-test",
        "lot_id": lot_id,
        "bot_id": "bot-test",
        "ticker": "MSFT",
        "closed_qty": 5.0,
        "entry_price": 200.0,
        "exit_price": 210.0,
        "allocated_entry_fee": 0.0,
        "exit_fee": 1.0,
        "closed_at": now,
    }
    fake_db[COLL_LOT_CLOSURES].docs = [closure_doc]

    count = evaluate_closed_lot_alpha_iteration(limit=10, now=now, db=fake_db)
    assert count == 1

    eval_record = fake_db["lot_closure_evaluations"].updates[0]["update"]["$set"]
    assert eval_record["provenance"] == "MIGRATION"
    assert eval_record["provenance_complete"] is False
    assert eval_record["is_attributable"] is False
    assert eval_record["exclusion_reason"] == "INCOMPLETE_PROVENANCE"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Canonical Pydantic Schema Validation
# ─────────────────────────────────────────────────────────────────────────────
def test_position_lot_model_validation():
    """PositionLot model validates strictly and enforces invariants."""
    now = datetime.datetime.now(datetime.timezone.utc)
    lot = PositionLot(
        lot_id="lot-model-test",
        bot_id="bot-1",
        ticker="GOOG",
        initial_qty=10.0,
        remaining_qty=10.0,
        entry_price=150.0,
        entry_notional=1501.0,
        entry_fee=1.0,
        remaining_entry_fee=1.0,
        opened_at=now,
    )
    assert lot.lot_id == "lot-model-test"
    assert lot.entry_fee == 1.0
    assert lot.status == "open"
    assert lot.origin == "LIVE"
    assert lot.provenance_complete is True

    # Validation rejection on negative quantity
    with pytest.raises(Exception):
        PositionLot(
            lot_id="lot-invalid",
            bot_id="bot-1",
            ticker="GOOG",
            initial_qty=-5.0,
            remaining_qty=5.0,
            entry_price=150.0,
            entry_notional=750.0,
            opened_at=now,
        )


def test_lot_closure_record_v4_roundtrip():
    """LotClosureRecordV4 roundtrips all realized accounting attributes."""
    now = datetime.datetime.now(datetime.timezone.utc)
    bm_spec = BenchmarkSpec.resolve("AAPL")
    obs_entry = PriceObservation(
        source="ALPACA_BARS",
        date=now,
        price=500.0,
    )
    obs_exit = PriceObservation(
        source="ALPACA_BARS",
        date=now,
        price=510.0,
    )

    record = LotClosureRecordV4(
        closure_id="close-v4-test",
        lot_id="lot-v4-test",
        bot_id="bot-1",
        ticker="AAPL",
        closed_qty=10.0,
        entry_price=100.0,
        exit_price=110.0,
        allocated_entry_fee=1.0,
        exit_fee=1.0,
        invested_capital_denominator=1001.0,
        dollar_pnl=98.0,
        net_realized_return=9.7902,
        benchmark_spec=bm_spec,
        benchmark_entry=obs_entry,
        benchmark_exit=obs_exit,
        benchmark_return=2.0,
        realized_net_alpha=7.7902,
        opened_at=now,
        closed_at=now,
    )

    dumped = record.model_dump()
    reloaded = LotClosureRecordV4.model_validate(dumped)
    assert reloaded.closure_id == "close-v4-test"
    assert reloaded.dollar_pnl == 98.0
    assert reloaded.invested_capital_denominator == 1001.0
    assert reloaded.net_realized_return == 9.7902
