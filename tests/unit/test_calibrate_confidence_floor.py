"""The confidence-floor sweep must read the store the desk actually writes.

WHY THIS FILE EXISTS
--------------------
`scripts/calibrate_confidence_floor.py` re-derives CONFIDENCE_FLOOR — a LIVE
policy gate — from realized outcomes. Until 2026-08-30 it read the Postgres
archive through `SIM_DSN`, and it failed in the two ways this migration keeps
producing at once:

  * `SIM_DSN` is in no `.env`, so `DSN = os.environ["SIM_DSN"]` raised
    `KeyError: 'SIM_DSN'` at MODULE IMPORT. The instrument had not answered
    since before the cutover and nothing said so.
  * Set the variable and it got worse, not better: the archive froze on
    2026-08-19 22:56:58, so the sweep would have re-derived the floor from a
    stale population and printed the same authoritative table.

Every test here was RED before that port:
  - the import test raised `KeyError: 'SIM_DSN'`
  - the coupling test saw 3 findings (`import psycopg` at line 26, two
    `.execute(<sql>)` calls at lines 80-81)
  - everything below refers to `CLEAN_QUERY` / `CORRUPT_QUERY` / `load()`,
    which did not exist — the filters were SQL strings.
"""
from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import calibrate_confidence_floor as mod  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

REL = "scripts/calibrate_confidence_floor.py"


def test_the_module_imports_with_no_postgres_dsn_in_the_environment():
    """RED before the port with `KeyError: 'SIM_DSN'` — raised at import, so
    the script could not even be `--help`ed, let alone run.

    Run in a subprocess with a SCRUBBED environment rather than by reloading in
    process: the parent already has `.env` loaded, and a test that inherits the
    variable it claims to have removed proves nothing.
    """
    code = (
        f"import sys; sys.path.insert(0, {str(REPO)!r});"
        "import scripts.calibrate_confidence_floor as m;"
        "print(sorted(m.CLEAN_QUERY))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={"PATH": "/usr/bin:/bin"},  # no SIM_DSN, no DATABASE_URL, no .env
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "SIM_DSN" not in proc.stderr
    assert "confidence" in proc.stdout


def test_the_sweep_has_no_postgres_coupling():
    """RED before the port: 3 findings — a psycopg import and two .execute()."""
    assert (REPO / REL).exists(), f"{REL} is gone"
    result = scan(REPO, targets=(REL,))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, (
        "the floor sweep reads Postgres again: "
        + "; ".join(f"{f['kind']} at line {f['line']}" for f in result["findings"]))


def test_the_lesson_filter_is_the_complement_not_a_membership_test():
    """The one clause in this script that does not translate by eye.

    The SQL was `COALESCE(lesson_stored,'') NOT LIKE '%PIPELINE FAILURE%'`, so
    a decision that stored NO lesson is KEPT — COALESCE turns its NULL into ''
    and '' does not contain the phrase. On 2026-08-30 that is 185 of the 1779
    clean rows, better than a tenth of the calibration sample: the `$not` form
    returns 1779, either membership form 1594.

    `{"$ne": None}` and `{"$nin": [None, ""]}` are membership tests: a document
    whose `lesson_stored` is absent fails both, so either one silently deletes
    those 185 rows and the sweep answers with a smaller population and no
    error. `{"$not": {...}}` is the set complement — it matches a document the
    inner expression does not match, INCLUDING one missing the field — which is
    what COALESCE-to-'' buys.
    """
    clause = mod.CLEAN_QUERY["lesson_stored"]
    assert set(clause) == {"$not"}, (
        "the lesson exclusion must be the COMPLEMENT of the regex; a "
        f"membership test drops every lesson-less decision. got {clause!r}")
    inner = clause["$not"]
    assert inner == {"$regex": "PIPELINE FAILURE|Failed to parse"}
    # SQL LIKE is case-sensitive and this regex must stay so. On 2026-08-30
    # adding `$options: "i"` happens to return the same 1779 rows, so this
    # assertion is not paying for itself today -- it is here because the day a
    # lesson is written in another case, the flag silently widens the excluded
    # population and the floor moves with it.
    assert "$options" not in inner
    # The corrupt count must be the mirror image, same regex, no negation.
    assert mod.CORRUPT_QUERY["$or"] == [
        {"confidence": 0}, {"lesson_stored": inner}]


def test_load_reads_decision_outcomes_and_keeps_the_select_order():
    """`summarize()` indexes r[1] and r[3]; the columns must arrive in order."""
    seen = {}

    def fake_find_rows(collection, query, columns, **kw):
        seen["rows"] = (collection, query, list(columns))
        return [("BUY", 80, 1.0, "WIN")]

    def fake_count(collection, query=None):
        seen["count"] = (collection, query)
        return 371

    with patch.object(mod.mongo_query, "find_rows", fake_find_rows), \
         patch.object(mod.mongo_query, "count", fake_count):
        rows, excluded = mod.load()

    assert rows == [("BUY", 80, 1.0, "WIN")]
    assert excluded == 371
    collection, query, columns = seen["rows"]
    assert collection == "decision_outcomes"
    assert columns == ["action", "confidence", "pnl_pct", "outcome"]
    assert query["action"] == {"$in": ["BUY", "SELL"]}
    assert query["resolved_at"] == {"$ne": None}
    assert query["confidence"] == {"$gt": 0}
    assert query["pnl_pct"] == {"$ne": None}
    assert seen["count"] == ("decision_outcomes", mod.CORRUPT_QUERY)
    # `load()` must not mutate the module-level filter it copies from.
    assert "action" not in mod.CLEAN_QUERY


def _run_main(rows, excluded=371, argv=("calibrate_confidence_floor.py",)):
    buf = io.StringIO()
    with patch.object(mod.mongo_query, "find_rows", lambda *a, **k: list(rows)), \
         patch.object(mod.mongo_query, "count", lambda *a, **k: excluded), \
         patch.object(sys, "argv", list(argv)), \
         redirect_stdout(buf):
        mod.main()
    return buf.getvalue()


def test_main_renders_the_sweep_from_mongo_rows():
    rows = ([("BUY", 60, -2.0, "LOSS")] * 10) + ([("SELL", 80, 5.0, "WIN")] * 10)
    out = _run_main(rows)
    assert "clean n=20  excluded as corrupt=371" in out
    assert "action=ALL" in out
    # 10 winners at +5 kept, 10 losers at -2 blocked -> the floor that keeps
    # only the 80s is the one that keeps the most money.
    assert "best total P&L at floor=65" in out or "best total P&L at floor=70" in out
    assert "  60-64" in out and "  80-84" in out


def test_the_positional_action_filter_still_selects_one_side():
    rows = ([("BUY", 80, 5.0, "WIN")] * 3) + ([("SELL", 80, 1.0, "WIN")] * 7)
    out = _run_main(rows, argv=("x", "buy"))
    assert "action=BUY  clean n=3" in out


def test_an_empty_population_exits_instead_of_printing_a_table():
    """A ported script that compiles, runs and answers nothing is the exact
    failure this migration exists to catch. Empty must be loud."""
    with pytest.raises(SystemExit) as e:
        _run_main([])
    assert "cannot calibrate" in str(e.value)


def test_a_population_entirely_below_the_lowest_floor_exits_cleanly():
    """The unported version raised `TypeError: 'NoneType' object is not
    subscriptable` on `best['floor']` here, because every `summarize()` came
    back empty and `best` stayed None."""
    with pytest.raises(SystemExit) as e:
        _run_main([("BUY", 10, 1.0, "WIN")])
    assert "admits a single decision" in str(e.value)
