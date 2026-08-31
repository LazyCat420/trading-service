"""`news_attribution_ab.py --sample` must draw its rows from the LIVE store.

The bench asks two questions and only ONE of them touched a database. `--run`
scores a frozen, committed oracle against the Jetson and the free heuristic
and never opens a store at all; `--sample`, which mints that oracle, read

    SELECT id, ticker, COALESCE(title, ''), summary
    FROM news_articles
    WHERE ticker_attribution IS NULL AND ticker IS NOT NULL
      AND summary IS NOT NULL AND length(summary) >= 400
    ORDER BY md5(id) LIMIT %s

through `scripts.migration.pg_connection.get_db`, which since the 2026-08-19
cutover raises `AttributeError: 'Settings' object has no attribute
'DATABASE_URL'` before it reaches the socket. So `--sample` was not returning
stale rows, it was returning a traceback: the bench could be scored but never
re-sampled, which is the half that matters, because the standing verdict is
"re-run at larger n" and every prompt change is required to bring a freshly
sampled oracle.

WHY EACH TEST HERE WOULD HAVE BEEN RED BEFORE THE PORT
------------------------------------------------------
  test_the_script_has_no_postgres_coupling
        `scripts.gate_zero_pg.scan` found 3 sites in the pre-port file:
        connection_import (line 180), get_db_call (182), execute_call (183).
        Measured 2026-08-30 against `git show 77e6dc3:scripts/news_attribution_ab.py`;
        it reports 0 for the ported file.
  test_the_coupling_scan_is_not_vacuous
        negative control — a scan that looked at nothing passes the assertion
        above just as happily.
  test_sample_reads_the_news_articles_collection
  test_sample_returns_the_labelling_template_shape
  test_title_is_coalesced_and_lead_is_truncated
        the pre-port `sample()` never mentioned `mongo_query`, so with the
        store stubbed it still went to `get_db` and raised AttributeError.
  test_the_candidate_scan_is_not_a_natural_order_limit
        pins the trap the port had to avoid rather than the code it replaced:
        `LIMIT 60` translated as `find_rows(..., limit=60)` compiles, runs and
        returns 60 plausible rows — the 60 OLDEST documents in natural order,
        i.e. a sample of the past dressed as a sample.
  test_each_null_clause_keeps_its_sql_meaning
        `IS NULL` / `IS NOT NULL` / `length() >= 400` have to survive the move
        onto a store with no column types and no DEFAULTs.
  test_length_counts_characters_not_bytes
        `$strLenBytes` is the wrong operator and passes every ASCII test.
  test_sample_orders_by_the_md5_hex_digest_like_postgres
  test_the_md5_order_check_is_not_vacuous
        the ordering is the whole reason the oracle is comparable across runs.

THE ORACLE THIS PINS AGAINST IS A POSTGRES ARTIFACT
---------------------------------------------------
`tests/fixtures/attribution_oracle.json` was frozen on 2026-08-07 by running
the SQL above against Postgres — its 60 rows are stored in the order
`ORDER BY md5(id)` returned them. So asserting that `_md5_key` reproduces that
order is not a restatement of the implementation: it is a comparison against
an artifact a different database produced, and it goes red for sha1, for
sorting on the id itself, and for not sorting at all.

PROVEN BY MUTATION, 2026-08-30
------------------------------
Ten mutants of the ported source were loaded in place of the module and this
file re-run against each. The unmutated control stayed 21/21 green; every
mutant that changes an answer went red:

    natural-order `limit=n` instead of the md5 sort      3 red
    $strLenBytes instead of $strLenCP                    1 red
    IS NULL narrowed to {"$exists": True, "$eq": None}  10 red
    IS NOT NULL written as {"$exists": True}             9 red
    sha1 instead of md5                                  1 red
    $gt instead of $gte                                  7 red
    COALESCE(title, '') dropped                          1 red
    lead truncation dropped                              1 red
    $ifNull guard dropped from the length test           2 red

The tenth — passing `collection_for("news_articles")` into `find_rows`, so the
name resolves TWICE — stayed green here, and cannot be caught here: while
`renames_active()` is False `collection_for` is the identity, so no stub can
tell the two apart. It is caught by AST at
`tests/unit/test_no_double_collection_resolution.py`, which reports 0
offenders for this file and flags the mutant at line 250. Duplicating that
check with a stub would only have pinned the bug.

Verified live 2026-08-30, which is why the fixture did not need re-labelling:
the candidate population is 42,920 rows in Postgres and 42,920 in Mongo with
zero difference in either direction, and the Mongo md5-top-60 reproduces every
`id`, `ticker`, `title` and `lead` in the fixture byte for byte.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import news_attribution_ab as ab  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

REL = "scripts/news_attribution_ab.py"
ORACLE = REPO / "tests" / "fixtures" / "attribution_oracle.json"


# ─── The Postgres coupling is gone ───────────────────────────────────────────


def test_the_script_has_no_postgres_coupling():
    result = scan(REPO, targets=(REL,))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, "; ".join(
        f"{f['kind']} at line {f['line']}" for f in result["findings"]
    )


def test_the_coupling_scan_is_not_vacuous(tmp_path):
    """NEGATIVE CONTROL, on the shape this file actually had.

    The DSN is a placeholder on purpose: `scripts/pg_script_inventory.py`
    scans `tests/` too and keys on a literal connection string in code, so a
    real one here would file THIS file as a new unclassified Postgres reader.
    """
    bad = tmp_path / "news_attribution_ab.py"
    bad.write_text(
        "def sample(n):\n"
        "    from scripts.migration.pg_connection import get_db\n"
        "    with get_db() as db:\n"
        "        return db.execute('SELECT id FROM news_articles', [n]).fetchall()\n",
        encoding="utf-8",
    )
    assert scan(tmp_path, targets=("news_attribution_ab.py",))["total"] > 0


def test_no_hardcoded_production_dsn():
    source = (REPO / REL).read_text(encoding="utf-8")
    for needle in ("5433", "trading_bot_pass", "10.0.0.16", "DATABASE_URL"):
        assert needle not in source, f"{REL} still carries {needle!r}"


# ─── A store that records what it was asked ──────────────────────────────────

_LONG = "x" * 400


def _doc(i, **over):
    d = {"id": i, "ticker": "AAPL", "title": f"title {i}", "summary": _LONG,
         "ticker_attribution": None}
    d.update(over)
    for k in [k for k, v in d.items() if v is _ABSENT]:
        del d[k]
    return d


class _Absent:
    def __repr__(self):
        return "<absent>"


_ABSENT = _Absent()


class _FakeStore:
    """Just enough of `mongo_query.find_rows` to answer the two reads.

    The candidate filter here is written from the SQL's semantics, not from
    `_SAMPLE_QUERY` — a fake that consulted the query under test would agree
    with it no matter what the query said. `_SAMPLE_QUERY` is checked
    separately, against a miniature of Mongo's own operators.
    """

    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    def find_rows(self, collection, query, columns, sort=None, limit=0):
        self.calls.append({"collection": collection, "query": query,
                           "columns": list(columns), "sort": sort, "limit": limit})
        if "id" in query:                                  # read 2: fetch by id
            wanted = set(query["id"]["$in"])
            rows = [d for d in self.docs if d.get("id") in wanted]
        else:                                              # read 1: candidates
            rows = [d for d in self.docs if self._sql_candidate(d)]
        return [tuple(d.get(c) for c in columns) for d in rows]

    @staticmethod
    def _sql_candidate(d):
        return (d.get("ticker_attribution") is None
                and d.get("ticker") is not None
                and len(d.get("summary") or "") >= 400)


@pytest.fixture
def store(monkeypatch):
    def _install(docs):
        fake = _FakeStore(docs)
        monkeypatch.setattr("app.db.mongo_query.find_rows", fake.find_rows)
        return fake
    return _install


# ─── sample() reads the collection, by table name ────────────────────────────


def test_sample_reads_the_news_articles_collection(store):
    fake = store([_doc(f"id{i}") for i in range(5)])
    ab.sample(3)

    assert [c["collection"] for c in fake.calls] == ["news_articles"] * 2, (
        "both reads must name the POSTGRES TABLE, which mongo_store._coll "
        "resolves exactly once; a resolved collection name passed in here "
        "resolves twice and silently addresses a second collection")
    assert fake.calls[0]["columns"] == ["id"], (
        "the candidate scan must project id only — the same rows' summary "
        "text is hundreds of megabytes and only n are ever read")
    assert fake.calls[1]["columns"] == ["id", "ticker", "title", "summary"]
    assert set(fake.calls[1]["query"]["id"]["$in"]) <= {f"id{i}" for i in range(5)}


def test_the_candidate_scan_is_not_a_natural_order_limit(store):
    """`LIMIT 60` must NOT become `find_rows(..., limit=60)`.

    Natural order returns the OLDEST documents, so that translation compiles,
    runs, and hands back 60 entirely plausible rows that are a window on the
    past rather than a sample of the corpus.
    """
    fake = store([_doc(f"id{i}") for i in range(5)])
    ab.sample(2)
    assert fake.calls[0]["limit"] == 0
    assert fake.calls[0]["sort"] is None


def test_sample_returns_the_labelling_template_shape(store):
    docs = [_doc(f"id{i}") for i in range(6)]
    store(docs)
    rows = ab.sample(3)

    assert len(rows) == 3
    assert [r["id"] for r in rows] == sorted(
        (d["id"] for d in docs), key=ab._md5_key)[:3]
    for r in rows:
        assert r["about"] is None and r["note"] == "", (
            "the template ships UNLABELLED — a pre-filled label is the model "
            "grading its own homework")
        assert set(r) == {"id", "ticker", "title", "lead", "about", "note"}


def test_title_is_coalesced_and_lead_is_truncated(store):
    store([_doc("only", title=None, summary="y" * 5000)])
    row = ab.sample(1)[0]
    assert row["title"] == "", "COALESCE(title, '') — find_rows hands back None"
    assert len(row["lead"]) == ab._MAX_TEXT_CHARS == 1200


def test_a_row_that_vanished_between_the_two_reads_is_dropped_not_crashed(store):
    """The candidate scan and the fetch are two round trips, so a document can
    be deleted in between. SQL returned one result set and could not see this;
    the port must not KeyError on it."""
    docs = [_doc(f"id{i}") for i in range(4)]
    fake = _FakeStore(docs)
    real = fake.find_rows

    def _vanishing(collection, query, columns, sort=None, limit=0):
        if "id" in query:
            fake.docs = fake.docs[1:]          # one row deleted mid-flight
        return real(collection, query, columns, sort=sort, limit=limit)

    import app.db.mongo_query as mq
    orig = mq.find_rows
    mq.find_rows = _vanishing
    try:
        rows = ab.sample(4)
    finally:
        mq.find_rows = orig
    assert len(rows) <= 4 and all(r["id"] for r in rows)


# ─── The predicate keeps its SQL meaning ─────────────────────────────────────


def _eval_expr(expr, doc):
    """A miniature of the aggregation operators `_SAMPLE_QUERY` uses.

    Written from MongoDB's documented semantics, deliberately including the
    part that BITES: `$strLenCP` raises on a null input, so a translation that
    dropped the `$ifNull` guard fails here the way it fails on the server,
    rather than quietly evaluating to 0.
    """
    if isinstance(expr, str) and expr.startswith("$"):
        return doc.get(expr[1:])
    if not isinstance(expr, dict):
        return expr
    (op, args), = expr.items()
    if op == "$ifNull":
        v = _eval_expr(args[0], doc)
        return _eval_expr(args[1], doc) if v is None else v
    if op == "$strLenCP":
        v = _eval_expr(args, doc)
        if not isinstance(v, str):
            raise TypeError("$strLenCP requires a string argument")
        return len(v)
    if op == "$strLenBytes":
        v = _eval_expr(args, doc)
        if not isinstance(v, str):
            raise TypeError("$strLenBytes requires a string argument")
        return len(v.encode("utf-8"))
    if op == "$gte":
        return _eval_expr(args[0], doc) >= _eval_expr(args[1], doc)
    raise AssertionError(f"the miniature does not model {op!r}")


def _mongo_matches(query, doc):
    """`{"f": None}` matches a null OR an absent field; `{"$ne": None}` matches
    neither. Both are MongoDB's own rules, not this port's."""
    for field, cond in query.items():
        if field == "$expr":
            if not _eval_expr(cond, doc):
                return False
        elif cond is None:
            if doc.get(field) is not None:
                return False
        elif isinstance(cond, dict) and set(cond) == {"$ne"}:
            if doc.get(field) == cond["$ne"]:
                return False
        else:
            raise AssertionError(f"the miniature does not model {cond!r}")
    return True


@pytest.mark.parametrize("doc, keep, why", [
    (_doc("a"), True, "the ordinary unlabelled legacy row"),
    (_doc("b", ticker_attribution=_ABSENT), True,
     "IS NULL matches a MISSING field too — PG DEFAULTs did not survive the "
     "cutover, so a field the archive always carried can simply be absent"),
    (_doc("c", ticker_attribution="llm"), False, "already labelled"),
    (_doc("d", ticker=None), False, "ticker IS NOT NULL"),
    (_doc("e", ticker=_ABSENT), False,
     "$ne: None must not match a missing field, matching IS NOT NULL"),
    (_doc("f", summary=None), False, "summary IS NOT NULL"),
    (_doc("g", summary=_ABSENT), False, "summary IS NOT NULL, field absent"),
    (_doc("h", summary="z" * 399), False, "length(summary) >= 400 is inclusive"),
    (_doc("i", summary="z" * 400), True, "length(summary) >= 400 is inclusive"),
])
def test_each_null_clause_keeps_its_sql_meaning(doc, keep, why):
    assert _mongo_matches(ab._SAMPLE_QUERY, doc) is keep, why


def test_length_counts_characters_not_bytes():
    """`$strLenBytes` agrees with `length()` on every ASCII article and
    disagrees on this corpus, which is full of curly quotes and em dashes.
    250 accented characters are 250 characters and 500 bytes."""
    doc = _doc("nonascii", summary="é" * 250)
    assert _mongo_matches(ab._SAMPLE_QUERY, doc) is False, (
        "Postgres length() counts characters: 250 < 400, so this row was "
        "never a candidate. $strLenBytes would see 500 and admit it.")


# ─── The md5 ordering is a Postgres artifact this must reproduce ─────────────


def _oracle():
    return json.loads(ORACLE.read_text(encoding="utf-8"))


def test_sample_orders_by_the_md5_hex_digest_like_postgres():
    """The fixture's row ORDER is what `ORDER BY md5(id)` returned from
    Postgres on 2026-08-07. Re-deriving it from the ids alone pins the digest
    and the key the port sorts on."""
    ids = [row["id"] for row in _oracle()]
    assert sorted(ids, key=ab._md5_key) == ids


def test_the_md5_order_check_is_not_vacuous():
    """Three ways the assertion above could be true for the wrong reason."""
    ids = [row["id"] for row in _oracle()]
    assert sorted(ids) != ids, "would pass for a plain lexicographic id sort"
    assert sorted(ids, key=lambda i: hashlib.sha1(i.encode()).hexdigest()) != ids, (
        "would pass for any digest, so it would not pin md5")
    assert len(set(ids)) == len(ids) == 60


def test_sample_returns_the_rows_in_md5_order(store):
    ids = [f"article-{i}" for i in range(40)]
    store([_doc(i) for i in ids])
    got = [r["id"] for r in ab.sample(10)]
    assert got == sorted(ids, key=ab._md5_key)[:10]
    assert got != sorted(ids)[:10], "the fixture ids must exercise the ordering"
