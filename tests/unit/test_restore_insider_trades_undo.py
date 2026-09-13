"""The restore path. It exists because the asset_prices undo file has no
consumer anywhere in `scripts/` — the word "restores" in its docstring was
never backed by code."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from bson import ObjectId, json_util

import scripts.restore_insider_trades_undo as restore


class FakeCollection:
    def __init__(self, docs=()):
        self.docs = list(docs)
        self.name = "insider_trades"

    def count_documents(self, spec):
        if spec == {}:
            return len(self.docs)
        ids = set(spec["_id"]["$in"])
        return sum(1 for d in self.docs if d["_id"] in ids)

    def insert_many(self, docs, ordered=False):
        existing = {d["_id"] for d in self.docs}
        for d in docs:
            if d["_id"] in existing:
                raise RuntimeError("E11000 duplicate key error: _id")
        self.docs.extend(docs)

        class R:
            inserted_ids = [d["_id"] for d in docs]

        return R()


def _undo_file(tmp_path: Path) -> Path:
    import datetime

    docs = [
        {"_id": ObjectId("00000000000000000000000%d" % i), "id": "a", "ticker": "AAPL",
         "trade_date": datetime.datetime(2026, 5, 14)}
        for i in (2, 3)
    ]
    p = tmp_path / "u.json"
    p.write_text(json_util.dumps(
        {"table": "insider_trades", "count": len(docs), "documents": docs}, indent=1))
    return p


@pytest.fixture
def coll(monkeypatch):
    c = FakeCollection([{"_id": ObjectId("000000000000000000000001"), "id": "a"}])
    monkeypatch.setattr(restore, "_collection", lambda: c)
    return c


def test_dry_run_by_default(coll, tmp_path, capsys):
    rc = restore.main([str(_undo_file(tmp_path))])
    assert rc == 0
    assert len(coll.docs) == 1, "restore must not write without --apply"
    assert "DRY RUN" in capsys.readouterr().out


def test_apply_reinserts_with_the_original_objectids(coll, tmp_path):
    rc = restore.main([str(_undo_file(tmp_path)), "--apply"])
    assert rc == 0
    assert len(coll.docs) == 3
    restored = [d for d in coll.docs if d["_id"] != ObjectId("000000000000000000000001")]
    assert all(isinstance(d["_id"], ObjectId) for d in restored)
    import datetime
    assert all(isinstance(d["trade_date"], datetime.datetime) for d in restored)


def test_already_present_ids_are_refused_not_doubled(coll, tmp_path, capsys):
    """The asset_prices failure mode: a str `_id` cannot collide, so a partial
    restore silently doubles the collection. Here a present `_id` is detected
    BEFORE any write and the run refuses."""
    restore.main([str(_undo_file(tmp_path)), "--apply"])
    rc = restore.main([str(_undo_file(tmp_path)), "--apply"])
    assert rc != 0
    assert len(coll.docs) == 3, "no partial double-insert"
    assert "already present" in capsys.readouterr().out.lower()


def test_rejects_an_undo_file_for_another_collection(coll, tmp_path):
    p = tmp_path / "other.json"
    p.write_text(json_util.dumps({"table": "asset_prices", "count": 0, "documents": []}))
    assert restore.main([str(p), "--apply"]) != 0


def test_rejects_a_plain_json_dumps_undo_file(coll, tmp_path):
    """A file written with json.dumps(default=str) has string _ids. Restoring
    it would create documents that can never match the originals."""
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"table": "insider_trades", "count": 1, "documents": [
        {"_id": "000000000000000000000002", "id": "a"}]}))
    rc = restore.main([str(p), "--apply"])
    assert rc != 0
    assert len(coll.docs) == 1
