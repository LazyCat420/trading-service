"""The instrument that finds values Postgres supplied and Mongo does not.

Postgres filled a column DEFAULT in on INSERT; Mongo fills in nothing. Writers
converted by the codemod named only the columns the SQL named, and the SQL never
named the defaulted ones — so they vanished from every document written after
the cutover, silently. Two live examples found by the first run:
`discovered_tickers.rate_limited_count` (the retry query returned 0 of 98
pending tickers) and `v3_system_commands.status` (14 commands the poller could
never see).

The artifact is checked in so the instrument survives the archive being closed.
"""
import json
from pathlib import Path

from scripts.mongo_default_gaps import ARTIFACT, CUTOVER, load_defaults, scan

REPO = Path(__file__).resolve().parents[2]


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def count_documents(self, query, **kw):
        return sum(1 for d in self.docs if self._match(d, query))

    def _match(self, doc, query):
        for key, cond in query.items():
            if key == "_id":
                if not (doc["_id"] > cond["$gt"]):
                    return False
            elif isinstance(cond, dict) and "$exists" in cond:
                if (key in doc) != cond["$exists"]:
                    return False
        return True


class _FakeDb:
    def __init__(self, collections):
        self.collections = collections

    def list_collection_names(self):
        return list(self.collections)

    def __getitem__(self, name):
        return _FakeCollection(self.collections[name])


def _oid(when):
    from bson import ObjectId
    return ObjectId.from_datetime(when)


def test_the_artifact_exists_and_is_not_empty():
    data = json.loads(ARTIFACT.read_text())
    assert data["tables"], "no defaults recorded — the export never ran"
    assert sum(len(v) for v in data["tables"].values()) > 100


def test_the_artifact_drops_serial_primary_keys():
    """Mongo's _id replaces a serial pk; reporting it as missing is noise."""
    for table, cols in load_defaults().items():
        assert "nextval" not in (cols.get("id") or ""), table


def test_a_column_the_writer_stopped_supplying_is_reported():
    import datetime
    after = CUTOVER + datetime.timedelta(days=1)
    db = _FakeDb({"widgets": [
        {"_id": _oid(after), "name": "a"},              # missing `count`
        {"_id": _oid(after), "name": "b", "count": 0},
    ]})
    rows = scan(db=db, defaults={"widgets": {"count": "0"}})
    assert len(rows) == 1
    assert rows[0]["collection"] == "widgets"
    assert rows[0]["missing"] == 1
    assert rows[0]["population"] == 2
    assert rows[0]["share"] == 0.5


def test_a_pre_cutover_gap_is_not_the_writers_fault():
    """A document from the backfill inherits whatever the archive row had."""
    import datetime
    before = CUTOVER - datetime.timedelta(days=30)
    db = _FakeDb({"widgets": [{"_id": _oid(before), "name": "old"}]})
    assert scan(db=db, defaults={"widgets": {"count": "0"}}) == []
    # ...but --all still shows it.
    rows = scan(db=db, defaults={"widgets": {"count": "0"}}, post_cutover_only=False)
    assert len(rows) == 1


def test_a_complete_collection_reports_nothing():
    import datetime
    after = CUTOVER + datetime.timedelta(days=1)
    db = _FakeDb({"widgets": [{"_id": _oid(after), "count": 3}]})
    assert scan(db=db, defaults={"widgets": {"count": "0"}}) == []


def test_a_table_with_no_collection_is_skipped_not_crashed():
    db = _FakeDb({})
    assert scan(db=db, defaults={"gone": {"count": "0"}}) == []
