"""
Tests for the evidence packet's news query.

An audit asked why `llm_summary` was NULL for all 37,808 news rows. The answer
is that it has had no writer since 8528bb0 ("rip out V2 python processors"),
and its job is now done by `grounded_facts`. But the dead column was not
inert — it was hiding a live defect in the evidence packet:

    c not in ("llm_summary")

Parentheses without a comma are not a tuple, so that is a SUBSTRING test, and
"summary" is a substring of "llm_summary". Both columns were therefore
rewritten to the same expression, and the query asked for `best_summary` twice
while never selecting plain `summary`.

It was harmless only because `llm_summary` is always NULL, which makes
`COALESCE(llm_summary, summary)` identical to `summary`. A writer for that
column — which is what someone auditing "why is this empty" would naturally
add — would have silently replaced the evidence packet's summary text.
"""
import inspect

import pytest

from app.cognition.evidence import packet_builder
from app.cognition.evidence.normalizer import normalize_news


def _code_only() -> str:
    """packet_builder's source with COMMENTS removed.

    Searching raw source matched packet_builder's own comment, which quotes
    the bug verbatim — a probe reading the documentation instead of the code.
    Stripping string literals as well went too far the other way: every token
    this guard looks for IS a string literal, so it could never fire. Verified
    against a reinstated bug.
    """
    import io
    import tokenize

    src = inspect.getsource(packet_builder)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        out.append(tok.string)
    return " ".join(out)


def test_the_news_query_selects_each_column_once():
    """The duplicate `best_summary` alias, as a test."""
    code = _code_only()
    assert "best_summary" not in code, (
        "the evidence query still builds the duplicated alias"
    )


def test_no_substring_membership_test_against_a_bare_string():
    """`c not in ("llm_summary")` — the shape of the bug, in case it returns."""
    code = _code_only()
    assert "not in" not in code or "llm_summary" not in code


def test_the_packet_no_longer_reads_the_unwritten_column():
    code = _code_only()
    assert "llm_summary" not in code, (
        "llm_summary has had no writer since 8528bb0; selecting it is noise"
    )


# ── The normalizer's contract ────────────────────────────────────────────────

def test_normalize_news_reads_the_summary_column():
    """`normalize_news` prefers `best_summary`, a name that could never appear
    in the column list it is handed — so the primary path was always dead and
    the fallback carried it. Plain `summary` must keep working."""
    cols = ["id", "title", "publisher", "url", "published_at", "summary"]
    row = (1, "A headline", "Reuters", "https://x.test/a",
           __import__("datetime").datetime(2026, 8, 9), "Nvidia rose three percent " * 20)

    doc = normalize_news(row, cols)

    assert doc is not None
    assert "Nvidia rose three percent" in doc.content
    assert doc.metadata["publisher"] == "Reuters"


def test_normalize_news_still_honours_an_explicit_best_summary():
    """Kept for callers that do alias it — the fallback must not be the only
    path that works."""
    cols = ["id", "title", "publisher", "url", "published_at", "best_summary"]
    row = (1, "A headline", "Reuters", "https://x.test/a",
           __import__("datetime").datetime(2026, 8, 9), "Chosen text " * 30)

    doc = normalize_news(row, cols)

    assert doc is not None
    assert "Chosen text" in doc.content


def test_a_scrape_artifact_is_still_rejected():
    """The evidence path's existing guard must survive the column change."""
    cols = ["id", "title", "publisher", "url", "published_at", "summary"]
    row = (1, "H", "P", "u", __import__("datetime").datetime(2026, 8, 9),
           "Just a moment... Checking your browser before accessing the site.")

    assert normalize_news(row, cols) is None
