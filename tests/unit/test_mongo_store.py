"""
Unit tests for the Postgres→Mongo document-store backend flags and shape
mapping (app/db/mongo_store.py). No live Mongo needed — the flag logic and the
read-shape transform are pure.
"""
import importlib

import pytest


def _reload_with_backend(monkeypatch, value):
    """Reimport mongo_store with MONGO_STORE_BACKEND set, so the module-level
    _BACKENDS parse runs against `value`."""
    monkeypatch.setenv("MONGO_STORE_BACKEND", value)
    import app.db.mongo_store as ms
    return importlib.reload(ms)


def test_default_backend_is_pg(monkeypatch):
    monkeypatch.delenv("MONGO_STORE_BACKEND", raising=False)
    ms = _reload_with_backend(monkeypatch, "")
    assert ms.backend_for("pipeline_events") == "pg"
    assert not ms.writes_mongo("pipeline_events")
    assert not ms.reads_mongo("pipeline_events")


def test_dual_writes_both_reads_pg(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "pipeline_events:dual")
    assert ms.backend_for("pipeline_events") == "dual"
    assert ms.writes_mongo("pipeline_events") is True   # write both
    assert ms.reads_mongo("pipeline_events") is False   # still read PG during soak


def test_mongo_mode_writes_and_reads_mongo(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "pipeline_events:mongo")
    assert ms.writes_mongo("pipeline_events") is True
    assert ms.reads_mongo("pipeline_events") is True


def test_unlisted_table_stays_pg(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "pipeline_events:mongo")
    assert ms.backend_for("trade_results") == "pg"
    assert not ms.writes_mongo("trade_results")


def test_bad_mode_ignored(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "pipeline_events:garbage,trade_results:dual")
    assert ms.backend_for("pipeline_events") == "pg"     # bad mode dropped → default
    assert ms.backend_for("trade_results") == "dual"


def test_multiple_tables_parsed(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "pipeline_events:dual, trade_results:mongo ")
    assert ms.backend_for("pipeline_events") == "dual"
    assert ms.backend_for("trade_results") == "mongo"


def test_read_pipeline_events_shape(monkeypatch):
    """read_pipeline_events maps Mongo docs to the exact dict shape the PG path
    produces (ts isoformat, data dict, elapsed_ms int)."""
    from datetime import datetime, timezone
    ms = _reload_with_backend(monkeypatch, "")
    ts = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    fake_docs = [
        {"id": "evt_1", "cycle_id": "c1", "timestamp": ts, "phase": "discovery",
         "step": "scraper_start", "detail": "go", "status": "running",
         "data": {"k": 1}, "elapsed_ms": 12},
    ]
    monkeypatch.setattr(ms, "find_docs", lambda *a, **k: fake_docs)
    out = ms.read_pipeline_events("c1")
    assert out == [{
        "ts": ts.isoformat(), "phase": "discovery", "step": "scraper_start",
        "detail": "go", "status": "running", "data": {"k": 1}, "elapsed_ms": 12,
    }]


def test_read_pipeline_events_handles_missing_fields(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "")
    monkeypatch.setattr(ms, "find_docs", lambda *a, **k: [{"id": "e", "cycle_id": "c"}])
    out = ms.read_pipeline_events("c")
    assert out[0]["ts"] is None and out[0]["data"] == {} and out[0]["elapsed_ms"] == 0
