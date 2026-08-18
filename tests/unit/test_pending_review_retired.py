"""Tests that the pending_evolution_fixes subsystem stays retired.

Measured 2026-07-28: the table holds 96 rows (rejected 54, deployed 34,
FAILED_REQUIRES_HUMAN 4, pending 3, error 1) and the newest row with status
``deployed`` is dated 2026-06-01 — nothing has shipped out of it in ~2 months.
It was superseded by CORAL, which stores graded attempts in
``evolution_repair_queue`` + ``evolution_attempts``.

The cost of the old behaviour was diagnostic, not functional: the UI rendered
the 3 leftover ``pending`` rows (2 from May 2026, 1 from 2026-07-27, all
judge_score 1.0 / attempt_count 0) as live queued work, so their presence read
as "the evolution loop is broken" rather than "the evolution loop moved". These
tests pin the two properties that prevent that reading from coming back: the
read path labels every row archived, and the retirement note names CORAL.

The 96 rows themselves are deliberately preserved as historical evidence, so
nothing here asserts the table is empty or dropped.
"""
from __future__ import annotations

import contextlib
from unittest.mock import patch

from app.services.logging import pending_review


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    """Serves the fixture rows to `mongo_query` and records every write.

    `pending_review` is off Postgres: the read is
    `mongo_query.find_rows('pending_evolution_fixes', ...)` and there is no
    `get_db` left to patch. `statements` still exists so the mutator tests can
    prove nothing was written — it now records the Mongo write calls, and the
    "no UPDATE was issued" assertion holds on an empty list either way.
    """

    def __init__(self, rows):
        self._rows = rows
        self.statements: list[str] = []

    def find_rows(self, collection, query=None, columns=None, **kwargs):
        return self._rows

    def update_docs(self, collection, query, update, **kwargs):
        self.statements.append(f"UPDATE {collection}")
        return 0

    def insert_docs(self, collection, docs, **kwargs):
        self.statements.append(f"INSERT {collection}")
        return len(docs)


# A row shaped like the 3 survivors: stored status is still literally "pending",
# judge_score 1.0, never attempted. This is the row that misled the UI.
_STUCK_PENDING_ROW = (
    "fix-may-2026", "cycle-1", "prompt", "some_agent", "diff...",
    "{}", "qwen", "none", 1.0, "pending", None, None,
)


def _fixes_from(rows):
    db = _FakeDb(rows)
    with patch.object(pending_review, "mongo_query", db), \
         patch.object(pending_review, "mongo_store", db):
        return pending_review.get_pending_fixes(), db


# ── The read path must not present these as actionable ──────────────────────

def test_stored_pending_row_is_labelled_archived_not_pending():
    fixes, _ = _fixes_from([_STUCK_PENDING_ROW])
    assert len(fixes) == 1
    fix = fixes[0]

    assert fix["archived"] is True
    assert fix["actionable"] is False
    assert fix["display_status"] == "archived"


def test_raw_status_is_preserved_because_the_rows_are_evidence():
    # Labelling must not rewrite history: the archive keeps saying "pending",
    # only the rendered status changes.
    fixes, _ = _fixes_from([_STUCK_PENDING_ROW])
    assert fixes[0]["status"] == "pending"


def test_every_row_is_labelled_regardless_of_stored_status():
    deployed = list(_STUCK_PENDING_ROW)
    deployed[9] = "deployed"
    rejected = list(_STUCK_PENDING_ROW)
    rejected[9] = "rejected"

    fixes, _ = _fixes_from([_STUCK_PENDING_ROW, tuple(deployed), tuple(rejected)])
    assert len(fixes) == 3
    assert all(f["archived"] for f in fixes)
    assert all(f["display_status"] == "archived" for f in fixes)


def test_rows_are_still_returned_so_the_history_stays_readable():
    # Retirement is a labelling change, not a deletion. If this ever returns
    # nothing, the 96 rows of evidence became invisible instead of archived.
    fixes, _ = _fixes_from([_STUCK_PENDING_ROW])
    assert fixes and fixes[0]["id"] == "fix-may-2026"


