"""Position-to-Lot Migration and Reconciliation Tooling.

Reconciles existing position quantities with tax-lot accounting prior to
enabling ENFORCE mode for a bot.

Invariants enforced:
1. Reconstructs opening lots from historical fills by replaying both BUY and SELL
   fills in chronological order (FIFO), accounting for existing allocations.
2. Where historical fills are missing/incomplete, creates deterministic opening
   lots tagged with origin='MIGRATION' and provenance_complete=False to prevent
   fabricated historical alpha claims.
3. Deterministic lot identities: repeat runs or interrupted migrations do not
   create duplicates.
4. Compares quantities and cost basis, identifying orphan lots for zero-position symbols.
5. Enforces integrity checks at promotion and execution boundaries.
6. Operates transactionally to prevent partial state application.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, Optional

from app.db import mongo_query, mongo_store
from app.trading.attribution.repository import COLL_POSITION_LOTS

logger = logging.getLogger(__name__)

COLL_LOT_MIGRATION_REPORTS = "lot_migration_reports"


class CandidateBuyLot:
    def __init__(self, fill_id: str, price: float, qty: float, filled_at: Any):
        self.fill_id = fill_id
        self.price = price
        self.qty = qty
        self.remaining_qty = qty
        self.filled_at = filled_at


def _replay_historical_fills_fifo(
    bot_id: str,
    ticker: str,
    session: Any = None,
) -> list[CandidateBuyLot]:
    """Replays all historical BUY and SELL fills in FIFO order to determine surviving lots."""
    all_fills = mongo_query.find_rows(
        "trade_fills",
        {"bot_id": bot_id, "ticker": ticker},
        ["fill_id", "side", "price", "qty", "filled_at", "order_id"],
        sort=[("filled_at", 1), ("_id", 1)],
        session=session,
    )

    open_candidate_lots: list[CandidateBuyLot] = []
    for fill in all_fills:
        f_id, f_side, f_px, f_qty, f_at, f_ord = fill
        f_side = str(f_side).upper().strip()
        f_qty = float(f_qty or 0.0)
        f_px = float(f_px or 0.0)
        if f_qty <= 0:
            continue

        if f_side == "BUY":
            open_candidate_lots.append(CandidateBuyLot(f_id, f_px, f_qty, f_at))
        elif f_side == "SELL":
            sell_rem = f_qty
            for c_lot in open_candidate_lots:
                if sell_rem <= 0:
                    break
                if c_lot.remaining_qty <= 0:
                    continue
                deplete = min(c_lot.remaining_qty, sell_rem)
                c_lot.remaining_qty -= deplete
                sell_rem -= deplete

    return [lot for lot in open_candidate_lots if lot.remaining_qty > 0.0001]


def reconcile_and_migrate_bot_positions(
    bot_id: str,
    session: Any = None,
) -> dict[str, Any]:
    """Idempotently reconciles existing positions into tax lots for a specific bot."""
    def _reconcile_op(s):
        db = mongo_store.get_doc_db()
        now = datetime.datetime.now(datetime.timezone.utc)
        batch_id = f"mig-{uuid.uuid4().hex[:12]}"

        positions = mongo_query.find_rows(
            "positions",
            {"bot_id": bot_id},
            ["id", "ticker", "qty", "avg_entry_price", "created_at"],
            session=s,
        )

        positions_checked = 0
        lots_created = 0
        total_qty_reconciled = 0.0
        unresolved_differences: list[dict[str, Any]] = []
        seen_tickers: set[str] = set()

        for pos in positions:
            pos_id, ticker, pos_qty, avg_entry_px, pos_created_at = pos
            ticker = str(ticker).upper().strip()
            seen_tickers.add(ticker)
            pos_qty = float(pos_qty or 0.0)
            avg_entry_px = float(avg_entry_px or 0.0)

            # Check existing open / partial lots
            existing_lots = list(
                db[COLL_POSITION_LOTS].find(
                    {"bot_id": bot_id, "ticker": ticker, "status": {"$in": ["open", "partial"]}},
                    session=s,
                )
            )
            existing_lot_qty = sum(float(lot.get("remaining_qty", 0.0)) for lot in existing_lots)

            if pos_qty <= 0.0001:
                if existing_lot_qty > 0.0001:
                    unresolved_differences.append({
                        "ticker": ticker,
                        "position_qty": pos_qty,
                        "existing_lot_qty": existing_lot_qty,
                        "difference": -existing_lot_qty,
                        "reason": "ORPHAN_LOTS_ZERO_POSITION",
                    })
                continue

            positions_checked += 1
            qty_needed = pos_qty - existing_lot_qty

            if abs(qty_needed) <= 0.0001:
                # Quantities match. Verify cost basis
                lot_cost_sum = sum(float(l.get("remaining_qty", 0.0)) * float(l.get("entry_price", 0.0)) for l in existing_lots)
                weighted_lot_price = lot_cost_sum / existing_lot_qty if existing_lot_qty > 0 else 0.0
                if abs(weighted_lot_price - avg_entry_px) > 0.05:
                    unresolved_differences.append({
                        "ticker": ticker,
                        "position_cost_basis": avg_entry_px,
                        "lot_cost_basis": weighted_lot_price,
                        "reason": "COST_BASIS_MISMATCH",
                    })
                total_qty_reconciled += pos_qty
                continue

            if qty_needed > 0.0001:
                # Need to allocate additional lots
                surviving_fills = _replay_historical_fills_fifo(bot_id, ticker, session=s)

                # Identify which fill_ids are already allocated in existing_lots
                allocated_fill_ids = {
                    lot.get("historical_fill_id")
                    for lot in existing_lots
                    if lot.get("historical_fill_id")
                }

                unallocated_qty = qty_needed
                for c_lot in surviving_fills:
                    if unallocated_qty <= 0.0001:
                        break
                    if c_lot.fill_id in allocated_fill_ids:
                        continue

                    lot_qty = min(c_lot.remaining_qty, unallocated_qty)
                    # Deterministic lot_id based on bot, ticker, and fill_id
                    lot_id = f"lot-recon-{bot_id}-{ticker}-{c_lot.fill_id}"
                    
                    # Upsert to guarantee idempotency across retries or interrupted runs
                    existing_lot = db[COLL_POSITION_LOTS].find_one({"lot_id": lot_id}, session=s)
                    if not existing_lot:
                        lot_doc = {
                            "lot_id": lot_id,
                            "bot_id": bot_id,
                            "ticker": ticker,
                            "initial_qty": lot_qty,
                            "remaining_qty": lot_qty,
                            "entry_price": c_lot.price,
                            "entry_notional": lot_qty * c_lot.price,
                            "opened_at": c_lot.filled_at or pos_created_at or now,
                            "status": "open",
                            "origin": "HISTORICAL_RECONSTRUCTION",
                            "provenance_complete": True,
                            "historical_fill_id": c_lot.fill_id,
                            "migration_batch_id": batch_id,
                        }
                        db[COLL_POSITION_LOTS].insert_one(lot_doc, session=s)
                        lots_created += 1

                    unallocated_qty -= lot_qty

                # If remainder exists after using all valid historical fills, create a migration lot
                if unallocated_qty > 0.0001:
                    lot_id = f"lot-mig-{bot_id}-{ticker}-rem"
                    existing_lot = db[COLL_POSITION_LOTS].find_one({"lot_id": lot_id}, session=s)
                    if not existing_lot:
                        lot_doc = {
                            "lot_id": lot_id,
                            "bot_id": bot_id,
                            "ticker": ticker,
                            "initial_qty": unallocated_qty,
                            "remaining_qty": unallocated_qty,
                            "entry_price": avg_entry_px,
                            "entry_notional": unallocated_qty * avg_entry_px,
                            "opened_at": pos_created_at or now,
                            "status": "open",
                            "origin": "MIGRATION",
                            "provenance_complete": False,  # Excludes unsupported alpha claims
                            "migration_batch_id": batch_id,
                        }
                        db[COLL_POSITION_LOTS].insert_one(lot_doc, session=s)
                        lots_created += 1

                total_qty_reconciled += pos_qty
            else:
                # qty_needed < -0.0001: Overallocated
                unresolved_differences.append({
                    "ticker": ticker,
                    "position_qty": pos_qty,
                    "existing_lot_qty": existing_lot_qty,
                    "difference": qty_needed,
                    "reason": "LOT_OVERALLOCATION_EXCEEDS_POSITION",
                })

        # Check for orphan lots on tickers with no row in positions table
        all_bot_lots = list(
            db[COLL_POSITION_LOTS].find(
                {"bot_id": bot_id, "status": {"$in": ["open", "partial"]}},
                session=s,
            )
        )
        for lot in all_bot_lots:
            lot_ticker = str(lot.get("ticker", "")).upper().strip()
            if lot_ticker not in seen_tickers and float(lot.get("remaining_qty", 0.0)) > 0.0001:
                unresolved_differences.append({
                    "ticker": lot_ticker,
                    "lot_id": lot.get("lot_id"),
                    "remaining_qty": float(lot.get("remaining_qty", 0.0)),
                    "reason": "ORPHAN_LOT_NO_POSITION_RECORD",
                })

        status = "PASS" if not unresolved_differences else "MISMATCH_BLOCKED"

        report = {
            "report_id": f"mig-rep-{uuid.uuid4().hex[:12]}",
            "bot_id": bot_id,
            "migration_batch_id": batch_id,
            "status": status,
            "reconciled_at": now,
            "positions_checked": positions_checked,
            "lots_created": lots_created,
            "total_qty_reconciled": total_qty_reconciled,
            "unresolved_differences": unresolved_differences,
        }

        db[COLL_LOT_MIGRATION_REPORTS].insert_one(report, session=s)
        logger.info(
            "[LotMigrator] Completed position-to-lot reconciliation for bot %s: status=%s, lots_created=%d, diffs=%d",
            bot_id, status, lots_created, len(unresolved_differences),
        )
        return report

    if session is not None:
        return _reconcile_op(session)
    with mongo_store.with_txn() as s:
        return _reconcile_op(s)


def verify_bot_position_lot_integrity(
    bot_id: str,
    session: Any = None,
) -> tuple[bool, list[str]]:
    """Verifies that all positions for bot_id match their open tax lots exactly.

    Checks:
    1. Quantity parity for each open position.
    2. Cost basis parity.
    3. Absence of orphan open lots for zero-position or non-existent positions.
    Returns (is_valid, errors). Must pass before ENFORCE mode is enabled.
    """
    db = mongo_store.get_doc_db()
    positions = mongo_query.find_rows(
        "positions",
        {"bot_id": bot_id},
        ["id", "ticker", "qty", "avg_entry_price"],
        session=session,
    )

    errors: list[str] = []
    seen_tickers: set[str] = set()

    for pos in positions:
        ticker = str(pos[1]).upper().strip()
        seen_tickers.add(ticker)
        pos_qty = float(pos[2] or 0.0)
        avg_entry_px = float(pos[3] or 0.0)

        lots = list(
            db[COLL_POSITION_LOTS].find(
                {"bot_id": bot_id, "ticker": ticker, "status": {"$in": ["open", "partial"]}},
                session=session,
            )
        )
        lot_qty = sum(float(l.get("remaining_qty", 0.0)) for l in lots)

        if pos_qty <= 0.0001:
            if lot_qty > 0.0001:
                errors.append(f"{ticker}: zero position but open lot quantity exists ({lot_qty:.4f})")
            continue

        if abs(pos_qty - lot_qty) > 0.0001:
            errors.append(
                f"{ticker}: position qty {pos_qty:.4f} != lot qty {lot_qty:.4f} (delta: {pos_qty - lot_qty:.4f})"
            )
        elif lot_qty > 0.0001 and avg_entry_px > 0:
            lot_cost_sum = sum(float(l.get("remaining_qty", 0.0)) * float(l.get("entry_price", 0.0)) for l in lots)
            weighted_lot_px = lot_cost_sum / lot_qty
            if abs(weighted_lot_px - avg_entry_px) > 0.05:
                errors.append(
                    f"{ticker}: cost basis mismatch: position avg {avg_entry_px:.4f} != lots weighted avg {weighted_lot_px:.4f}"
                )

    # Check for orphan lots for symbols without any position record
    all_open_lots = list(
        db[COLL_POSITION_LOTS].find(
            {"bot_id": bot_id, "status": {"$in": ["open", "partial"]}},
            session=session,
        )
    )
    for lot in all_open_lots:
        lot_ticker = str(lot.get("ticker", "")).upper().strip()
        rem_qty = float(lot.get("remaining_qty", 0.0))
        if lot_ticker not in seen_tickers and rem_qty > 0.0001:
            errors.append(f"{lot_ticker}: orphan open lot {lot.get('lot_id')} ({rem_qty:.4f} shares) with no position record")

    return len(errors) == 0, errors


def promote_bot_to_enforce(
    bot_id: str,
    session: Any = None,
) -> dict[str, Any]:
    """Validates bot position-lot integrity and promotes bot to ENFORCE mode.

    Fails closed if any integrity mismatches exist.
    Updates db['bots'] by bot_id (NOT id).
    """
    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)

    # Verify integrity first
    is_valid, errors = verify_bot_position_lot_integrity(bot_id, session=session)
    if not is_valid:
        raise ValueError(
            f"Cannot promote bot {bot_id} to ENFORCE: integrity violations present: {'; '.join(errors)}"
        )

    res = db["bots"].update_one(
        {"bot_id": bot_id},
        {"$set": {"control_plane_mode": "ENFORCE", "promoted_at": now}},
        session=session,
    )
    if res.matched_count == 0:
        raise ValueError(f"Bot {bot_id} not found in database for promotion")

    logger.info("[LotMigrator] Successfully promoted bot %s to ENFORCE mode", bot_id)
    return {
        "bot_id": bot_id,
        "status": "PROMOTED",
        "control_plane_mode": "ENFORCE",
        "promoted_at": now,
    }
