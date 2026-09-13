"""Every collection a cycle reads on its hot path must lead with an index.

MEASURED 2026-09-13 against the live store, before this change:

    news_articles  indexes: ['_id_', 'id_1', 'id_plain_1']   (140,158 docs / 594 MB)
      {'ticker': t} sort collected_at   COLLSCAN  140,158 examined  132 ms
      {'url': url}                      COLLSCAN  140,158 examined  134 ms
      {'content_hash': h}               COLLSCAN  140,158 examined  131 ms

The last two run once per ARTICLE inside the news sweep — 164 articles per
sweep, so ~43 seconds of a 12.4-minute discovery phase was spent scanning 594 MB
to answer "have I seen this before". `id`/`id_plain` were the only indexes and
no reader queries by `id`.

This asserts the DECLARATION, not the live database. An index that exists only
because someone ran `create_index` by hand against production is not a fact
about the code — it disappears on the next fresh store and nothing fails, it
just gets slow. Same argument as `test_technicals_index.py`.

It is expressed as COVERAGE of the readers' leading fields rather than a list
of index names, so adding a reader that leads on an unindexed field fails here
instead of quietly costing a scan.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "app" / "db" / "mongo_store.py"

#: collection -> the leading fields real readers filter/sort on first.
#: Each entry was taken from a live call site, named in the comment.
HOT_READS: dict[str, set[str]] = {
    # news_collector dedupe + fan-out, once per article; context_block reads
    # by ticker; the quality sweep aggregates on collected_at.
    "news_articles": {"ticker", "url", "content_hash", "collected_at"},
    # the client's /run-cycle/audit/{id} and performance_audit, per cycle
    "execution_errors": {"cycle_id"},
    "cycle_audit_log": {"cycle_id"},
    # watch_desk's four readers
    "watch_triage_log": {"ticker", "created_at"},
    # the screener's latest-per-ticker read, and the date-only freshness scan
    "technicals": {"ticker", "date"},
    # outcome_tracker's held snapshot, once per cycle
    "shared_desk": {"cycle_id"},
    # alt_data_block, once per ticker
    "insider_trades": {"ticker"},
    # cycle_forensics — the A/B instrument
    "v3_guardrail_firings": {"cycle_id"},
}


def _declared_leading_fields() -> dict[str, set[str]]:
    """collection -> set of fields that LEAD one of its declared indexes.

    Only the first key of a compound index can serve a filter on that field
    alone, which is the whole reason `technicals`' (ticker, date) index did
    nothing for a `{'date': {'$gte': ...}}` scan over 1.39M documents.
    """
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "ensure_indexes"), None)
    assert fn is not None, "ensure_indexes is gone — every declaration went with it"

    out: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_try" and node.args):
            continue
        coll = node.args[0]
        if not (isinstance(coll, ast.Constant) and isinstance(coll.value, str)):
            continue
        if len(node.args) < 2:
            continue
        keys = node.args[1]
        lead = None
        if isinstance(keys, ast.Constant) and isinstance(keys.value, str):
            lead = keys.value                      # _try(coll, "field")
        elif isinstance(keys, (ast.List, ast.Tuple)) and keys.elts:
            first = keys.elts[0]
            if isinstance(first, (ast.Tuple, ast.List)) and first.elts:
                f = first.elts[0]
                if isinstance(f, ast.Constant):
                    lead = f.value                 # _try(coll, [("field", ASC), ...])
        if lead:
            out.setdefault(coll.value, set()).add(lead)
    return out


@pytest.mark.parametrize("collection", sorted(HOT_READS))
def test_every_hot_read_leads_with_a_declared_index(collection):
    declared = _declared_leading_fields().get(collection, set())
    missing = HOT_READS[collection] - declared
    assert not missing, (
        f"{collection}: readers filter on {sorted(missing)} but no declared "
        f"index LEADS with those fields (declared leading fields: "
        f"{sorted(declared) or 'NONE'}).\n"
        "A filter on a field that is not the first key of some index is a "
        "collection scan. Add a declaration in mongo_store.ensure_indexes."
    )


def test_the_parser_is_not_returning_an_empty_map():
    """Non-vacuity. A parser that returns {} makes every assertion above vacuous
    the moment the set difference is empty — and it would be empty for a
    collection with no entry in HOT_READS, so the failure mode is silent."""
    declared = _declared_leading_fields()
    assert len(declared) >= 10, (
        f"only parsed {len(declared)} collections out of ensure_indexes — the "
        "AST shape of _try() has changed and this guard is reading nothing"
    )
    assert "price_history" in declared or "technicals" in declared


def test_a_unique_index_is_not_declared_where_duplicates_exist():
    """`insider_trades` is 85.4% redundant (2,446 docs, 356 distinct ids).

    A unique index cannot build while duplicates exist, and `_try` SWALLOWS the
    failure and logs — so declaring one here would let a deploy report success
    while the collection stayed unprotected. Dedupe first, then declare.
    """
    src = STORE.read_text(encoding="utf-8")
    fn_src = src[src.index("def ensure_indexes"):]
    window = fn_src[:fn_src.index("_indexes_ready = True")]
    for line in window.splitlines():
        if '"insider_trades"' in line or "'insider_trades'" in line:
            assert "unique=True" not in line, (
                "a unique index on insider_trades is declared while the "
                "collection still holds 2,090 duplicate rows — _try will "
                "swallow the IndexOptionsConflict and the deploy will look fine"
            )
