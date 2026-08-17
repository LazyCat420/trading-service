#!/usr/bin/env python3
"""Move ONE table ONE step along ``pg → dual → mongo_read → mongo``, as a
checked ceremony rather than a hand-edited flag.

Why this exists
---------------
A cutover is currently three edits nobody can review as a unit: a table:mode
pair appended to ``app/db/mongo_backends.env``, the same pair appended to
trading-client's byte-identical copy, and a ``mode_now`` bumped in
``app/db/migration_ledger.json``. Every failure this migration has actually
suffered lives in the gap between those three edits and the evidence that was
supposed to precede them:

  * the map lived in a gitignored, repo-shared ``.env.deploy`` that stood at 30
    tables at ``mongo`` while the containers ran 13 — a cutover nobody chose,
    shipped by an unrelated deploy;
  * ``context_blobs`` was scored OK by a SAMPLED parity check three runs out of
    four while an exhaustive sweep of the same unchanged data found 117 drifted
    ``created_at`` values and 2 permanently missing documents;
  * ``embeddings`` reached ``mongo``, where Postgres is frozen, and a scrub
    script then deleted from the store nobody reads.

So the flag edit is the LAST thing this tool does, and it is the only thing it
does after every precondition has produced a verdict. It refuses far more often
than it promotes, and each refusal names the specific fact that stopped it.

The four tiers
--------------
The tier is a claim about what this table's data is worth, and it selects which
gate applies. It is chosen per table, not derived — deriving it would let a
misclassified shape rubber-stamp a money cutover.

  one-shot  archive-and-cutover. No backfill, no parity: for most tables data
            preservation is deliberately NOT the priority. The DEFAULT. The one
            gate is that the final step to ``mongo`` — the irreversible one —
            names an archive file that exists, because "archive-and-cutover"
            without the archive is just "cutover".
  full      backfill → exhaustive verify → dual soak → mongo_read → read-guard
            window → mongo. For tables whose data must survive. Every step that
            makes Mongo authoritative (``mongo_read``, ``mongo``) demands an
            EXHAUSTIVE parity verdict from scripts/prove_mongo.py. A sampled
            verify can never satisfy this gate; see `check_evidence`.
  drain     queues. A queue must never sit at ``dual`` OR ``mongo_read`` —
            BOTH of those modes write both stores, so a job would be enqueued
            twice and dequeued from one store while the other still offers it.
            The legal path is a single flip pg → mongo with intake stopped and
            the queue observed empty (--drained).
  replay    money and ledger tables (Decimal128, transactional). Refuses to run
            unattended and requires an explicit named human signoff, plus the
            same exhaustive evidence `full` demands.

What it guarantees about the three files
----------------------------------------
It refuses to start on a tree whose map and ledger already disagree — promoting
on top of a drift bakes the drift in. It then edits all three files, runs
scripts/check_backend_map.py over the result, and REVERTS every edit if that
check fails. Under --dry-run (the default) the edit is applied to a temporary
mirror of the tree and the checker is run against THAT, so a dry run reports a
measured verdict rather than a promise.

Note: scripts/build_migration_ledger.py regenerates the ledger with
``promoted_*: null`` hardcoded, so the timestamps this tool stamps there do not
survive a rebuild. The durable per-table cutover record is the --json artifact.

Usage
-----
    python scripts/promote_table.py --table cycle_audit_log --to mongo_read \
        --tier one-shot --json documentation/cutover/cycle_audit_log.json
    python scripts/promote_table.py --table context_blobs --to mongo_read \
        --tier full --evidence documentation/signoff/ --apply
    python scripts/promote_table.py --table trade_results --rollback \
        --accept-stale-reads --apply

Exit codes (house convention, see scripts/check_backend_map.py:27):
    0  the promotion was applied, or --dry-run proved it would apply cleanly
    1  REFUSED — a precondition failed; nothing was edited
    2  a required file is missing or unreadable
    3  the edit was made and then REVERTED because check_backend_map.py failed
       against the result (or, worst case, the revert itself failed)
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
DEFAULT_REPO_ROOT = THIS_FILE.parents[1]

# The ladder. Index in this tuple is the ONLY definition of "one step forward".
MODE_ORDER = ("pg", "dual", "mongo_read", "mongo")

# Both of these write Postgres AND Mongo. That is why neither is legal for a
# queue: a dual-written queue hands the same job out twice.
DUAL_WRITE_MODES = ("dual", "mongo_read")

TIERS = ("one-shot", "full", "drain", "replay")

# The ledger fields that record when a table reached each mode.
STAMP_FIELD = {
    "dual": "promoted_dual",
    "mongo_read": "promoted_mongo_read",
    "mongo": "promoted_mongo",
}

# Check verdicts. Only REFUSE stops the run; WARN and SKIP are recorded and
# printed, because a skipped check that prints nothing is how a drift ships.
PASS = "PASS"
WARN = "WARN"
SKIP = "SKIP"
REFUSE = "REFUSE"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_MISSING = 2
EXIT_REVERTED = 3

# An exhaustive verify is a statement about the data as it was when the sweep
# ran. A week-old sweep of a table taking writes says nothing about today.
DEFAULT_EVIDENCE_MAX_AGE_HOURS = 168

# Signoff strings that are not a person. `replay` exists to put a name on a
# money cutover; "yes" is not a name.
_NOT_A_SIGNOFF = frozenset({
    "y", "yes", "ok", "okay", "true", "1", "me", "auto", "automated", "agent",
    "claude", "bot", "ci", "none", "n/a", "-",
})


# ── result plumbing ────────────────────────────────────────────────────────
@dataclass
class Check:
    """One precondition and its verdict. `status` is all the driver reads."""

    name: str
    status: str
    detail: str
    data: dict = field(default_factory=dict)


def refused(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status == REFUSE]


# ── the flag map ───────────────────────────────────────────────────────────
def load_backend_checker(repo_root: Path):
    """Import scripts/check_backend_map.py fresh, pointed at `repo_root`.

    Fresh every call, and deliberately NOT registered in sys.modules: a test
    module elsewhere in the suite imports the checker under its own name and
    monkeypatches REPO_ROOT, and a shared module object would let one test's
    patched root decide another run's verdict.

    Its `parse_map` is reused rather than reimplemented — a second parser is a
    second opinion about what the committed file says, and the whole point of
    this tool is that there is only one.
    """
    for candidate in (repo_root / "scripts" / "check_backend_map.py",
                      THIS_FILE.parent / "check_backend_map.py"):
        if candidate.exists():
            spec = importlib.util.spec_from_file_location(
                "_promote_table_backend_check", candidate)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.REPO_ROOT = repo_root
            return mod
    raise FileNotFoundError(
        f"scripts/check_backend_map.py not found under {repo_root} or "
        f"{THIS_FILE.parent}")


def map_line_of(path: Path) -> str:
    """The raw MONGO_STORE_BACKEND= line, for the record."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("MONGO_STORE_BACKEND="):
            return line.strip()
    raise ValueError(f"{path} has no MONGO_STORE_BACKEND= line")


