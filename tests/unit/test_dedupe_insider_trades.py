"""The dedupe script, its index build, and its undo file.

Every test here exists because `scripts/dedupe_asset_prices.py` — the working
precedent — gets the same thing wrong. See the module docstring of
`scripts/dedupe_insider_trades.py` for the four defects.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from bson import ObjectId

import scripts.dedupe_insider_trades as dedupe

REPO = Path(__file__).resolve().parents[2]


class FakeCollection:
    """Enough of a pymongo Collection to drive the script.

    `create_index` RAISES on a same-key/different-options build, which is what
    a real server does and what the asset_prices script never handled.
    """

    def __init__(self, docs, indexes=None):
        self.docs = list(docs)
        self.name = "insider_trades"
        self.indexes = dict(indexes or {"_id_": {"key": [("_id", 1)]}})
        self.dropped = []
        self.created = []

    def count_documents(self, _filter):
        return len(self.docs)

    def estimated_document_count(self):  # pragma: no cover - must not be used
        raise AssertionError("estimated_document_count is approximate; use count_documents")

    def find(self, _filter, sort=None):
        docs = list(self.docs)
        if sort:
            for field, direction in reversed(sort):
                docs.sort(key=lambda d: str(d.get(field)), reverse=direction < 0)
        return iter(docs)

    def delete_many(self, spec):
        ids = set(spec["_id"]["$in"])
        before = len(self.docs)
        self.docs = [d for d in self.docs if d["_id"] not in ids]

        class R:
            deleted_count = before - len(self.docs)

        return R()

    def index_information(self):
        return dict(self.indexes)

    def list_indexes(self):
        return [dict(spec, name=name) for name, spec in self.indexes.items()]

    def drop_index(self, name):
        self.dropped.append(name)
        self.indexes.pop(name, None)

    def create_index(self, keys, name=None, unique=False, **kw):
        keys = [tuple(k) for k in keys]
        for existing_name, spec in self.indexes.items():
            if [tuple(k) for k in spec["key"]] == keys:
                if bool(spec.get("unique")) != bool(unique) or existing_name != name:
                    raise RuntimeError(
                        "IndexOptionsConflict: An existing index has the same "
                        f"key pattern but different options: {existing_name}"
                    )
        if unique:
            seen = set()
            for d in self.docs:
                k = tuple(d.get(f) for f, _ in keys)
                if k in seen:
                    raise RuntimeError("E11000 duplicate key error")
                seen.add(k)
        self.indexes[name] = {"key": keys, "unique": unique}
        self.created.append(name)
        return name


def _docs():
    """Three ids: one with 3 copies (only the first carries collected_at),
    one with 2 identical copies, one singleton."""
    import datetime

    return [
        {"_id": ObjectId("000000000000000000000001"), "id": "a", "ticker": "AAPL",
         "qty": 10, "trade_date": datetime.datetime(2026, 5, 14),
         "collected_at": datetime.datetime(2026, 5, 15, 11, 30)},
        {"_id": ObjectId("000000000000000000000002"), "id": "a", "ticker": "AAPL",
         "qty": 10, "trade_date": datetime.datetime(2026, 5, 14)},
        {"_id": ObjectId("000000000000000000000003"), "id": "a", "ticker": "AAPL",
         "qty": 10, "trade_date": datetime.datetime(2026, 5, 14)},
        {"_id": ObjectId("000000000000000000000004"), "id": "b", "ticker": "MSFT",
         "qty": 5, "trade_date": datetime.datetime(2026, 5, 14)},
        {"_id": ObjectId("000000000000000000000005"), "id": "b", "ticker": "MSFT",
         "qty": 5, "trade_date": datetime.datetime(2026, 5, 14)},
        {"_id": ObjectId("000000000000000000000006"), "id": "c", "ticker": "NVDA",
         "qty": 1, "trade_date": datetime.datetime(2026, 5, 14)},
    ]


@pytest.fixture
def coll(monkeypatch):
    c = FakeCollection(_docs(), {
        "_id_": {"key": [("_id", 1)]},
        # the live shape: a NON-unique index on the natural key
        "natural_key": {"key": [("id", 1)], "background": True},
    })
    monkeypatch.setattr(dedupe, "_collection", lambda: c)
    return c


# ── 4. the collection name comes from the mapping helper, not a literal ────
def test_collection_is_resolved_through_the_mapping_helper():
    """`get_doc_db()["insider_trades"]` bypasses collection_for(), which is the
    one thing mongo_store._coll exists to forbid: a name that skips the
    resolver does not error, it silently opens a second collection."""
    src = (REPO / "scripts" / "dedupe_insider_trades.py").read_text()
    tree = ast.parse(src)
    # AST, not a substring: the docstring names the forbidden form in order to
    # explain it, and a grep-shaped check cannot tell prose from code.
    literals = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Subscript)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "get_doc_db"
        and isinstance(n.slice, ast.Constant)
    ]
    assert not literals, f"collection named by literal at line {literals and literals[0].lineno}"
    assert "collection_for" in {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }


# ── 3. no estimated_document_count gating an exact equality check ──────────
def test_uses_exact_counts_not_estimated(coll):
    """FakeCollection.estimated_document_count raises; if the script calls it
    the test fails. An estimate compared for EQUALITY against an exact
    distinct-key count reports a spurious mismatch (or hides a real one)."""
    assert dedupe.main([]) == 0


# ── population report, dry run ─────────────────────────────────────────────
def test_dry_run_reports_population_and_deletes_nothing(coll, capsys):
    rc = dedupe.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(coll.docs) == 6, "dry run must not delete"
    assert "documents" in out and "6" in out
    assert "distinct" in out and "3" in out
    assert "redundant" in out
    assert "DRY RUN" in out
    assert "worst" in out.lower()


def test_nothing_to_do_exits_nonzero_distinctly(monkeypatch, capsys):
    """A clean collection is not an error worth a traceback, but it IS a
    different outcome from "deduped 2090" and the operator must be able to
    tell them apart from the exit code alone."""
    monkeypatch.setattr(dedupe, "_collection", lambda: FakeCollection(
        [_docs()[0], _docs()[3], _docs()[5]],
        {"_id_": {"key": [("_id", 1)]}, "natural_key": {"key": [("id", 1)]}}))
    rc = dedupe.main([])
    assert rc == dedupe.EXIT_NOTHING_TO_DO
    assert rc != 0
    assert "nothing to do" in capsys.readouterr().out.lower()


# ── survivor rule ──────────────────────────────────────────────────────────
def test_survivor_is_first_by_id(coll, tmp_path):
    dedupe.main(["--apply", "--undo-file", str(tmp_path / "u.json")])
    survivors = {d["id"]: d for d in coll.docs}
    assert len(coll.docs) == 3
    assert survivors["a"]["_id"] == ObjectId("000000000000000000000001")
    assert survivors["a"]["collected_at"] is not None, (
        "the first copy is the only one carrying collected_at; last-by-_id "
        "loses it on 92 of the 188 live multi-copy ids"
    )


def test_reports_whether_copies_differ_on_business_fields(coll, capsys):
    """Live, all 12 business fields are identical across every copy and only
    `collected_at` differs. The script must MEASURE that rather than assert
    it, so a future run against different data cannot quietly inherit the
    claim."""
    dedupe.main([])
    out = capsys.readouterr().out
    assert "business" in out.lower()
    assert "collected_at" in out


# ── 1. the index build ─────────────────────────────────────────────────────
def test_build_index_drops_the_non_unique_index_first(coll, tmp_path):
    rc = dedupe.main(["--apply", "--build-index", "--undo-file", str(tmp_path / "u.json")])
    assert rc == 0
    assert "natural_key" in coll.dropped, (
        "create_index on the same key pattern with unique=True RAISES while "
        "the non-unique index exists; it does not modify it"
    )
    assert coll.indexes["natural_key_unique"]["unique"] is True


def test_failed_index_build_returns_nonzero_and_says_unprotected(coll, tmp_path, capsys):
    def boom(*a, **k):
        raise RuntimeError("index build refused")

    coll.create_index = boom
    rc = dedupe.main(["--apply", "--build-index", "--undo-file", str(tmp_path / "u.json")])
    out = capsys.readouterr()
    assert rc == dedupe.EXIT_UNPROTECTED
    assert rc != 0
    assert "DEDUPED AND UNPROTECTED" in (out.out + out.err)


def test_without_build_index_the_warning_is_reachable(coll, tmp_path, capsys):
    rc = dedupe.main(["--apply", "--undo-file", str(tmp_path / "u.json")])
    assert rc == 0
    assert "index NOT built" in capsys.readouterr().out


# ── 2. the undo file must actually be restorable ───────────────────────────
def test_undo_file_preserves_bson_types(coll, tmp_path):
    """json.dumps(..., default=str) turns _id into a str, so a restored doc
    can never collide with the original ObjectId and a partial restore
    DOUBLES the collection instead of erroring."""
    from bson import json_util

    p = tmp_path / "u.json"
    dedupe.main(["--apply", "--undo-file", str(p)])
    raw = json.loads(p.read_text())
    assert isinstance(raw["documents"][0]["_id"], dict) and "$oid" in raw["documents"][0]["_id"]
    round_tripped = json_util.loads(json.dumps(raw["documents"]))
    assert isinstance(round_tripped[0]["_id"], ObjectId)
    import datetime
    assert isinstance(round_tripped[0]["trade_date"], datetime.datetime)


def test_undo_file_records_the_collection_and_count(coll, tmp_path):
    p = tmp_path / "u.json"
    dedupe.main(["--apply", "--undo-file", str(p)])
    raw = json.loads(p.read_text())
    assert raw["table"] == "insider_trades"
    assert raw["count"] == 3
    assert len(raw["documents"]) == 3


def test_apply_without_undo_file_still_writes_one(coll, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dedupe.main(["--apply"])
    assert list(tmp_path.glob("insider_trades_undo_*.json")), (
        "an --apply that leaves no undo file is unrecoverable"
    )


def test_script_never_claims_to_restore_without_a_restore_script():
    """defect 2: dedupe_asset_prices says its undo file "restores the exact
    documents" and `grep -rn undo scripts/` finds no consumer anywhere."""
    assert (REPO / "scripts" / "restore_insider_trades_undo.py").exists()


def test_dedupe_is_dry_run_by_default_at_the_cli():
    """Not a unit call — the real entry point, because a default that only
    holds inside main() is not the one an operator gets."""
    r = subprocess.run([sys.executable, "scripts/dedupe_insider_trades.py", "--help"],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0
    assert "--apply" in r.stdout
    assert "dry run" in r.stdout.lower()
