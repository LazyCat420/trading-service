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


# ── Phase 1: fallback gate ──────────────────────────────────────────────────

def test_pg_fallback_allowed_tracks_writes_pg(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "a:pg,b:dual,c:mongo_read,d:mongo")
    assert ms.pg_fallback_allowed("a") is True
    assert ms.pg_fallback_allowed("b") is True
    assert ms.pg_fallback_allowed("c") is True
    assert ms.pg_fallback_allowed("d") is False     # PG stale — no fallback
    assert ms.pg_fallback_allowed("unlisted") is True


def test_handle_mongo_read_failure_logs_and_returns_while_pg_fresh(monkeypatch, caplog):
    ms = _reload_with_backend(monkeypatch, "t:mongo_read")
    err = RuntimeError("mongo down")
    with caplog.at_level("WARNING"):
        ms.handle_mongo_read_failure("t", "[test] read", err)   # must NOT raise
    assert any("PG fallback" in r.message for r in caplog.records)


def test_handle_mongo_read_failure_raises_at_full_mongo(monkeypatch, caplog):
    """THE cutover-hazard test: at mode `mongo` the PG table is stale, so the
    reader must fail loudly instead of silently serving old rows."""
    ms = _reload_with_backend(monkeypatch, "t:mongo")
    err = RuntimeError("mongo down")
    with caplog.at_level("CRITICAL"):
        with pytest.raises(RuntimeError):
            ms.handle_mongo_read_failure("t", "[test] read", err)
    assert any("STALE" in r.message for r in caplog.records)


def test_forced_outage_on_mongo_mode_table_raises_through_a_real_reader(monkeypatch):
    """End-to-end through a real converted call site: cycle_replay_router's
    trade-actions reader with Mongo forced down must raise immediately."""
    ms = _reload_with_backend(monkeypatch, "trade_results:mongo")
    import app.routers.cycle_replay_router as crr
    crr = importlib.reload(crr)
    boom = RuntimeError("forced outage")
    monkeypatch.setattr(ms, "find_docs", lambda *a, **k: (_ for _ in ()).throw(boom))

    with pytest.raises(RuntimeError):
        crr._trade_actions("cycle-x")


# ── Phase 1: new helpers (pure logic, mocked collection) ────────────────────

class _FakeColl:
    def __init__(self):
        self.calls = []

    def delete_many(self, query, session=None):
        self.calls.append(("delete_many", query))

        class _R:
            deleted_count = 3
        return _R()

    def update_many(self, query, update, upsert=False, session=None):
        self.calls.append(("update_many", query, update, upsert))

        class _R:
            modified_count = 2
        return _R()

    def find_one_and_update(self, query, update, sort=None, upsert=False,
                            return_document=None, session=None):
        self.calls.append(("find_one_and_update", query, update, sort))
        return {"id": 1, "status": "running"}


def _fake_db(monkeypatch, ms):
    coll = _FakeColl()
    monkeypatch.setattr(ms, "get_doc_db", lambda: {"q": coll})
    return coll


def test_delete_docs_refuses_empty_query(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "")
    with pytest.raises(ValueError):
        ms.delete_docs("q", {})


def test_delete_docs_returns_count(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "")
    coll = _fake_db(monkeypatch, ms)
    assert ms.delete_docs("q", {"id": 1}) == 3
    assert coll.calls[0] == ("delete_many", {"id": 1})


def test_update_docs_wraps_plain_doc_in_set(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "")
    coll = _fake_db(monkeypatch, ms)
    assert ms.update_docs("q", {"id": 1}, {"status": "done"}) == 2
    assert coll.calls[0][2] == {"$set": {"status": "done"}}


def test_update_docs_passes_operator_updates_through(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "")
    coll = _fake_db(monkeypatch, ms)
    ms.update_docs("q", {"id": 1}, {"$inc": {"n": 1}})
    assert coll.calls[0][2] == {"$inc": {"n": 1}}


def test_find_one_and_update_is_the_claim_primitive(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "")
    coll = _fake_db(monkeypatch, ms)
    doc = ms.find_one_and_update("q", {"status": "pending"}, {"status": "running"},
                                 sort=[("created_at", 1)])
    assert doc == {"id": 1, "status": "running"}
    name, query, update, sort = coll.calls[0]
    assert query == {"status": "pending"}
    assert update == {"$set": {"status": "running"}}
    assert sort == [("created_at", 1)]


def test_dec128_never_goes_through_float_arithmetic(monkeypatch):
    ms = _reload_with_backend(monkeypatch, "")
    from bson import Decimal128
    from decimal import Decimal
    assert ms.dec128(10000.1).to_decimal() == Decimal("10000.1")
    assert ms.dec128(Decimal("0.30")).to_decimal() == Decimal("0.30")
    assert ms.dec128("42.42").to_decimal() == Decimal("42.42")
    already = Decimal128("7.77")
    assert ms.dec128(already) is already
