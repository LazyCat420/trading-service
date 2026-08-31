"""The instruments the cutover is verified WITH must read the store it moves to.

A Mongo-only cycle graded by a Postgres reader grades a store nothing writes
any more. Every check comes back clean — zero errors, zero stuck commands, zero
duplicate agent runs — because every table it counts is frozen. That is the
worst possible failure for a verification tool: it reports success, in detail,
with numbers.

`gate_zero_pg.py` scans `app/` and deliberately not `scripts/`, because the
migration and parity tooling must keep reading the frozen Postgres backup
forever. These four files are the exception inside that exception: they are how
the cutover and the 24-72h soak get judged, so they are held to `app/`'s rule.

The ratchet on the rest of `scripts/` is here too. 0 is not reachable there yet
— ~100 files of one-off reports and backfills still read Postgres — and a gate
nobody can turn on is worth less than a number that may only go down.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.gate_zero_pg import scan  # noqa: E402

# The soak battery, per the cutover runbook: grade a cycle, check the pre/post
# checklist, smoke-test one ticker, and the quick per-cycle count audit.
INSTRUMENTS = (
    "scripts/cycle_audit.py",
    "scripts/cycle_healthcheck.py",
    "scripts/smoke_test_cycle.py",
    "scripts/run_audit.py",
    # The control plane, added 2026-08-19 after the first post-cutover cycle.
    # These four do not grade a cycle, they START and GUARD one, and reading the
    # frozen store made each of them lie in a different direction: the two
    # triggers enqueued onto a Postgres queue no poller reads and printed
    # success; `check_pipeline_state` (the command guard_deploy.py tells an
    # operator to run) answered `done` mid-cycle; and `deploy_preflight` — the
    # last gate before the container swap — printed "pipeline idle, deploy may
    # proceed" while cycle-v3-1787193855 was analyzing, which is the 2026-08-11
    # cycle-killing incident with the safety catch filed off.
    "scripts/trigger_cycle.py",
    "scripts/observe_cycle.py",
    "scripts/check_pipeline_state.py",
    "scripts/deploy_preflight.py",
)

# Measured 2026-08-19 after the control-plane four were converted (427 before),
# and RE-BASELINED 2026-08-30. It had sat at 408 while the tree was at 279 —
# 129 couplings of unearned headroom, i.e. a third of everything the migration
# removed could have come back without this failing. A ratchet that is not
# re-measured when work lands is a ratchet with the teeth filed off.
# Lower it whenever a script is converted, moved under scripts/migration/, or
# deleted; never raise it.
# 241 -> 74 on 2026-08-30, when the last 35 Postgres readers outside app/ were
# ported in one session. What is left is the retained archive tooling, which
# reads Postgres on purpose: the pool, the backfill, the seeder, the parity
# instruments and their tests. `docs/migration/pg_script_inventory.json` says
# which and why, one row per file, and `test_pg_script_inventory.py` fails if a
# PG-bound file has no row.
#
# This is the last big drop this number can make. From here it moves only if
# retained tooling is deleted — so if you find yourself lowering it again,
# check that a reader was RETIRED and not merely hidden.
SCRIPTS_RATCHET = 74


@pytest.mark.parametrize("rel", INSTRUMENTS)
def test_an_instrument_has_no_postgres_coupling(rel):
    assert (REPO / rel).exists(), f"{rel} is gone — the runbook still names it"
    result = scan(REPO, targets=(rel,))
    assert result["errors"] == [], result["errors"]
    sites = [f for f in result["findings"]]
    assert result["total"] == 0, (
        f"{rel} still reads Postgres: "
        + "; ".join(f"{f['kind']} at line {f['line']}" for f in sites[:5]))


def test_the_scan_finds_couplings_when_they_exist(tmp_path):
    """NEGATIVE CONTROL: a scan that finds nothing because it LOOKED at nothing
    passes the four assertions above just as happily. This pins that the same
    call, pointed at a file that does couple, comes back nonzero."""
    bad = tmp_path / "reader.py"
    bad.write_text(
        "import psycopg\n"
        "def f():\n"
        "    with psycopg.connect('x') as c:\n"
        "        c.execute('SELECT 1 FROM positions')\n",
        encoding="utf-8")
    result = scan(tmp_path, targets=("reader.py",))
    assert result["total"] > 0


def test_the_rest_of_scripts_only_ratchets_down():
    total = scan(REPO, targets=("scripts",))["total"]
    assert total <= SCRIPTS_RATCHET, (
        f"scripts/ grew a Postgres coupling: {total} > {SCRIPTS_RATCHET}. "
        "Convert it, move it under scripts/migration/, or delete it — do not "
        "raise the ratchet.")
