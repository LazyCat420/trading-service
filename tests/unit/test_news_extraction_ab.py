"""`scripts/news_extraction_ab.py` reads the store that still has articles.

WHAT WAS BROKEN
---------------
The bench replayed "real articles" out of the Postgres `news_articles` table.
That table stopped growing at the 2026-08-19 cutover: on 2026-08-30 its newest
row was `collected_at = 2026-08-20 00:56:24`, while Mongo held 18,953 more
articles in the same 60-day window. So the default `--days 7` selected from a
window the archive has NOTHING in — an A/B whose corpus is empty is not a
close call, it is `no articles found` and exit 2.

It never got that far, because `settings.DATABASE_URL` is gone: the first
statement raised `AttributeError` inside `_ensure_pool()`. Loud, but equally
dead.

WHAT THESE TESTS PIN
--------------------
The three things a port of this shape gets wrong:

  * it keeps a Postgres import (test 1, with its negative control in test 2);
  * it builds a Mongo filter that compiles and matches NOTHING — the empty
    result that reads as "no recent articles" rather than as a bug (tests 4-7,
    and the live test at the bottom, which fails on an empty answer);
  * it drops something the SELECT list was doing — here `COALESCE(title, '')`,
    whose absence makes the prompt say "None" for every untitled article
    instead of "(untitled)" (test 8).

Test 4 is the one that would not have been obvious. `\\m ticker \\M` has no
Mongo operator, and `\\b` is the obvious substitute — but Mongo's PCRE
classifies word characters ASCII-only, so `\\bF\\b` matches the F of the German
"Für" where Postgres's `\\mF\\M` does not. Over collected_at in 2026-08-10..14
that is 5310 rows against Postgres's 5309. Spelling the word class out in
unicode properties returns 5309, field for field, in the same order.
"""
from __future__ import annotations

import ast
import datetime as dt
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from app.services.news_extraction import _MIN_TEXT_CHARS  # noqa: E402
from scripts import news_extraction_ab as ab  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

REL = "scripts/news_extraction_ab.py"


# ── 1. no Postgres coupling, and proof the check can fail ───────────────────

def test_the_bench_has_no_postgres_coupling():
    result = scan(REPO, targets=(REL,))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, (
        "still reads Postgres: "
        + "; ".join(f"{f['kind']} line {f['line']}: {f['detail']}"
                    for f in result["findings"]))


def test_the_scan_would_have_failed_on_the_original(tmp_path):
    """NEGATIVE CONTROL for the test above. A scan that finds nothing because
    it looked at nothing passes just as happily, so this feeds the same call
    the exact three statements the original had."""
    coupled = tmp_path / "news_extraction_ab.py"
    coupled.write_text(
        "from scripts.migration.pg_connection import get_db\n"
        "def fetch_articles(n, days):\n"
        "    with get_db() as db:\n"
        "        return db.execute('SELECT id FROM news_articles').fetchall()\n",
        encoding="utf-8")
    result = scan(tmp_path, targets=("news_extraction_ab.py",))
    kinds = {f["kind"] for f in result["findings"]}
    assert result["total"] == 3, result["findings"]
    assert kinds == {"connection_import", "get_db_call", "execute_call"}


# ── 2. the read goes through the seam, at the right collection ──────────────

def _capture(n=5, days=7, on_ticker=False, rows=()):
    """Run fetch_articles with the Mongo seam replaced by a recorder."""
    seen = {}

    def _fake_find_rows(collection, query, columns, sort=None, limit=0, **kw):
        seen.update(collection=collection, query=query, columns=list(columns),
                    sort=sort, limit=limit)
        return list(rows)

    from app.db import mongo_query
    with patch.object(mongo_query, "find_rows", _fake_find_rows):
        out = ab.fetch_articles(n, days, on_ticker=on_ticker)
    return seen, out


def test_it_reads_the_postgres_table_name_not_a_resolved_collection():
    """`find_rows` resolves `collection_for()` itself, exactly once. A caller
    that resolves first makes it twice: a silent miss, and an invisible second
    collection on write, the day renames are switched on.

    Asserted on the SOURCE, not on the value that comes back. `collection_for`
    is the identity function while `apply_renames` is false, so
    `find_rows(collection_for("news_articles"), ...)` passes this exact string
    today — a value assertion here is a tautology that pins the bug in place,
    which is the mistake `test_no_double_collection_resolution.py` was written
    after someone made it at `agent_latency_report.py:208`."""
    tree = ast.parse((REPO / REL).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "fetch_articles")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id in ("mongo_query", "mongo_store")]
    assert len(calls) == 1, [ast.unparse(c)[:80] for c in calls]
    first = calls[0].args[0]
    assert isinstance(first, ast.Constant) and first.value == "news_articles", (
        f"the collection argument is {ast.unparse(first)!r}, not the literal "
        "postgres table name")

    # And the value really does arrive at the seam.
    seen, _ = _capture()
    assert seen["collection"] == "news_articles"


def test_the_select_list_and_ordering_survive_the_port():
    seen, _ = _capture(n=5, days=7)
    # Positional unpacking at the call site depends on this exact order.
    assert seen["columns"] == ["id", "ticker", "title", "summary"]
    assert seen["sort"] == [("collected_at", -1)]   # ORDER BY collected_at DESC
    assert seen["limit"] == 5                       # LIMIT %s


def test_the_window_is_the_days_flag_measured_from_now():
    seen, _ = _capture(days=7)
    cutoff = seen["query"]["collected_at"]["$gt"]
    assert isinstance(cutoff, dt.datetime) and cutoff.tzinfo is not None
    delta = dt.datetime.now(dt.timezone.utc) - cutoff
    assert dt.timedelta(days=7) <= delta < dt.timedelta(days=7, seconds=60)


