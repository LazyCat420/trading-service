"""Unit tests for position-to-lot migration and reconciliation tooling."""

import datetime
import pytest

from app.trading.migration.lot_migrator import (
    reconcile_and_migrate_bot_positions,
    verify_bot_position_lot_integrity,
)


def test_reconcile_and_migrate_bot_positions(monkeypatch):
    """Idempotently reconstructs lots from positions with zero drift."""
    lots_db = []
    reports_db = []

    class MockCollection:
        def __init__(self, storage):
            self.storage = storage

        def find(self, filt, session=None):
            ticker = filt.get("ticker")
            bot_id = filt.get("bot_id")
            return [x for x in self.storage if x.get("ticker") == ticker and x.get("bot_id") == bot_id]

        def insert_one(self, doc, session=None):
            self.storage.append(dict(doc))

    monkeypatch.setattr(
        "app.db.mongo_store.get_doc_db",
        lambda: {
            "position_lots": MockCollection(lots_db),
            "lot_migration_reports": MockCollection(reports_db),
        },
    )

    # Mock 2 positions: AAPL (10 shares @ 150), MSFT (5 shares @ 300)
    def mock_find_rows(table, filt, cols, **kwargs):
        if table == "positions":
            return [
                ("pos-1", "AAPL", 10.0, 150.0, datetime.datetime.now(datetime.timezone.utc)),
                ("pos-2", "MSFT", 5.0, 300.0, datetime.datetime.now(datetime.timezone.utc)),
            ]
        elif table == "trade_fills":
            return []
        return []

    monkeypatch.setattr("app.db.mongo_query.find_rows", mock_find_rows)

    # 1. First run creates migration lots
    report = reconcile_and_migrate_bot_positions("bot-test")
    assert report["status"] == "PASS"
    assert report["lots_created"] == 2
    assert len(lots_db) == 2

    # Verify lots are marked MIGRATION with provenance_complete=False
    for lot in lots_db:
        assert lot["origin"] == "MIGRATION"
        assert lot["provenance_complete"] is False

    # 2. Verify integrity passes
    ok, errors = verify_bot_position_lot_integrity("bot-test")
    assert ok is True
    assert len(errors) == 0

    # 3. Idempotent re-run creates 0 new lots
    report2 = reconcile_and_migrate_bot_positions("bot-test")
    assert report2["status"] == "PASS"
    assert report2["lots_created"] == 0
    assert len(lots_db) == 2
