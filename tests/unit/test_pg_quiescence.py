"""The soak's quiescence check, and the three ways it could report false quiet.

`pg_quiescence.py --diff` is the criterion the 24-72h soak passes or fails on:
Postgres must receive NOTHING for the trading tables across three full cycles.
A check like that is only worth its verdict if it cannot say "quiet" for the
wrong reason, and there are exactly three ways it could:

  1. the counters were RESET between the snapshots — every number restarts at
     zero, every delta goes negative, and the diff reads as perfect silence;
  2. a table was DROPPED — its row leaves pg_stat_user_tables entirely, taking
     its counters with it;
  3. it only looked at writes — a SELECT changes no data and is exactly the
     coupling that turns "frozen archive" back into "load-bearing".

The live positive control belongs to the tool rather than the suite: run
`--snapshot` and `--diff` a second apart against production and the diff names
the tables the running cycle touched (measured 2026-08-19: pipeline_state
seq_scan+5 in a 260ms window). These tests pin the arithmetic around it.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

spec = importlib.util.spec_from_file_location(
    "pg_quiescence", _ROOT / "scripts" / "pg_quiescence.py")
pq = importlib.util.module_from_spec(spec)
sys.modules["pg_quiescence"] = pq
spec.loader.exec_module(pq)


def _snap(tables, *, reset="2026-08-01T00:00:00+00:00", clients=(), at="t"):
    return {"taken_at": at, "stats_reset": reset, "tables": tables,
            "clients": list(clients), "excluded_foreign_tables": []}


ZERO = dict.fromkeys(pq.COUNTERS, 0)


def test_no_movement_is_quiescent():
    base = _snap({"positions": dict(ZERO, seq_scan=10, idx_scan=99)})
    assert pq.diff(base, _snap({"positions": dict(ZERO, seq_scan=10, idx_scan=99)})) == 0


@pytest.mark.parametrize("counter", pq.COUNTERS)
def test_any_counter_moving_fails(counter):
    """Reads count. `seq_scan`/`idx_scan` are how a forgotten dashboard query
    or a cron report shows up — no data changes, and the coupling is real."""
    base = _snap({"positions": dict(ZERO)})
    now = _snap({"positions": dict(ZERO, **{counter: 1})})
    assert pq.diff(base, now) == 1


def test_a_stats_reset_is_inconclusive_not_quiet():
    """THE FALSE-QUIET CASE: after `pg_stat_reset()` every counter is smaller
    than its baseline, so every delta is negative and nothing looks touched."""
    base = _snap({"positions": dict(ZERO, seq_scan=1_000_000)},
                 reset="2026-08-01T00:00:00+00:00")
    now = _snap({"positions": dict(ZERO, seq_scan=3)},
                reset="2026-08-19T00:00:00+00:00")
    assert pq.diff(base, now) == 2       # 2 = inconclusive, not 0 = quiet


def test_a_dropped_table_is_not_silence():
    base = _snap({"positions": dict(ZERO), "old_table": dict(ZERO)})
    now = _snap({"positions": dict(ZERO)})
    assert pq.diff(base, now) == 1


def test_a_table_that_appeared_after_the_baseline_is_measured_from_zero():
    """A collection created after T0 has no baseline row. Treating a missing
    baseline as "unknown, skip" would exempt exactly the new writer this check
    exists to catch."""
    base = _snap({"positions": dict(ZERO)})
    now = _snap({"positions": dict(ZERO), "new_table": dict(ZERO, n_tup_ins=5)})
    assert pq.diff(base, now) == 1


def test_treesearch_tables_are_excluded_by_an_explicit_list():
    """They share the database and never stop writing, so a database-wide
    counter could never go quiet. The exclusion is a list, not a prefix — a
    pattern would silently adopt the next table someone adds."""
    assert "observations" in pq.FOREIGN_TABLES
    assert "canonical_strains" in pq.FOREIGN_TABLES
    assert "positions" not in pq.FOREIGN_TABLES
    assert "price_history" not in pq.FOREIGN_TABLES
