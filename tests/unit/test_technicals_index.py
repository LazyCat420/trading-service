"""`technicals` must be indexed on (ticker, date).

Why this needs its own guard: `technicals` is the ONE collection nothing seeds
through the backfill. Every other collection gets its natural key from
`ensure_key_index()`, which only runs on the backfill path;  `technicals` is
rebuilt in place by scripts/recompute_technicals.py, which writes straight
through mongo_store. So it silently had no index but `_id` — and nothing
failed, it just got slow: the screener's latest-per-ticker read is a sort of
the whole collection by (ticker, date), which measured 53.2s and 500'd behind
the proxy on 2026-08-19, against 0.11s with the index.

"Slow" was also not the end state. The recompute is refilling this collection
toward ~1.37M documents, past the point where an unindexed in-memory sort hits
Mongo's 100MB limit and fails outright.

An index that exists only because someone ran create_index by hand against a
live database is not a fact about the code. This asserts the DECLARATION.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "app" / "db" / "mongo_store.py"


def _ensure_indexes_source() -> str:
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "ensure_indexes"), None)
    assert fn is not None, "ensure_indexes is gone — every collection's index declaration went with it"
    return ast.dump(fn)


def test_ensure_indexes_declares_the_technicals_key():
    dumped = _ensure_indexes_source()
    assert "'technicals'" in dumped or '"technicals"' in dumped, (
        "ensure_indexes no longer declares an index for `technicals`. Nothing "
        "else creates one — the backfill's ensure_key_index() does not run for "
        "this collection — so it would go back to an unindexed full-collection "
        "sort on the screener's hot path."
    )
    assert "ticker_date" in dumped, (
        "the technicals index is declared under a different name; keep "
        "`ticker_date` so a redeploy matches the index already built in "
        "production rather than building a second one"
    )


def test_the_detector_would_notice_a_missing_declaration():
    """NEGATIVE CONTROL: the assertion above passes just as happily against a
    scanner that finds nothing, so prove it can tell the two apart."""
    src = (
        "def ensure_indexes():\n"
        "    _try('tool_usage_stats', 'called_at')\n"
    )
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "ensure_indexes")
    dumped = ast.dump(fn)
    assert "'technicals'" not in dumped and "ticker_date" not in dumped
