"""A killed AutoResearch run must not be recorded as a finished one.

`run_autoresearch` is awaited inside the eval worker. When the container is
told to stop, asyncio cancels that task and raises CancelledError — which is a
BaseException, so it walked straight past `except Exception` into `finally`.
The finally saw status == 'running' and called
`_update_ar_state(report_id, running=False)`, and `_update_ar_state` maps
running=False to status='done'.

Measured on 2026-09-04: three of the last 25 reports (cycle-v3-1788468755,
-1788418980, -1788392565) carried status='done', phase='judge_eval' and NO
scores, while their system_commands rows read "Container restarted
unexpectedly". The dashboard rendered a green done badge beside three red 0
score cards, and those zeros plotted as dips on the trend sparkline — a
crashed audit was indistinguishable from a bad one.

Both tests fail on the pre-fix code: the first asserts the status, the second
pins the discriminator (a report with no overall_score never reached the
scoring write, whatever exception type got it there).
"""

import asyncio
from unittest.mock import patch

import pytest

import app.autoresearch.core as core


class _Store:
    """Record every $set applied to autoresearch_reports, by report id."""

    def __init__(self):
        self.docs: dict[str, dict] = {}

    def insert_docs(self, collection, docs, **kw):
        if collection == "autoresearch_reports":
            for d in docs:
                self.docs[d["id"]] = dict(d)
        return len(docs)

    def update_docs(self, collection, query, update, **kw):
        if collection != "autoresearch_reports":
            return 0
        sets = update.get("$set", {})
        for rid, doc in self.docs.items():
            if query.get("id") in (None, rid) or query.get("status") == doc.get("status"):
                if query.get("id") not in (None, rid):
                    continue
                doc.update(sets)
        return 1

    def upsert_doc(self, *a, **k):
        return 1


class _Query:
    def __init__(self, store):
        self.store = store

    def find_row(self, collection, query, columns, **kw):
        if collection != "autoresearch_reports":
            return None
        doc = self.store.docs.get(query.get("id"))
        if not doc:
            return None
        return tuple(doc.get(c) for c in columns)

    def agg_row(self, collection, query, aggs, **kw):
        return tuple(0 if op.startswith("count") else None for op, _ in aggs)

    def find_rows(self, *a, **k):
        return []

    def count(self, *a, **k):
        return 0


def _run_until_cancelled(monkeypatch, store):
    """Drive run_autoresearch to a cancel at its first real stage."""
    query = _Query(store)
    monkeypatch.setattr(core, "mongo_store", store)
    monkeypatch.setattr(core, "mongo_query", query)

    def _boom(*a, **k):
        raise asyncio.CancelledError()

    # resolve_pending_outcomes is the first stage after the report row is
    # inserted, so cancelling there leaves the report exactly where a real
    # SIGTERM leaves it: status 'running', no scores.
    monkeypatch.setattr(core, "resolve_pending_outcomes", _boom)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(core.run_autoresearch("cycle-test-cancel", {"tickers_final": []}))

    assert store.docs, "no report row was written — test drove nothing"
    return next(iter(store.docs.values()))


def test_a_cancelled_audit_is_not_marked_done(monkeypatch):
    doc = _run_until_cancelled(monkeypatch, _Store())
    assert doc["status"] != "done", (
        "a run killed mid-audit was recorded as finished — this is the defect: "
        f"status={doc['status']!r} phase={doc.get('phase')!r} "
        f"overall_score={doc.get('overall_score')!r}"
    )
    assert doc["status"] == "interrupted"


def test_an_interrupted_report_carries_no_scores(monkeypatch):
    """The status must agree with the evidence: no score means not finished."""
    doc = _run_until_cancelled(monkeypatch, _Store())
    assert doc.get("overall_score") is None
    assert doc.get("phase") == "interrupted"
