"""LEFT JOIN and anti-join are not `join_rows` — they return different rows.

WHY THESE ARE THREE FUNCTIONS AND NOT ONE WITH A FLAG
-----------------------------------------------------
`join_rows` is an INNER join. Ported onto a `LEFT JOIN` it drops exactly the
rows the outer join exists to keep, and ported onto an anti-join
(`LEFT JOIN r ON ... WHERE r.key IS NULL`) it returns the COMPLEMENT — the
matched rows instead of the unmatched ones.

Neither failure raises, and neither looks wrong in isolation:

  * the outer-join case turns "every S&P 500 name in this sector" into "the
    ones that happen to have a price row" — a shorter listing that still looks
    complete (`sector_aggregator` hit this and hand-rolled a two-scan stitch);
  * the anti-join case turns "tickers with no analysis yet" into "tickers that
    already have one", so the caller queues work that is already done. Known
    consumers: `pending_review`, `smart_money_tools`, two sites in
    `strategy_auditor`.

A flag on one function is a thing a codemod gets wrong silently; three names
make the row-count difference visible at the call site.

The NULL-key cases are here because `NULL = NULL` is not true in SQL, and a
document that simply lacks the join field reads back as `None` — grouping those
under a `None` key joins every keyless right doc to every keyless left one.
"""
from __future__ import annotations

import pytest

from app.db import mongo_query


@pytest.fixture
def store(monkeypatch):
    """`mongo_store.find_docs` answering from in-memory collections."""
    collections: dict[str, list[dict]] = {}

    def find_docs(collection, query, sort=None, projection=None, limit=0, session=None):
        docs = [d for d in collections.get(collection, [])
                if all(d.get(k) == v for k, v in (query or {}).items())]
        return [dict(d) for d in docs]

    monkeypatch.setattr(mongo_query.mongo_store, "find_docs", find_docs)
    monkeypatch.setattr(mongo_query, "_money_cols", lambda *a, **k: frozenset())
    return collections


def _fixture(store):
    """3 tickers, only 2 of which have a price row."""
    store["ticker_metadata"] = [
        {"ticker": "AAPL", "sector": "Technology"},
        {"ticker": "MSFT", "sector": "Technology"},
        {"ticker": "NVDA", "sector": "Technology"},
    ]
    store["price_history"] = [
        {"ticker": "AAPL", "close": 100.0},
        {"ticker": "MSFT", "close": 200.0},
    ]


def _args(select):
    return dict(
        left="ticker_metadata", left_query={"sector": "Technology"}, left_key="ticker",
        right="price_history", right_key="ticker",
        left_fields=["ticker"], right_fields=["close"], select=select,
    )


SELECT = [("l", "ticker"), ("r", "close")]


def test_inner_drops_the_unmatched_row(store):
    _fixture(store)
    assert mongo_query.join_rows(**_args(SELECT)) == [("AAPL", 100.0), ("MSFT", 200.0)]


def test_left_keeps_it_with_nulls(store):
    """The whole point: NVDA survives, priced NULL — as Postgres returns it."""
    _fixture(store)
    assert mongo_query.left_join_rows(**_args(SELECT)) == [
        ("AAPL", 100.0), ("MSFT", 200.0), ("NVDA", None),
    ]


def test_anti_returns_only_the_unmatched_row(store):
    _fixture(store)
    args = _args([("l", "ticker")])
    args.pop("right_fields")
    assert mongo_query.anti_join_rows(**args) == [("NVDA",)]


def test_anti_is_the_complement_of_inner_not_a_variant_of_it(store):
    """THE TRAP, stated as an assertion: the two share no rows, and together
    they are the left side. A codemod that reaches for `join_rows` here does
    not get an approximation — it gets the opposite set."""
    _fixture(store)
    inner = {r[0] for r in mongo_query.join_rows(**_args(SELECT))}
    anti_args = _args([("l", "ticker")])
    anti_args.pop("right_fields")
    anti = {r[0] for r in mongo_query.anti_join_rows(**anti_args)}
    assert inner & anti == set()
    assert inner | anti == {"AAPL", "MSFT", "NVDA"}


def test_a_right_column_in_an_anti_join_is_refused(store):
    """It is NULL for every row by construction, so asking for it means the
    statement was not an anti-join. A silent column of Nones would read as
    data."""
    _fixture(store)
    args = _args(SELECT)
    args.pop("right_fields")
    with pytest.raises(ValueError) as exc:
        mongo_query.anti_join_rows(**args)
    assert "left_join_rows" in str(exc.value)


def test_a_duplicated_right_key_multiplies_left_rows_in_both_joins(store):
    """SQL emits one row per match. The outer join must not collapse them —
    that would be a DISTINCT nobody asked for."""
    store["ticker_metadata"] = [{"ticker": "AAPL", "sector": "Technology"}]
    store["price_history"] = [
        {"ticker": "AAPL", "close": 100.0},
        {"ticker": "AAPL", "close": 101.0},
    ]
    assert mongo_query.join_rows(**_args(SELECT)) == [("AAPL", 100.0), ("AAPL", 101.0)]
    assert mongo_query.left_join_rows(**_args(SELECT)) == [("AAPL", 100.0), ("AAPL", 101.0)]


def test_limit_counts_emitted_rows_in_the_outer_join(store):
    _fixture(store)
    assert len(mongo_query.left_join_rows(limit=2, **_args(SELECT))) == 2


# ── NULL keys: `NULL = NULL` is not true ───────────────────────────────────
def test_a_keyless_right_document_joins_nothing(store):
    """A document that lacks the join field reads back as None. Indexing those
    under `None` would cross-join every keyless pair — rows that never existed
    in the SQL, and no row count that would look wrong."""
    store["ticker_metadata"] = [{"sector": "Technology"}]          # no ticker
    store["price_history"] = [{"close": 100.0}]                    # no ticker
    assert mongo_query.join_rows(**_args(SELECT)) == []
    assert mongo_query.left_join_rows(**_args(SELECT)) == [(None, None)]


def test_a_keyless_left_row_is_unmatched_so_the_anti_join_keeps_it(store):
    store["ticker_metadata"] = [{"sector": "Technology"}]
    store["price_history"] = [{"close": 100.0}]
    args = _args([("l", "ticker")])
    args.pop("right_fields")
    assert mongo_query.anti_join_rows(**args) == [(None,)]


def test_the_right_query_narrows_the_match_not_the_left_side(store):
    """NEGATIVE CONTROL: filtering the right side of a LEFT JOIN moves rows to
    NULL, it does not delete them — the ON-clause predicate, not a WHERE."""
    _fixture(store)
    args = _args(SELECT) | {"right_query": {"ticker": "AAPL"}}
    assert mongo_query.left_join_rows(**args) == [
        ("AAPL", 100.0), ("MSFT", None), ("NVDA", None),
    ]
    anti = _args([("l", "ticker")]) | {"right_query": {"ticker": "AAPL"}}
    anti.pop("right_fields")
    assert mongo_query.anti_join_rows(**anti) == [("MSFT",), ("NVDA",)]
