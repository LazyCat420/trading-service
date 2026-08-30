"""A ticker that has never been rate-limited must be eligible for retry.

Under Postgres `discovered_tickers.rate_limited_count` was `INTEGER DEFAULT 0`,
so `WHERE rate_limited_count < 5` matched every pending row. No Mongo writer
supplied the field, and `{"$lt": 5}` matches neither a null nor a MISSING
field — so `get_pending_retries()` returned 0 of the 98 pending tickers in
production, and the only tickers it could ever return were ones that had
already been rate-limited at least once. The retry path had been dead since the
2026-08-19 cutover, silently, with no error anywhere.

This is the same defect the `$setOnInsert: {validation_status: 'pending'}`
calls in reddit_collector/youtube_collector were added to fix. That sweep fixed
one column of the class; this is the next one.

Proven red on the pre-fix tree: the first test returns [] there.
"""
from unittest.mock import patch

from app.validation import persistence


class _FakeStore:
    """Applies a Mongo filter to documents, for the operators used here."""

    def __init__(self, docs):
        self.docs = docs
        self.last_query = None

    def find_docs(self, collection, query, projection=None, **kw):
        self.last_query = query
        return [d for d in self.docs if self._match(d, query)]

    def _match(self, doc, query):
        for key, cond in query.items():
            if key == "$or":
                if not any(self._match(doc, c) for c in cond):
                    return False
                continue
            value = doc.get(key)          # missing -> None, as Mongo treats it
            if isinstance(cond, dict):
                for op, operand in cond.items():
                    if op == "$lt":
                        if value is None or not value < operand:
                            return False
                    elif op == "$exists":
                        if (key in doc) != operand:
                            return False
                    else:
                        raise AssertionError(f"unhandled operator {op}")
            elif cond is None:
                # Mongo: {f: None} matches a null AND a missing field.
                if value is not None:
                    return False
            elif value != cond:
                return False
        return True


def _run(docs):
    store = _FakeStore(docs)
    with patch.object(persistence, "mongo_store", store):
        return persistence.get_pending_retries(), store


def test_a_ticker_with_no_rate_limited_count_is_retried():
    got, _ = _run([{"ticker": "NVDA", "validation_status": "pending"}])
    assert got == ["NVDA"], (
        "a newly discovered ticker has never been rate-limited, so it must be "
        "eligible; Postgres defaulted the column to 0 and Mongo does not")


def test_a_null_rate_limited_count_is_retried():
    got, _ = _run([{"ticker": "NVDA", "validation_status": "pending",
                    "rate_limited_count": None}])
    assert got == ["NVDA"]


def test_a_count_under_the_limit_is_retried():
    got, _ = _run([{"ticker": "NVDA", "validation_status": "pending",
                    "rate_limited_count": 4}])
    assert got == ["NVDA"]


def test_a_ticker_at_the_limit_is_not_retried():
    got, _ = _run([{"ticker": "NVDA", "validation_status": "pending",
                    "rate_limited_count": 5}])
    assert got == [], "the whole point of the counter is that 5 stops it"


def test_a_non_pending_ticker_is_not_retried():
    got, _ = _run([{"ticker": "NVDA", "validation_status": "valid"}])
    assert got == []


def test_every_discovered_ticker_insert_seeds_the_counter():
    """The read tolerating a missing field is the repair; seeding it is the fix.

    Otherwise the gap keeps growing and every future reader of this field has
    to remember the same exception.
    """
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    writers = [
        "app/collectors/reddit_collector.py",
        "app/collectors/youtube_collector.py",
        "app/services/cycle_scheduler.py",
    ]
    for rel in writers:
        src = (repo / rel).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("upsert_doc", "update_docs", "insert_docs")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "discovered_tickers"):
                continue
            call = ast.unparse(node)
            if "validation_status" not in call:
                continue          # not an insert path
            assert "rate_limited_count" in call, (
                f"{rel}:{node.lineno} inserts a discovered ticker with a "
                "validation_status but no rate_limited_count — the retry "
                "query cannot see it:\n  " + call[:300])
