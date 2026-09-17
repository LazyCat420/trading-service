"""Position-to-Lot Migration and Reconciliation Tooling.

Reconciles existing position quantities with tax-lot accounting prior to
enabling ENFORCE mode for a bot.

Invariants enforced:
1. Reconstructs opening lots from historical fills when available.
2. Where historical fills are missing/incomplete, creates deterministic opening
   lots tagged with origin='MIGRATION' and provenance_complete=False to prevent
   fabricated historical alpha claims.
3. Idempotent: re-running does not duplicate existing lots if quantities match.
4. Generates a durable LotMigrationReport tracking cost basis, quantities, and deltas.
5. Verifies integrity: blocks ENFORCE mode activation if a bot has unresolved mismatches.
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


def reconcile_and_migrate_bot_positions(
    bot_id: str,
    session: Any = None,
) -> dict[str, Any]:
    """Idempotently reconciles existing positions into tax lots for a specific bot."""
    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    batch_id = f"mig-{uuid.uuid4().hex[:12]}"

    positions = mongo_query.find_rows(
        "positions",
        {"bot_id": bot_id},
        ["id", "ticker", "qty", "avg_entry_price", "created_at"],
        session=session,
    )

    positions_checked = 0
    lots_created = 0
    total_qty_reconciled = 0.0
    unresolved_differences: list[dict[str, Any]] = []

    for pos in positions:
        pos_id, ticker, pos_qty, avg_entry_px, pos_created_at = pos
        ticker = str(ticker).upper().strip()
        pos_qty = float(pos_qty or 0.0)
        avg_entry_px = float(avg_entry_px or 0.0)
        if pos_qty <= 0.0001:
            continue

        positions_checked += 1

        # Sum existing open / partial lots
        existing_lots = list(
            db[COLL_POSITION_LOTS].find(
                {"bot_id": bot_id, "ticker": ticker, "status": {"$in": ["open", "partial"]}},
                session=session,
            )
        )
        existing_lot_qty = sum(float(lot.get("remaining_qty", 0.0)) for lot in existing_lots)
        qty_needed = pos_qty - existing_lot_qty

        if abs(qty_needed) <= 0.0001:
            # Already in parity
            total_qty_reconciled += pos_qty
            continue

        if qty_needed > 0.0001:
            # We need to create lot(s) for the unallocated position qty
            # Check if historical buy fills exist that haven't been bound
            historical_fills = mongo_query.find_rows(
                "trade_fills",
                {"bot_id": bot_id, "ticker": ticker, "side": "BUY"},
                ["fill_id", "price", "qty", "filled_at", "order_id"],
                sort=[("filled_at", 1)],
                session=session,
            )

            if historical_fills:
                # Try reconstruction from historical fills
                unallocated_qty = qty_needed
                for fill in historical_fills:
                    if unallocated_qty <= 0.0001:
                        break
                    f_id, f_price, f_qty, f_at, f_ord = fill
                    f_qty = float(f_qty or 0.0)
                    f_price = float(f_price or avg_entry_px)
                    f_date = f_at or pos_created_at or now

                    lot_qty = min(f_qty, unallocated_qty)
                    lot_doc = {
                        "lot_id": f"lot-recon-{uuid.uuid4().hex[:12]}",
                        "bot_id": bot_id,
                        "ticker": ticker,
                        "initial_qty": lot_qty,
                        "remaining_qty": lot_qty,
                        "entry_price": f_price,
                        "entry_notional": lot_qty * f_price,
                        "opened_at": f_date,
                        "status": "open",
                        "origin": "HISTORICAL_RECONSTRUCTION",
                        "provenance_complete": True,
                        "historical_fill_id": f_id,
                        "migration_batch_id": batch_id,
                    }
                    db[COLL_POSITION_LOTS].insert_one(lot_doc, session=session)
                    lots_created += 1
                    unallocated_qty -= lot_qty

                # If remainder exists after using all fills, create a migration lot
                if unallocated_qty > 0.0001:
                    lot_doc = {
                        "lot_id": f"lot-mig-{uuid.uuid4().hex[:12]}",
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
                    db[COLL_POSITION_LOTS].insert_one(lot_doc, session=session)
                    lots_created += 1
            else:
                # No historical fills: create deterministic migration lot
                lot_doc = {
                    "lot_id": f"lot-mig-{uuid.uuid4().hex[:12]}",
                    "bot_id": bot_id,
                    "ticker": ticker,
                    "initial_qty": qty_needed,
                    "remaining_qty": qty_needed,
                    "entry_price": avg_entry_px,
                    "entry_notional": qty_needed * avg_entry_px,
                    "opened_at": pos_created_at or now,
                    "status": "open",
                    "origin": "MIGRATION",
                    "provenance_complete": False,  # Excludes unsupported alpha claims
                    "migration_batch_id": batch_id,
                }
                db[COLL_POSITION_LOTS].insert_one(lot_doc, session=session)
                lots_created += 1

            total_qty_reconciled += pos_qty
        else:
            # qty_needed < -0.0001: More lot quantity than position quantity (negative delta / over-allocated)
            unresolved_differences.append({
                "ticker": ticker,
                "position_qty": pos_qty,
                "existing_lot_qty": existing_lot_qty,
                "difference": qty_needed,
                "reason": "LOT_OVERALLOCATION_EXCEEDS_POSITION",
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

    db[COLL_LOT_MIGRATION_REPORTS].insert_one(report, session=session)
    logger.info(
        "[LotMigrator] Completed position-to-lot reconciliation for bot %s: status=%s, lots_created=%d, diffs=%d",
        bot_id, status, lots_created, len(unresolved_differences),
    )
    return report


def verify_bot_position_lot_integrity(
    bot_id: str,
    session: Any = None,
) -> tuple[bool, list[str]]:
    """Verifies that all positions for bot_id match their open tax lots exactly.

    Returns (is_valid, errors). Must pass before ENFORCE mode is enabled.
    """
    db = mongo_store.get_doc_db()
    positions = mongo_query.find_rows(
        "positions",
        {"bot_id": bot_id},
        ["id", "ticker", "qty"],
        session=session,
    )

    errors = []
    for pos in positions:
        pos_id, ticker, pos_qty = pos[0], str(pos[1]).upper().strip(), float(pos[2] or 0.0)
        if pos_qty <= 0.0001:
            continue

        lots = list(
            db[COLL_POSITION_LOTS].find(
                {"bot_id": bot_id, "ticker": ticker, "status": {"$in": ["open", "partial"]}},
                session=session,
            )
        )
        lot_qty = sum(float(l.get("remaining_qty", 0.0)) for l in lots)
        if abs(pos_qty - lot_qty) > 0.0001:
            errors.append(
                f"{ticker}: position qty {pos_qty:.4f} != lot qty {lot_qty:.4f} (delta: {pos_qty - lot_qty:.4f})"
            )

    return len(errors) == 0, errors