# ── The retirement note must name the replacement ───────────────────────────

def test_retirement_note_names_coral_as_the_replacement():
    # Without naming CORAL, a future reader finds a dead table and no forwarding
    # address — which is how this got misdiagnosed the first time.
    assert "CORAL" in pending_review.RETIRED_SUPERSEDED_BY
    assert "evolution_repair_queue" in pending_review.RETIRED_SUPERSEDED_BY
    assert "evolution_attempts" in pending_review.RETIRED_SUPERSEDED_BY
    assert "CORAL" in pending_review.RETIRED_NOTE


def test_module_docstring_carries_the_evidence():
    doc = pending_review.__doc__ or ""
    assert "CORAL" in doc
    assert "2026-06-01" in doc      # last deployment out of the table
    assert "96 rows" in doc         # the row count that makes it evidence
    assert "evolution_repair_queue" in doc


def test_returned_rows_carry_the_forwarding_address():
    fixes, _ = _fixes_from([_STUCK_PENDING_ROW])
    assert "CORAL" in fixes[0]["superseded_by"]
    assert "CORAL" in fixes[0]["retired_note"]


# ── Mutators are inert and never raise ──────────────────────────────────────

def test_approve_is_a_no_op_and_issues_no_update():
    # An approved row would sit forever: the deploy path that consumed
    # 'approved' has not run since 2026-06-01.
    db = _FakeDb([])
    with patch.object(pending_review, "mongo_query", db), \
         patch.object(pending_review, "mongo_store", db):
        result = pending_review.approve_fix("fix-may-2026")

    assert result["archived"] is True
    assert result["actionable"] is False
    assert not any("UPDATE" in s.upper() for s in db.statements)


def test_reject_is_a_no_op_and_issues_no_update():
    db = _FakeDb([])
    with patch.object(pending_review, "mongo_query", db), \
         patch.object(pending_review, "mongo_store", db):
        result = pending_review.reject_fix("fix-may-2026")

    assert result["archived"] is True
    assert not any("UPDATE" in s.upper() for s in db.statements)


def test_mutators_return_instead_of_raising():
    # These sit behind request handlers; a retired subsystem must never become
    # an error path.
    assert pending_review.approve_fix("nope")["status"] == "archived"
    assert pending_review.reject_fix("nope")["status"] == "archived"

# ── The deploy path is GONE, not merely refusing ────────────────────────────
#
# There used to be a test here asserting deploy_fix_to_disk() refused a retired
# fix. On 2026-07-31 the whole evolution deployer was deleted along with the
# autonomous repair loop, and eval_worker stopped dispatching DEPLOY_FIX and
# ROLLBACK_FIX. Deletion is strictly stronger than a refusal, and there is no
# longer a module to point the test at. Nothing in trading-service enqueues
# either command, so removing the consumer strands nothing.
#
# What still needs guarding is that the retirement is not quietly undone:


def test_no_code_path_deploys_a_stored_fix_to_disk():
    """No executable reference to the deploy/rollback commands may return.

    Greps the tree rather than importing, because the thing being asserted is an
    ABSENCE — there is no module left to import, and a test that imports one
    would only prove the module exists.
    """
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    hits = subprocess.run(
        ["git", "grep", "-nE", r"DEPLOY_FIX|ROLLBACK_FIX|deploy_fix_to_disk",
         "--", "app", "scripts"],
        cwd=root, capture_output=True, text=True,
    ).stdout.splitlines()

    live = []
    for hit in hits:
        # "path:lineno:source" — rsplit is wrong, a Windows-ish path could
        # contain ':' but the source certainly can.
        parts = hit.split(":", 2)
        if len(parts) < 3:
            continue
        source = parts[2].strip()
        # Comments explaining the removal are the point; code is not.
        if source.startswith("#"):
            continue
        live.append(hit)

    assert not live, "a deploy-to-disk path came back:\n" + "\n".join(live)