def render_map_line(modes: dict[str, str]) -> str:
    return "MONGO_STORE_BACKEND=" + ",".join(f"{t}:{m}" for t, m in modes.items())


def next_modes(current: dict[str, str], table: str, mode: str) -> dict[str, str]:
    """The map with `table` at `mode`.

    A table returning to `pg` is REMOVED rather than written as `table:pg`.
    The file's own header defines the list as the set of tables that are not
    pg ("Anything unlisted defaults to pg"), and a rollback that leaves a
    `:pg` entry behind reads, to a human scanning the line, as a table still
    in flight.
    """
    out = dict(current)
    if mode == "pg":
        out.pop(table, None)
    else:
        out[table] = mode
    return out


def write_map(path: Path, modes: dict[str, str]) -> None:
    """Replace the MONGO_STORE_BACKEND line in place, byte-for-byte elsewhere.

    Every comment above it is load-bearing documentation, and the two repos'
    copies must stay byte-identical — so this rewrites one line and touches
    nothing else, in each repo independently. Copying one file over the other
    would "fix" a pre-existing comment drift without anyone diagnosing it.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    new_line = render_map_line(modes)
    for i, line in enumerate(lines):
        if line.strip().startswith("MONGO_STORE_BACKEND="):
            ending = "\n" if line.endswith("\n") else ""
            lines[i] = new_line + ending
            path.write_text("".join(lines), encoding="utf-8")
            return
    raise ValueError(f"{path} has no MONGO_STORE_BACKEND= line")


# ── the ledger ─────────────────────────────────────────────────────────────
def ledger_row(ledger: dict, table: str) -> dict | None:
    for row in ledger.get("tables", []):
        if row.get("table") == table:
            return row
    return None


def write_ledger_mode(path: Path, table: str, mode: str, when: str) -> None:
    """Set mode_now and the promotion stamps, in build_migration_ledger's format.

    Rolling back CLEARS the stamps for every mode at or above the new one: a
    ledger that still claims `promoted_mongo` for a table now at `dual` is a
    record of a promotion that was undone, and the next audit reads it as
    finished work.
    """
    ledger = json.loads(path.read_text(encoding="utf-8"))
    row = ledger_row(ledger, table)
    if row is None:
        raise KeyError(f"{table} is not in {path}")
    row["mode_now"] = mode
    target_idx = MODE_ORDER.index(mode)
    for m, fieldname in STAMP_FIELD.items():
        idx = MODE_ORDER.index(m)
        if idx == target_idx:
            row[fieldname] = when
        elif idx > target_idx:
            row[fieldname] = None
    path.write_text(json.dumps(ledger, indent=2, sort_keys=False) + "\n",
                    encoding="utf-8")


# ── evidence ───────────────────────────────────────────────────────────────
def _find_bundle(payload: dict, table: str) -> dict | None:
    """A prove_mongo artifact is either one bundle or {"tables": [bundle...]}."""
    if payload.get("table") == table:
        return payload
    for bundle in payload.get("tables", []) or []:
        if isinstance(bundle, dict) and bundle.get("table") == table:
            return bundle
    return None


def check_evidence(path_arg: str | None, table: str, current_mode: str,
                   max_age_hours: float, now: datetime) -> Check:
    """The exhaustive-parity gate.

    Reads a scripts/prove_mongo.py signoff bundle and REFUSES unless its
    `parity` check both PASSED and was produced by the exhaustive sweep. This
    is the check the migration has already been burned by: `--verify-fields N`
    draws N random rows, so its verdict depends on the draw, and it scored
    context_blobs OK three runs out of four while a full sweep of the same data
    found 117 drifted timestamps and 2 missing documents.

    Every unreadable input below is a REFUSE, never a pass. A missing input is
    the absence of a verdict, and this tool's entire value is that it does not
    read absence as agreement.
    """
    if not path_arg:
        return Check("evidence", REFUSE,
                     "this tier requires an exhaustive parity verdict and no "
                     "--evidence bundle was given. Produce one with: "
                     f"python scripts/prove_mongo.py --table {table} "
                     f"--json signoff_{table}.json")

    path = Path(path_arg)
    if path.is_dir():
        # prove_mongo --all --json DIR/ writes prove_mongo_<table>.json
        path = path / f"prove_mongo_{table}.json"
    if not path.exists():
        return Check("evidence", REFUSE, f"evidence bundle {path} does not exist")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse failure denies the gate
        return Check("evidence", REFUSE,
                     f"evidence bundle {path} is unreadable: "
                     f"{type(exc).__name__}: {exc}")

    bundle = _find_bundle(payload, table)
    if bundle is None:
        return Check("evidence", REFUSE,
                     f"evidence bundle {path} contains no entry for {table!r} "
                     "— evidence about another table is not evidence about "
                     "this one")

    checks = {c.get("name"): c for c in bundle.get("checks", []) or []}
    parity = checks.get("parity")
    if parity is None:
        return Check("evidence", REFUSE,
                     f"evidence bundle {path} has no `parity` check at all")

    mode_run = str((parity.get("data") or {}).get("mode_run") or "")
    if not mode_run:
        return Check("evidence", REFUSE,
                     "the parity check does not record which mode produced it, "
                     "so a sampled verdict cannot be told from an exhaustive "
                     "one. An unlabelled number is not an exhaustive verify")
    if "exhaustive" not in mode_run.lower():
        return Check("evidence", REFUSE,
                     f"the parity verdict came from {mode_run!r}, not the "
                     "exhaustive sweep. A sampled verify is not a parity "
                     "guarantee — it scored context_blobs OK three runs out of "
                     "four while a full sweep found 117 drifted timestamps and "
                     "2 missing documents. Re-run with "
                     "pg_to_mongo_backfill.py --verify-all",
                     {"mode_run": mode_run})

    if parity.get("status") != PASS:
        return Check("evidence", REFUSE,
                     f"the exhaustive parity check is {parity.get('status')!r}, "
                     f"not PASS: {parity.get('headline', '')}",
                     {"mode_run": mode_run})

    data = parity.get("data") or {}
    defects = {k: data[k] for k in ("missing_in_mongo", "drifted_fields")
               if data.get(k)}
    if defects:
        return Check("evidence", REFUSE,
                     f"the exhaustive sweep reported defects: {defects}",
                     {"mode_run": mode_run, **defects})

    # Age. Parity is a statement about the data at sweep time.
    generated = bundle.get("generated_at") or payload.get("generated_at")
    age_hours = None
    if generated:
        try:
            stamp = datetime.fromisoformat(str(generated))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age_hours = (now - stamp).total_seconds() / 3600.0
        except ValueError:
            generated = None
    if generated is None:
        return Check("evidence", REFUSE,
                     f"evidence bundle {path} has no readable generated_at, so "
                     "its age cannot be bounded")
    if age_hours > max_age_hours:
        return Check("evidence", REFUSE,
                     f"the exhaustive verdict is {age_hours:.0f}h old "
                     f"(limit {max_age_hours:.0f}h). Parity describes the data "
                     "at sweep time; this table has taken writes since",
                     {"age_hours": round(age_hours, 1)})

    # Everything else in the bundle is context, not this gate -- but a FAIL
    # anywhere still denies the promotion, and an unrun check is reported.
    other_fail = [n for n, c in checks.items()
                  if n != "parity" and c.get("status") == "FAIL"]
    if other_fail:
        return Check("evidence", REFUSE,
                     f"parity passed but these checks FAILED in the same "
                     f"bundle: {', '.join(sorted(other_fail))}")

    unrun = [n for n, c in checks.items() if c.get("status") == "INSUFFICIENT"]
    detail = (f"exhaustive parity PASS from {path}, {age_hours:.0f}h old "
              f"({mode_run})")
    if unrun:
        return Check("evidence", WARN,
                     detail + f"; these checks produced no verdict: "
                              f"{', '.join(sorted(unrun))}",
                     {"mode_run": mode_run, "age_hours": round(age_hours, 1),
                      "unrun": sorted(unrun)})
    if bundle.get("mode") and bundle["mode"] != current_mode:
        return Check("evidence", WARN,
                     detail + f"; the bundle was gathered at mode "
                              f"{bundle['mode']!r}, the map says {current_mode!r}",
                     {"mode_run": mode_run, "age_hours": round(age_hours, 1)})
    return Check("evidence", PASS, detail,
                 {"mode_run": mode_run, "age_hours": round(age_hours, 1),
                  "bundle": str(path)})


# ── preconditions ──────────────────────────────────────────────────────────
def preflight(*, row: dict | None, table: str, current_mode: str,
              ledger_mode: str | None, target: str, action: str, tier: str,
              args, repo_root: Path, now: datetime) -> list[Check]:
    """Every precondition, each producing its own verdict.

    Runs to completion instead of stopping at the first REFUSE: an operator who
    is two gates away from a promotion should learn that in one run, not two.
    """
    checks: list[Check] = []

    # 1. the table is in the ledger, and the ledger says it is ours to migrate.
    if row is None:
        checks.append(Check("ledger_row", REFUSE,
                            f"{table!r} is not in app/db/migration_ledger.json. "
                            "The ledger is the scope of this migration; a table "
                            "outside it has no shape, no disposition and no "
                            "row count, so nothing here can be checked"))
        return checks
    checks.append(Check("ledger_row", PASS,
                        f"shape={row.get('shape')} "
                        f"numeric_policy={row.get('numeric_policy')} "
                        f"rows={row.get('row_count')}"))

    disposition = row.get("disposition")
    if disposition == "migrate":
        checks.append(Check("disposition", PASS, "disposition=migrate"))
    elif disposition == "absent":
        checks.append(Check("disposition", REFUSE,
                            f"{table!r} is disposition=absent: it is named in "
                            "the code but does not exist in the database. There "
                            "is nothing to promote and a flag for it would make "
                            "the map describe a table that is not there"))
    elif disposition == "archive-only":
        checks.append(Check("disposition", REFUSE,
                            f"{table!r} is disposition=archive-only: it was "
                            "classified as dump-and-drop, not migrate. Promoting "
                            "it moves data the migration decided not to keep, and "
                            "hides a table that should be leaving. Re-classify it "
                            "in the ledger first if that decision was wrong"))
    else:
        checks.append(Check("disposition", REFUSE,
                            f"{table!r} has disposition={disposition!r}, which is "
                            "not one of migrate / archive-only / absent"))

    # 2. the two files must already agree about where this table is. Promoting
    #    on top of a drift bakes the drift in and makes the artifact a lie.
    if ledger_mode != current_mode:
        checks.append(Check("map_ledger_agree", REFUSE,
                            f"mongo_backends.env says {current_mode!r} and the "
                            f"ledger says {ledger_mode!r} for {table!r}. Resolve "
                            "that first (scripts/check_backend_map.py); a "
                            "promotion computed from a disagreement lands on "
                            "whichever file this tool happened to read"))
    else:
        checks.append(Check("map_ledger_agree", PASS,
                            f"both files say {current_mode!r}"))

    # 3. the transition itself.
    checks.append(check_transition(current_mode, target, action, tier,
                                   force=args.force,
                                   accept_stale=args.accept_stale_reads,
                                   table=table))

    # 4. the tier must match what the table IS, before it gates anything.
    checks.append(check_tier_fits_shape(row, tier, table, action))

    # 5. the tier's own gate.
    checks.extend(check_tier_gate(row=row, table=table, tier=tier, target=target,
                                  action=action, current_mode=current_mode,
                                  args=args, repo_root=repo_root, now=now))
    return checks


def check_transition(current: str, target: str, action: str, tier: str,
                     *, force: bool, accept_stale: bool, table: str) -> Check:
    """One step, in the direction the action claims, or an explicit override."""
    cur_i = MODE_ORDER.index(current)
    tgt_i = MODE_ORDER.index(target)

    if action == "rollback":
        if cur_i == 0:
            return Check("transition", REFUSE,
                         f"{table!r} is already at 'pg'; there is nothing to roll "
                         "back to")
        # Rolling reads back OUT of `mongo` is the one move that fails silently.
        if current == "mongo" and not accept_stale:
            return Check("transition", REFUSE,
                         f"refusing to roll {table!r} back from 'mongo' to "
                         f"{target!r} without --accept-stale-reads. At 'mongo' "
                         "Postgres is FROZEN: every write since the cutover "
                         "exists only in Mongo, so pointing reads back at "
                         "Postgres does not fail — it serves stale rows that "
                         "look current, forever, with nothing raising. "
                         "(embeddings has been at 'mongo' since 2026-07-25 with "
                         "701 vectors written only to Mongo.) If you accept "
                         "that, pass --accept-stale-reads and backfill Postgres "
                         "from Mongo before anyone reads it.")
        return Check("transition", PASS if current != "mongo" else WARN,
                     f"rollback {current} → {target}" +
                     (" (stale-read exposure acknowledged)"
                      if current == "mongo" else ""))

    # promote
    if tgt_i == cur_i:
        return Check("transition", REFUSE,
                     f"{table!r} is already at {current!r}; nothing to do")
    if tgt_i < cur_i:
        return Check("transition", REFUSE,
                     f"{target!r} is BEHIND {current!r}. Going backwards is a "
                     "rollback and has its own preconditions — use --rollback")

    # `drain` flips a queue pg → mongo in one move on purpose: the intermediate
    # modes dual-write, which is the one thing a queue must never do. So the
    # multi-step jump is the ceremony for that tier, not a skipped step.
    if tier == "drain" and current == "pg" and target == "mongo":
        return Check("transition", PASS,
                     "drain flip pg → mongo (the intermediate modes dual-write, "
                     "which a queue must never do)")

    if tgt_i != cur_i + 1:
        if not force:
            return Check("transition", REFUSE,
                         f"{current!r} → {target!r} skips "
                         f"{', '.join(MODE_ORDER[cur_i + 1:tgt_i])}. Each step "
                         "exists to be observed: `dual` proves the mirror writes, "
                         "`mongo_read` proves the readers moved while Postgres is "
                         "still correct. Promote one step at a time, or pass "
                         "--force")
        return Check("transition", WARN,
                     f"FORCED {current!r} → {target!r}, skipping "
                     f"{', '.join(MODE_ORDER[cur_i + 1:tgt_i])}. No soak was "
                     "observed at the skipped mode(s)")
    return Check("transition", PASS, f"one step forward: {current} → {target}")


def check_tier_fits_shape(row: dict, tier: str, table: str,
                          action: str = "promote") -> Check:
    """The tier must not be weaker than what the ledger says the table is.

    Chosen per table, but not freely: money and queues have failure modes the
    other tiers do not check for at all.

    A rollback is exempt. The tier describes what a PROMOTION must prove, and
    demanding a money ceremony to UNDO a money promotion would leave the one
    table that most needs a retreat unable to make one.
    """
    if action == "rollback":
        return Check("tier_fits_shape", SKIP,
                     "a rollback is not tier-gated; the tier describes what a "
                     "promotion must prove")
    shape = row.get("shape")
    numeric = row.get("numeric_policy")

    if shape == "money" or numeric == "dec128":
        if tier != "replay":
            return Check("tier_fits_shape", REFUSE,
                         f"{table!r} is shape={shape!r} "
                         f"numeric_policy={numeric!r} — money. Tier {tier!r} does "
                         "not check anything money needs: Decimal128 round-trip, "
                         "a transactional write, and a P&L replay against "
                         "Postgres truth. A float round-trip through JSON loses "
                         "cents silently and no parity count would show it. Use "
                         "--tier replay")
        return Check("tier_fits_shape", PASS,
                     f"money ({shape}/{numeric}) on the replay tier")

    if shape == "queue":
        if tier != "drain":
            return Check("tier_fits_shape", REFUSE,
                         f"{table!r} is shape=queue. Tier {tier!r} would walk it "
                         "through 'dual' and/or 'mongo_read', and BOTH of those "
                         "write to both stores — the same job would be enqueued "
                         "twice and dequeued from one store while the other still "
                         "offers it. Use --tier drain")
        return Check("tier_fits_shape", PASS, "queue on the drain tier")

    if tier == "drain":
        return Check("tier_fits_shape", WARN,
                     f"{table!r} is shape={shape!r}, not a queue, but is being "
                     "flipped on the drain tier — no parity evidence will be "
                     "demanded")
    return Check("tier_fits_shape", PASS, f"shape={shape!r} on tier {tier!r}")


def check_tier_gate(*, row: dict, table: str, tier: str, target: str,
                    action: str, current_mode: str, args, repo_root: Path,
                    now: datetime) -> list[Check]:
    """The per-tier evidence gate. Rollbacks are ungated by design.

    A rollback moves authority back towards Postgres, which is the direction
    that is safe by construction everywhere except out of `mongo` — and that
    one is handled in `check_transition`, where it belongs.
    """
    if action == "rollback":
        return [Check("tier_gate", SKIP,
                      "rollbacks are not evidence-gated; they move authority "
                      "back towards Postgres")]

    out: list[Check] = []

    if tier == "replay":
        out.append(check_unattended())
        out.append(check_signoff(args.signoff))

    if tier in ("full", "replay"):
        if target in ("mongo_read", "mongo"):
            out.append(check_evidence(args.evidence, table, current_mode,
                                      args.max_evidence_age_hours, now))
        else:
            # pg → dual makes Mongo a MIRROR, not an authority: Postgres still
            # serves every read, so there is nothing yet to be wrong about.
            out.append(Check("evidence", SKIP,
                             f"{target!r} does not make Mongo authoritative; the "
                             "exhaustive parity gate applies at 'mongo_read' and "
                             "'mongo'"))

    if tier == "drain":
        out.append(check_drained(args.drained, table, target))

    if tier == "one-shot":
        out.append(check_archive(row, args.archive, table, target, repo_root))

    return out


def check_unattended() -> Check:
    """`replay` must not run from a scheduler.

    A money cutover is the one operation here with no cheap undo: at `mongo`
    Postgres is frozen and the P&L that replayed against it is the last check
    anybody gets. `CI`/`IS_TOOL_PROCESS` are the markers this repo already uses
    for "no human is watching" (pg_write_guard._is_script_context).
    """
    markers = [name for name in ("CI", "IS_TOOL_PROCESS")
               if os.getenv(name, "").strip().lower() in ("1", "true", "yes")]
    if markers:
        return Check("unattended", REFUSE,
                     f"the replay tier refuses to run unattended and "
                     f"{', '.join(markers)} is set in the environment. A money "
                     "cutover is signed off by a person at a terminal, not by a "
                     "scheduler")
    return Check("unattended", PASS, "no unattended-execution marker in the env")


def check_signoff(signoff: str | None) -> Check:
    if not signoff or not signoff.strip():
        return Check("signoff", REFUSE,
                     "the replay tier requires --signoff 'NAME' — the person "
                     "who ran the P&L replay against Postgres truth and read "
                     "the result. This is recorded in the cutover artifact")
    value = signoff.strip()
    if value.lower() in _NOT_A_SIGNOFF:
        return Check("signoff", REFUSE,
                     f"--signoff {value!r} is a confirmation, not a name. The "
                     "artifact has to say who accepted the money cutover")
    return Check("signoff", PASS, f"signed off by {value!r}")


def check_drained(drained: bool, table: str, target: str) -> Check:
    if target in DUAL_WRITE_MODES:
        return Check("drain_state", REFUSE,
                     f"{target!r} writes BOTH stores, and a queue must never be "
                     "dual-written. Stop intake, drain to empty, and flip "
                     "pg → mongo in one move")
    if not drained:
        return Check("drain_state", REFUSE,
                     f"the drain tier requires --drained: intake stopped and "
                     f"{table!r} observed EMPTY. Flipping a queue with rows still "
                     "in Postgres strands them — the new consumer reads Mongo and "
                     "the remaining jobs are never handed out again. This tool "
                     "does not touch the database, so the emptiness is your "
                     "observation and it is recorded as yours in the artifact")
    return Check("drain_state", PASS,
                 "operator asserts intake stopped and the queue drained empty")


def check_archive(row: dict, archive_arg: str | None, table: str, target: str,
                  repo_root: Path) -> Check:
    """one-shot's only gate, and only on the irreversible step.

    The tier is called archive-and-cutover. Without a file that exists, it is
    just cutover, and `mongo` is where Postgres stops being written — the last
    moment the Postgres rows can be dumped.
    """
    if target != "mongo":
        return Check("archive", SKIP,
                     f"one-shot demands the archive at the final step; {target!r} "
                     "still writes Postgres")
    candidate = archive_arg or row.get("archive_file")
    if not candidate:
        return Check("archive", REFUSE,
                     f"one-shot is archive-and-cutover, and neither --archive nor "
                     f"the ledger's archive_file names a dump for {table!r}. "
                     "'mongo' freezes the Postgres rows; this is the last step at "
                     "which they can be archived")
    path = Path(candidate)
    if not path.is_absolute():
        path = repo_root / path
    if not path.exists():
        return Check("archive", REFUSE,
                     f"the named archive {path} does not exist. A path in a "
                     "field is not a dump on disk")
    size = path.stat().st_size
    if size == 0:
        return Check("archive", REFUSE,
                     f"the named archive {path} is 0 bytes — an empty file "
                     "satisfies an existence check and preserves nothing")
    return Check("archive", PASS, f"archive {path} ({size:,} bytes)")


# ── applying the edit ──────────────────────────────────────────────────────
def run_invariant(repo_root: Path, sibling: Path) -> tuple[int, str]:
    """Run check_backend_map.py over `repo_root`, capturing its report.

    `--sibling` is passed ALWAYS, including when that path holds no checkout.
    Omitting it lets the checker fall back to its own default sibling, and the
    two tools then disagree about which trading-client is under discussion:
    this tool would skip an absent --client while the checker compared against
    a different one it found on its own. One path, named once.
    """
    cbm = load_backend_checker(repo_root)
    argv = ["--sibling", str(sibling)]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cbm.main(argv)
    return rc, buf.getvalue().strip()


@dataclass
class Edit:
    """The three files a cutover touches, and their pre-edit bytes."""

    repo_env: Path
    ledger: Path
    client_env: Path | None
    _snapshots: dict[Path, bytes] = field(default_factory=dict)

    def targets(self) -> list[Path]:
        return [p for p in (self.repo_env, self.ledger, self.client_env) if p]

    def snapshot(self) -> None:
        for p in self.targets():
            self._snapshots[p] = p.read_bytes()

    def revert(self) -> list[str]:
        """Put every touched file back. Returns the paths it could not restore."""
        failed = []
        for p, blob in self._snapshots.items():
            try:
                p.write_bytes(blob)
            except Exception:  # noqa: BLE001 - report, never swallow
                failed.append(str(p))
        return failed


def apply_edit(edit: Edit, table: str, target: str, modes_after: dict[str, str],
               when: str) -> None:
    """Write all three files. Ordering is irrelevant — the verify follows."""
    write_map(edit.repo_env, modes_after)
    if edit.client_env:
        write_map(edit.client_env, modes_after)
    write_ledger_mode(edit.ledger, table, target, when)


def mirror_tree(repo_root: Path, client_root: Path | None,
                dest: Path) -> tuple[Path, Path]:
    """A throwaway copy of just the files the invariant reads.

    This is what makes --dry-run a measurement: the edit is really applied,
    check_backend_map really runs, and the verdict comes back from the same
    code path a real promotion would take.

    The mirrored client path is always RETURNED but only CREATED when there is
    a client, so an absent one produces the same SKIP in the mirror that it
    would produce live.
    """
    service = dest / "service"
    (service / "app" / "db").mkdir(parents=True)
    shutil.copy2(repo_root / "app" / "db" / "mongo_backends.env",
                 service / "app" / "db" / "mongo_backends.env")
    shutil.copy2(repo_root / "app" / "db" / "migration_ledger.json",
                 service / "app" / "db" / "migration_ledger.json")
    client = dest / "client"
    if client_root:
        (client / "app" / "db").mkdir(parents=True)
        shutil.copy2(client_root / "app" / "db" / "mongo_backends.env",
                     client / "app" / "db" / "mongo_backends.env")
    return service, client


# ── the record ─────────────────────────────────────────────────────────────
def git_head(repo_root: Path) -> dict:
    """Which commit this cutover was performed from. A record that cannot be
    tied to a tree state cannot be replayed."""
    def _run(*cmd: str) -> str | None:
        try:
            out = subprocess.run(cmd, cwd=str(repo_root), capture_output=True,
                                 text=True, timeout=10)
            return out.stdout.strip() if out.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None
    dirty = _run("git", "status", "--porcelain")
    return {"head": _run("git", "rev-parse", "HEAD"),
            "branch": _run("git", "rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(dirty) if dirty is not None else None}


def render(record: dict) -> str:
    bar = "=" * 78
    out = [bar,
           f"PROMOTE {record['table']}   {record['from_mode']} → "
           f"{record['to_mode']}   (tier: {record['tier']}, "
           f"action: {record['action']})",
           f"generated {record['timestamp']}",
           bar]
    for c in record["checks"]:
        out.append(f"[{c['status']:6}] {c['name']}: {c['detail']}")
    out.append("-" * 78)
    out.append(f"STATUS: {record['status']}")
    return "\n".join(out)


def write_record(path_arg: str | None, record: dict) -> None:
    if not path_arg:
        return
    path = Path(path_arg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, default=str) + "\n",
                    encoding="utf-8")
    print(f"\nwrote cutover record {path}")


# ── CLI ────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Promote one table one step along pg → dual → mongo_read → mongo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 applied/would-apply, 1 refused, 2 missing file, "
               "3 applied then reverted.")
    ap.add_argument("--table", required=True, help="the table to move")
    ap.add_argument("--to", choices=MODE_ORDER, help="the mode to promote to")
    ap.add_argument("--rollback", action="store_true",
                    help="move back one step instead")
    ap.add_argument("--tier", choices=TIERS, default="one-shot",
                    help="ceremony tier (default: one-shot, archive-and-cutover)")

    ap.add_argument("--apply", action="store_true",
                    help="really edit the files (default is --dry-run)")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                    help="show and VERIFY the edit against a temp mirror without "
                         "touching the repo (the default)")

    ap.add_argument("--evidence", metavar="PATH",
                    help="scripts/prove_mongo.py signoff bundle (or a directory "
                         "of them) carrying the exhaustive parity verdict")
    ap.add_argument("--max-evidence-age-hours", type=float,
                    default=DEFAULT_EVIDENCE_MAX_AGE_HOURS,
                    help="how old an exhaustive verdict may be "
                         f"(default {DEFAULT_EVIDENCE_MAX_AGE_HOURS:.0f}h)")
    ap.add_argument("--signoff", metavar="NAME",
                    help="the person accepting a `replay`-tier money cutover")
    ap.add_argument("--drained", action="store_true",
                    help="drain tier: intake is stopped and the queue is EMPTY")
    ap.add_argument("--archive", metavar="PATH",
                    help="one-shot tier: the dump taken before the final flip")

    ap.add_argument("--force", action="store_true",
                    help="allow a multi-step jump (loudly; no soak is observed)")
    ap.add_argument("--accept-stale-reads", action="store_true",
                    help="acknowledge that rolling back out of `mongo` points "
                         "reads at frozen Postgres rows")
    ap.add_argument("--allow-missing-client", action="store_true",
                    help="--apply without trading-client present, accepting that "
                         "the two containers' maps will differ")

    ap.add_argument("--json", dest="json_path", metavar="PATH",
                    help="write the per-table cutover record here")
    ap.add_argument("--repo-root", default=str(DEFAULT_REPO_ROOT),
                    help="trading-service checkout (default: this script's repo)")
    ap.add_argument("--client", metavar="PATH", default=None,
                    help="trading-client checkout, whose copy of the map must "
                         "stay byte-identical (default: sibling of --repo-root)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    when = now.isoformat()
    repo_root = Path(args.repo_root).resolve()

    if args.rollback and args.to:
        print("FAIL: --rollback computes its own target; do not also pass --to",
              file=sys.stderr)
        return EXIT_REFUSED
    if not args.rollback and not args.to:
        print("FAIL: one of --to MODE or --rollback is required", file=sys.stderr)
        return EXIT_REFUSED

    env_path = repo_root / "app" / "db" / "mongo_backends.env"
    ledger_path = repo_root / "app" / "db" / "migration_ledger.json"
    for p in (env_path, ledger_path):
        if not p.exists():
            print(f"FAIL: {p} is missing", file=sys.stderr)
            return EXIT_MISSING

    try:
        cbm = load_backend_checker(repo_root)
        flags = cbm.parse_map(env_path)
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any load failure is fatal
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_MISSING

    # The client copy. Absent is a SKIP, never a silent one: the two containers
    # stage their own copies, so a service-only edit splits them.
    client_root: Path | None = None
    client_state: str
    default_client = repo_root.parent / "trading-client"
    # `client_candidate` is the ONE path both this tool and check_backend_map
    # are told about, whether or not a checkout is there.
    client_candidate = (Path(args.client).resolve() if args.client
                        else default_client)
    client_env = client_candidate / "app" / "db" / "mongo_backends.env"
    if client_env.exists():
        client_root = client_candidate
        client_state = "will be updated"
    else:
        client_state = f"ABSENT at {client_env}"

    row = ledger_row(ledger, args.table)
    current_mode = flags.get(args.table, "pg")
    ledger_mode = row.get("mode_now") if row else None

    action = "rollback" if args.rollback else "promote"
    if args.rollback:
        idx = MODE_ORDER.index(current_mode)
        target = MODE_ORDER[max(idx - 1, 0)]
    else:
        target = args.to

    checks = preflight(row=row, table=args.table, current_mode=current_mode,
                       ledger_mode=ledger_mode, target=target, action=action,
                       tier=args.tier, args=args, repo_root=repo_root, now=now)

    # The tree must already be consistent. This is run BEFORE any edit for the
    # same reason the tool exists: a promotion computed on top of an existing
    # drift bakes the drift in and the artifact records a state nobody chose.
    try:
        rc, report = run_invariant(repo_root, client_candidate)
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("preflight_invariant", REFUSE,
                            f"could not run check_backend_map.py: "
                            f"{type(exc).__name__}: {exc}"))
        rc, report = 2, ""
    else:
        checks.append(Check("preflight_invariant",
                            PASS if rc == 0 else REFUSE,
                            f"check_backend_map.py rc={rc}: "
                            f"{report.splitlines()[0] if report else ''}",
                            {"returncode": rc, "report": report}))

    if client_root is None:
        checks.append(Check("client_copy", WARN if not args.apply else
                            (WARN if args.allow_missing_client else REFUSE),
                            f"trading-client is {client_state}. Its copy of the "
                            "map is staged into its own container, so leaving it "
                            "behind means the service and the client disagree "
                            "about which store is authoritative. Point at it "
                            "with --client PATH" +
                            ("" if args.allow_missing_client else
                             ", or pass --allow-missing-client to accept the "
                             "drift")))
    else:
        checks.append(Check("client_copy", PASS,
                            f"trading-client at {client_root}"))

    record = {
        "tool": "scripts/promote_table.py",
        "schema_version": 1,
        "table": args.table,
        "action": action,
        "tier": args.tier,
        "from_mode": current_mode,
        "to_mode": target,
        "timestamp": when,
        "dry_run": not args.apply,
        "applied": False,
        "status": "REFUSED",
        "repo_root": str(repo_root),
        "client_root": str(client_root) if client_root else None,
        "client_state": client_state,
        "git": git_head(repo_root),
        "flags": {"force": args.force, "evidence": args.evidence,
                  "signoff": args.signoff, "drained": args.drained,
                  "archive": args.archive,
                  "accept_stale_reads": args.accept_stale_reads},
        "checks": [c.__dict__ for c in checks],
        "map_line_before": map_line_of(env_path),
        "map_line_after": None,
    }

    blockers = refused(checks)
    if blockers:
        record["status"] = "REFUSED"
        print(render(record))
        print("\nREFUSED — nothing was edited:")
        for c in blockers:
            print(f"  - {c.name}: {c.detail}")
        write_record(args.json_path, record)
        return EXIT_REFUSED

    modes_after = next_modes(flags, args.table, target)
    record["map_line_after"] = render_map_line(modes_after)

    if not args.apply:
        # Prove the edit against a mirror rather than promising it.
        with tempfile.TemporaryDirectory(prefix="promote_table_dryrun_") as tmp:
            svc, cli = mirror_tree(repo_root, client_root, Path(tmp))
            edit = Edit(repo_env=svc / "app" / "db" / "mongo_backends.env",
                        ledger=svc / "app" / "db" / "migration_ledger.json",
                        client_env=(cli / "app" / "db" / "mongo_backends.env")
                        if client_root else None)
            apply_edit(edit, args.table, target, modes_after, when)
            rc, report = run_invariant(svc, cli)
        checks.append(Check("post_edit_invariant", PASS if rc == 0 else REFUSE,
                            f"check_backend_map.py on a temp mirror of the edit: "
                            f"rc={rc}: {report}", {"returncode": rc}))
        record["checks"] = [c.__dict__ for c in checks]
        record["status"] = "WOULD APPLY" if rc == 0 else "WOULD BE REVERTED"
        print(render(record))
        print(f"\n  - {record['map_line_before']}")
        print(f"  + {record['map_line_after']}")
        print(f"  ledger: {args.table}.mode_now {current_mode} → {target}")
        print("\nDRY RUN — no file in the repo was touched. Re-run with --apply.")
        write_record(args.json_path, record)
        return EXIT_OK if rc == 0 else EXIT_REVERTED

    edit = Edit(repo_env=env_path, ledger=ledger_path,
                client_env=(client_root / "app" / "db" / "mongo_backends.env")
                if client_root else None)
    edit.snapshot()
    apply_edit(edit, args.table, target, modes_after, when)
    try:
        rc, report = run_invariant(repo_root, client_candidate)
    except Exception as exc:  # noqa: BLE001 - an unverifiable edit is reverted
        rc, report = 2, f"{type(exc).__name__}: {exc}"

    if rc != 0:
        failed = edit.revert()
        checks.append(Check("post_edit_invariant", REFUSE,
                            f"check_backend_map.py rejected the edit (rc={rc}); "
                            f"the edit was REVERTED: {report}",
                            {"returncode": rc, "revert_failures": failed}))
        record["checks"] = [c.__dict__ for c in checks]
        record["status"] = "REVERTED"
        print(render(record))
        print(f"\nREVERTED — check_backend_map.py failed after the edit:\n{report}")
        if failed:
            print("\n*** THE REVERT ITSELF FAILED for: " + ", ".join(failed) +
                  "\n*** These files are in an unverified state. Restore them "
                  "from git before any deploy.", file=sys.stderr)
        write_record(args.json_path, record)
        return EXIT_REVERTED

    checks.append(Check("post_edit_invariant", PASS,
                        f"check_backend_map.py rc=0: {report}"))
    record["checks"] = [c.__dict__ for c in checks]
    record["applied"] = True
    record["status"] = "APPLIED"
    record["files_changed"] = [str(p) for p in edit.targets()]
    print(render(record))
    print(f"\n  - {record['map_line_before']}")
    print(f"  + {record['map_line_after']}")
    print("\nAPPLIED. Both repos now carry the same map; commit and deploy BOTH.")
    write_record(args.json_path, record)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