# ── 3. the filter cannot silently match nothing ─────────────────────────────

def _exprs(query):
    return [c["$expr"] for c in query["$and"]]


def test_the_length_floor_counts_characters_and_tolerates_a_missing_field():
    """$strLenBytes would over-count non-ASCII summaries, and $strLenCP on a
    document with no `summary` field ERRORS the whole query rather than
    filtering that document out. Post-cutover documents no longer inherit
    Postgres column defaults, so "the field is always present" is an
    assumption with an expiry date."""
    seen, _ = _capture()
    length = _exprs(seen["query"])[0]
    assert length == {"$gte": [{"$strLenCP": {"$ifNull": ["$summary", ""]}},
                               _MIN_TEXT_CHARS]}
    assert seen["query"]["summary"] == {"$ne": None}   # summary IS NOT NULL


def test_on_ticker_uses_unicode_word_boundaries_and_never_backslash_b():
    r"""Mongo's PCRE runs UTF-8 but not unicode character properties for \w, so
    `\bF\b` matches the F of "Für" and Postgres's `\mF\M` does not. Measured
    2026-08-10..14: `\b` 5310 rows, Postgres 5309, the class below 5309."""
    seen, _ = _capture(on_ticker=True)
    regex = _exprs(seen["query"])[1]["$regexMatch"]["regex"]
    pattern = "".join(p for p in regex["$concat"] if isinstance(p, str))
    assert r"\b" not in pattern, pattern
    assert r"\p{L}" in pattern and r"\p{N}" in pattern, pattern
    assert pattern.startswith("(?<!") and pattern.endswith(")"), pattern
    # `~` is case sensitive in Postgres; an "i" option would widen the filter.
    assert "options" not in seen["query"]["$and"][1]["$expr"]["$regexMatch"]


def test_on_ticker_excludes_the_null_tickers_the_sql_could_not_match():
    """`'\\m' || NULL || '\\M'` is NULL and `summary ~ NULL` is NULL, so those
    rows never came back. 9,198 of 116,354 documents have no ticker. And
    `$concat` over a null yields null, which makes `$regexMatch` ERROR rather
    than skip — hence the $ifNull as well as the filter."""
    seen, _ = _capture(on_ticker=True)
    assert seen["query"]["ticker"] == {"$ne": None}
    regex = _exprs(seen["query"])[1]["$regexMatch"]["regex"]
    fallback = [p for p in regex["$concat"] if isinstance(p, dict)][0]
    assert fallback["$ifNull"][0] == "$ticker"
    # An assertion that can never be satisfied, so a null ticker matches nothing.
    assert fallback["$ifNull"][1] == "(?!)"


def test_the_ticker_filter_is_absent_unless_asked_for():
    seen, _ = _capture(on_ticker=False)
    assert len(seen["query"]["$and"]) == 1
    assert "ticker" not in seen["query"]


def test_limit_zero_answers_without_asking_mongo():
    """SQL `LIMIT 0` returned no rows; Mongo's `$limit` REFUSES a zero and the
    underlying find() would return EVERYTHING for limit=0."""
    seen, out = _capture(n=0, rows=[("x", "AAPL", "t", "b" * 500)])
    assert out == []
    assert seen == {}


# ── 4. the SELECT list did work the caller still depends on ────────────────

def test_a_null_title_still_arrives_as_an_empty_string():
    """`COALESCE(title, '')`. Without it `_PROMPT_TEMPLATE.format(title=None)`
    writes "None" into the prompt where the caller's `title or "(untitled)"`
    would have written "(untitled)"."""
    _, out = _capture(rows=[("id1", "AAPL", None, "body")])
    assert out == [("id1", "AAPL", "", "body")]


def test_rows_come_back_in_select_order_unchanged():
    _, out = _capture(rows=[("id1", "AAPL", "Title", "body"),
                            ("id2", None, "Other", "body2")])
    assert out == [("id1", "AAPL", "Title", "body"),
                   ("id2", None, "Other", "body2")]


# ── 5. against the real store: empty is RED ────────────────────────────────

@pytest.mark.skipif(not os.environ.get("TRADING_BOT_LIVE_AUDIT"),
                    reason="live audit — set TRADING_BOT_LIVE_AUDIT=1")
def test_the_filter_selects_real_articles_from_the_live_collection(live_mongo):
    """A filter that compiles, runs and returns [] is the exact failure this
    port exists to catch, and no structural assertion above can see it. This
    one runs the real query against the real collection and re-checks every
    predicate in Python — with the `regex` module, whose `\\p{L}` is unicode
    aware the way Postgres's ctype is."""
    import regex

    rows = ab.fetch_articles(25, 7)
    assert rows, "seven days of news_articles selected nothing"
    assert len({r[0] for r in rows}) == len(rows), "duplicate article ids"
    for article_id, _ticker, title, summary in rows:
        assert isinstance(summary, str) and len(summary) >= _MIN_TEXT_CHARS
        assert isinstance(title, str)          # COALESCE, not None

    on_ticker = ab.fetch_articles(25, 7, on_ticker=True)
    assert on_ticker, "the --on-ticker population is empty"
    word = r"[\p{L}\p{N}_]"
    for article_id, ticker, _title, summary in on_ticker:
        assert ticker, f"{article_id}: a null ticker survived the filter"
        assert regex.search(rf"(?<!{word}){regex.escape(ticker)}(?!{word})",
                            summary), f"{article_id}: {ticker!r} not a word in the body"
