"""The promotion evidence bundle must not be able to manufacture a PASS.

`scripts/prove_mongo.py` exists because three separate instruments were caught
reporting a clean that was not clean: a sampled parity check that scored
`context_blobs` OK three runs out of four, six mirror-failure log sites at DEBUG
under an INFO root logger, and a flag map that lived in a gitignored file.

So the thing worth testing here is not that the tool prints nicely. It is that
every path where a check could not run ends at INSUFFICIENT rather than PASS,
and that each individual instrument can actually FIRE -- a detector that
silently matches nothing would pass a "zero hits" assertion forever. Every
detector below therefore gets a positive control as well as its clean case.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Loaded by path rather than by putting scripts/ on sys.path: that directory
# holds ~120 top-level modules and a `db/` package, and adding it to the import
# path for the whole session lets any of them shadow a real module in a later
# test file.
_SPEC = importlib.util.spec_from_file_location(
    "prove_mongo", Path(__file__).resolve().parents[2] / "scripts" / "prove_mongo.py")
pm = importlib.util.module_from_spec(_SPEC)
sys.modules["prove_mongo"] = pm
_SPEC.loader.exec_module(pm)


# ── the verdict itself ─────────────────────────────────────────────────────

def test_a_single_unrun_check_denies_the_pass():
    """The whole point: a missing input must never read as a pass."""
    assert pm.worst([pm.PASS, pm.PASS, pm.INSUFFICIENT]) == pm.INSUFFICIENT


def test_a_failure_outranks_an_unrun_check():
    assert pm.worst([pm.INSUFFICIENT, pm.FAIL, pm.PASS]) == pm.FAIL


def test_no_checks_at_all_is_not_a_pass():
    """A bundle that measured nothing is the most insufficient thing there is."""
    assert pm.worst([]) == pm.INSUFFICIENT


def test_exit_codes_are_distinct_and_documented():
    assert pm.EXIT_BY_STATUS == {pm.PASS: 0, pm.FAIL: 1, pm.INSUFFICIENT: 2}
    assert pm.EXIT_CANNOT_START == 3


def test_the_rendered_verdict_says_it_is_not_a_pass():
    bundle = {
        "table": "t", "mode": "dual", "verdict": pm.INSUFFICIENT,
        "generated_at": "now",
        "checks": [{"name": "logs", "status": pm.INSUFFICIENT, "headline": "dead feed",
                    "lines": [], "data": {}}],
    }
    text = pm.render(bundle)
    assert "INSUFFICIENT EVIDENCE" in text
    assert "This is not a pass" in text
    assert "logs" in text


# ── check: flags ───────────────────────────────────────────────────────────

@pytest.fixture
def fake_maps(monkeypatch):
    def _set(flags: dict, ledger: dict):
        monkeypatch.setattr(pm, "committed_backend_map", lambda: flags)
        monkeypatch.setattr(pm, "ledger_rows", lambda: {
            t: {"table": t, "mode_now": m, "row_count": 1, "shape": "append",
                "key_field": "id", "natural_key": None}
            for t, m in ledger.items()})
    return _set


def test_flags_agree(fake_maps):
    fake_maps({"embeddings": "mongo"}, {"embeddings": "mongo"})
    c = pm.check_flags("embeddings", {})
    assert c.status == pm.PASS
    assert c.data["effective_mode"] == "mongo"


def test_flags_disagreeing_is_a_failure(fake_maps):
    """Ledger `mongo` + map `dual` means the migration believes a promotion the
    containers never received."""
    fake_maps({"embeddings": "dual"}, {"embeddings": "mongo"})
    c = pm.check_flags("embeddings", {})
    assert c.status == pm.FAIL


def test_a_ledger_promotion_the_map_does_not_carry_is_a_failure(fake_maps):
    fake_maps({}, {"embeddings": "mongo"})
    assert pm.check_flags("embeddings", {}).status == pm.FAIL


def test_an_ambient_backend_map_that_disagrees_is_reported(fake_maps):
    """The .env.deploy hazard: a shared, gitignored file exported a different
    flag state into the deploy shell. It must show up as a finding, not be
    silently used."""
    fake_maps({"embeddings": "mongo"}, {"embeddings": "mongo"})
    c = pm.check_flags("embeddings", {"ambient_backend_map": "embeddings:dual"})
    assert c.data["ambient_backend_map_differs"] is True
    assert any("ambient" in ln.lower() for ln in c.lines)


# ── check: guard (the verdict must come FROM the guard) ────────────────────

def test_the_guard_verdict_is_observed_not_assumed(monkeypatch):
    """Sabotage: blank the guard's table map so it stops firing.

    If this check re-derived the rule from the mode string it would still say
    "writes raise at mongo" and certify a protection that is not running. It
    must notice instead.
    """
    from app.db import pg_write_guard as guard
    monkeypatch.setattr(guard, "_guarded_cache", {})
    monkeypatch.setattr(guard, "reset_cache", lambda: None)
    c = pm.check_guard("embeddings", "mongo")
    assert c.status == pm.FAIL


def test_the_guard_verdict_passes_when_the_guard_really_fires(monkeypatch):
    from app.db import pg_write_guard as guard
    monkeypatch.setattr(guard, "_guarded_cache", {"embeddings": "mongo"})
    monkeypatch.setattr(guard, "reset_cache", lambda: None)
    monkeypatch.delenv("MONGO_GUARD_BLOCK_READS", raising=False)
    monkeypatch.delenv("MONGO_GUARD_ALLOW_PG", raising=False)
    c = pm.check_guard("embeddings", "mongo")
    assert c.status == pm.PASS
    assert c.data["write_insert"]["raised"]
    # The read guard is off here, but the counterfactual must still be measured.
    assert c.data["read_guard_armed_here"] is False
    assert c.data["read_ambient"]["raised"] is None
    assert c.data["read_forced_armed"]["raised"]


def test_the_probe_does_not_leave_the_read_guard_armed(monkeypatch):
    """Forcing MONGO_GUARD_BLOCK_READS on for one probe must not arm it for the
    rest of the process -- this tool runs inside other people's shells."""
    from app.db import pg_write_guard as guard
    monkeypatch.setattr(guard, "_guarded_cache", {"embeddings": "mongo"})
    monkeypatch.setattr(guard, "reset_cache", lambda: None)
    monkeypatch.delenv("MONGO_GUARD_BLOCK_READS", raising=False)
    pm.check_guard("embeddings", "mongo")
    assert guard._read_guard_enabled() is False


def test_a_raising_write_at_mongo_read_is_a_failure(monkeypatch):
    """At mongo_read Postgres is still dual-written; a guard that blocked it
    would break the live writer."""
    from app.db import pg_write_guard as guard
    monkeypatch.setattr(guard, "_guarded_cache", {"trade_results": "mongo"})
    monkeypatch.setattr(guard, "reset_cache", lambda: None)
    c = pm.check_guard("trade_results", "mongo_read")
    assert c.status == pm.FAIL


# ── check: parity (exhaustive only, and a timeout is not a pass) ───────────

def _fake_run(stdout: str, rc: int = 0):
    def _run(*a, **k):
        return subprocess.CompletedProcess(a[0] if a else [], rc, stdout, "")
    return _run


VERIFY_OK = "[t] VERIFY-ALL: pg_rows=100 mongo_docs=100 missing-in-mongo=0 drifted-fields=0\n[t] VERIFY-ALL: OK"
VERIFY_BAD = "[t] VERIFY-ALL: pg_rows=100 mongo_docs=98 missing-in-mongo=2 drifted-fields=117\n[t] VERIFY-ALL: MISMATCH"


def test_parity_reads_the_exhaustive_numbers(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(VERIFY_OK))
    c = pm.check_parity("t", "dual", 60, skip=False)
    assert c.status == pm.PASS
    assert c.data["mode_run"] == "exhaustive (--verify-all)"
    assert c.data["missing_in_mongo"] == 0 and c.data["drifted_fields"] == 0


def test_parity_fails_on_drift(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(VERIFY_BAD, rc=1))
    c = pm.check_parity("t", "dual", 60, skip=False)
    assert c.status == pm.FAIL
    assert c.data["drifted_fields"] == 117


def test_a_parity_failure_names_the_join_key_it_used(monkeypatch):
    """Both of `embeddings`' defect classes turned out to be artefacts of HOW the
    comparison was made: 701 rows "missing" that are present under a different
    `id` (the store re-keys on re-embed), and 27,956 "drifted" vectors that are
    byte-identical once the BSON Binary is unpacked. A FAIL must hand the reader
    the join key rather than just a number."""
    monkeypatch.setattr(subprocess, "run", _fake_run(VERIFY_BAD, rc=1))
    c = pm.check_parity("embeddings", "mongo", 60, skip=False)
    assert c.status == pm.FAIL
    text = "\n".join(c.lines)
    assert "join key" in text and "BSON Binary" in text


def test_the_parity_command_is_the_exhaustive_one(monkeypatch):
    """It must never shell out to --verify-fields: that verdict depends on the
    draw, and a sampled result is not a parity statement."""
    seen = {}

    def _run(cmd, *a, **k):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, VERIFY_OK, "")

    monkeypatch.setattr(subprocess, "run", _run)
    pm.check_parity("t", "dual", 60, skip=False)
    assert "--verify-all" in seen["cmd"]
    assert "--verify-fields" not in seen["cmd"]


def test_a_timeout_is_insufficient_not_a_partial_pass(monkeypatch):
    def _run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(subprocess, "run", _run)
    c = pm.check_parity("t", "dual", 1, skip=False)
    assert c.status == pm.INSUFFICIENT
    assert c.data["timed_out"] is True


def test_unparsable_output_is_insufficient(monkeypatch):
    """No VERIFY-ALL line means no verdict -- not a quiet pass on rc=0."""
    monkeypatch.setattr(subprocess, "run", _fake_run("cannot build a spec for 't'", rc=2))
    assert pm.check_parity("t", "dual", 60, skip=False).status == pm.INSUFFICIENT


def test_skipping_parity_is_insufficient():
    c = pm.check_parity("t", "dual", 60, skip=True)
    assert c.status == pm.INSUFFICIENT
    assert c.data["mode_run"] == "skipped"


def test_surplus_mongo_docs_are_expected_at_mongo_but_not_at_dual(monkeypatch):
    """Once a table is at `mongo`, Postgres is frozen and Mongo keeps growing, so
    a surplus is the cutover working. At `dual` both stores are written, so the
    same surplus is an orphan population."""
    surplus = ("[t] VERIFY-ALL: pg_rows=100 mongo_docs=140 "
               "missing-in-mongo=0 drifted-fields=0\n[t] VERIFY-ALL: MISMATCH")
    monkeypatch.setattr(subprocess, "run", _fake_run(surplus, rc=1))
    assert pm.check_parity("t", "mongo", 60, skip=False).status == pm.PASS
    assert pm.check_parity("t", "dual", 60, skip=False).status == pm.FAIL


# ── check: logs (a zero needs its positive control) ────────────────────────

LIVE_WARN = "\n".join(
    [f"2026-08-17 10:00:0{i} [app.x] INFO something happened" for i in range(9)]
    + ["2026-08-17 10:00:09 [app.y] WARNING a normal warning"])
MIRROR_LINE = ("2026-08-17 10:00:10 [app.db.vector_store] WARNING "
               "[vector_store] mongo mirror failed (non-fatal): boom")


def test_a_zero_from_a_dead_feed_is_labelled_dead(tmp_path):
    """The client log carried 1 WARN/ERROR in 63,178 lines; its zero mirror hits
    were worthless and read as a clean. An empty window must say so."""
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    c = pm.check_logs("trade_results", None, str(empty), "48h")
    assert c.status == pm.INSUFFICIENT
    assert "DEAD FEED" in c.headline or any("DEAD FEED" in ln for ln in c.lines)


def test_a_zero_is_never_printed_without_the_volume_that_backs_it(tmp_path):
    log = tmp_path / "live.log"
    log.write_text(LIVE_WARN, encoding="utf-8")
    c = pm.check_logs("trade_results", None, str(log), "48h")
    assert c.status == pm.PASS
    assert c.data["mirror_failure_lines"] == 0
    assert c.data["warn_error_lines"] > 0
    # the count and its control travel together, always
    assert "WARN/ERROR" in "\n".join(c.lines)
    assert str(c.data["warn_error_lines"]) in c.headline


def test_the_mirror_detector_actually_fires(tmp_path):
    """Positive control for THIS tool's own detector. Without it, a regex that
    matched nothing would make every soak look clean forever."""
    log = tmp_path / "bad.log"
    log.write_text(LIVE_WARN + "\n" + MIRROR_LINE, encoding="utf-8")
    c = pm.check_logs("embeddings", None, str(log), "48h")
    assert c.status == pm.FAIL
    assert c.data["mirror_failure_lines"] == 1


@pytest.mark.parametrize("line", [
    "2026-08-17 [x] WARNING [AgentAudit] Mongo mirror failed (non-fatal): e",
    "2026-08-17 [x] WARNING app.db.mongo_store: mongo read failed, PG fallback: e",
    "2026-08-17 [x] ERROR [PG GUARD] DELETE on 'embeddings' hit Postgres only",
    "2026-08-17 [x] ERROR [PipelineStateDB] Mongo dual-write failed (non-fatal): e",
])
def test_every_real_evidence_line_shape_is_matched(line):
    """The strings the promotion soaks have always grepped for."""
    assert pm._MIRROR_RE.search(line), line


def test_an_unreadable_log_source_is_insufficient(tmp_path):
    c = pm.check_logs("t", None, str(tmp_path / "nope.log"), "48h")
    assert c.status == pm.INSUFFICIENT


# ── check: logs, the static half (can the site print at all?) ──────────────

def test_a_debug_mirror_site_is_reported_as_unprintable(tmp_path):
    """The original defect: six sites at DEBUG under an INFO root logger, so a
    48-hour grep returned a zero that could not have been anything else."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "planted.py").write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(e):\n"
        "    logger.debug('[X] Mongo mirror failed (non-fatal): %s', e)\n"
        "    logger.warning('[Y] Mongo mirror failed (non-fatal): %s', e)\n",
        encoding="utf-8")
    sites = pm.mirror_log_sites(app)
    assert len(sites) == 2
    by_level = {s["level"]: s["emittable"] for s in sites}
    assert by_level == {"debug": False, "warning": True}


def test_a_zero_with_an_invisible_site_is_not_a_clean(tmp_path, monkeypatch):
    """Even a demonstrably live stream cannot clear a table whose mirror-failure
    site could not have printed into it."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "planted.py").write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(e):\n"
        "    logger.debug('[X] Mongo mirror failed: %s', e)\n", encoding="utf-8")
    monkeypatch.setattr(pm, "REPO_ROOT", tmp_path)
    log = tmp_path / "live.log"
    log.write_text(LIVE_WARN, encoding="utf-8")
    c = pm.check_logs("t", None, str(log), "48h")
    assert c.status == pm.INSUFFICIENT
    assert "not falsifiable" in c.headline


def test_the_live_tree_has_no_unprintable_evidence_site():
    """Regression against the real app/: cd44c0b promoted all six."""
    invisible = [s for s in pm.mirror_log_sites(pm.REPO_ROOT / "app")
                 if not s["emittable"]]
    assert not invisible, invisible


# ── check: provenance (the oracle must admit when it is blind) ─────────────

class _FakePG:
    def __init__(self, values, ts_cols=("created_at",)):
        self._values = values
        self._ts = list(ts_cols)

    def table_exists(self, table):
        return True

    def timestamp_columns(self, table):
        return self._ts

    def all(self, sql, params=None):
        return [(v,) for v in self._values]


def _mongo_values(monkeypatch, values):
    from app.db import mongo_store
    monkeypatch.setattr(mongo_store, "aggregate",
                        lambda coll, pipeline: [{"created_at": v} for v in values])


def test_the_oracle_is_usable_when_the_two_stores_look_different(monkeypatch):
    _mongo_values(monkeypatch, [datetime(2026, 8, 17, 1, 2, 3, 456000)] * 50)
    pg = _FakePG([datetime(2026, 8, 17, 1, 2, 3, 456789)] * 50)
    c = pm.check_provenance("t", pg, None, 50)
    assert c.status == pm.PASS
    assert c.data["oracle_usable"] is True


def test_an_oracle_whose_two_classes_look_identical_is_blind(monkeypatch):
    """If the Postgres column is itself millisecond-aligned -- a writer with a
    millisecond clock -- then alignment names no store, and the 1-in-1000
    false-positive rate is not 1-in-1000 but 1-in-1."""
    aligned = datetime(2026, 8, 17, 1, 2, 3, 456000)
    _mongo_values(monkeypatch, [aligned] * 50)
    pg = _FakePG([aligned] * 50)
    c = pm.check_provenance("t", pg, None, 50)
    assert c.status == pm.INSUFFICIENT
    assert "BLIND" in c.headline
    assert c.data["pg_alignment_rate"] == 1.0


def test_the_output_states_the_20_value_requirement(monkeypatch):
    _mongo_values(monkeypatch, [datetime(2026, 8, 17, 1, 2, 3, 456000)] * 50)
    pg = _FakePG([datetime(2026, 8, 17, 1, 2, 3, 456789)] * 50)
    text = "\n".join(pm.check_provenance("t", pg, None, 50).lines)
    assert "~20 aligned values" in text
    assert "silent on documents carrying no timestamp" in text


def test_documents_without_the_field_are_counted_and_disclaimed(monkeypatch):
    from app.db import mongo_store
    monkeypatch.setattr(mongo_store, "aggregate", lambda coll, pipeline: (
        [{"created_at": datetime(2026, 8, 17, 1, 2, 3, 456000)}] * 30 + [{}] * 20))
    pg = _FakePG([datetime(2026, 8, 17, 1, 2, 3, 456789)] * 50)
    c = pm.check_provenance("t", pg, None, 50)
    assert c.data["mongo_without_field"] == 20
    assert any("NO 'created_at'" in ln for ln in c.lines)


def test_a_table_with_no_timestamp_column_is_insufficient_not_pass():
    c = pm.check_provenance("t", _FakePG([], ts_cols=()), None, 50)
    assert c.status == pm.INSUFFICIENT


def test_an_unreachable_postgres_cannot_yield_an_oracle():
    """Without the Postgres side there is no measured false-positive rate, and
    an assumed one is how a blind discriminator gets believed."""
    c = pm.check_provenance("t", None, "connection refused", 50)
    assert c.status == pm.INSUFFICIENT


# ── counts ─────────────────────────────────────────────────────────────────

def test_mongo_short_of_postgres_is_always_a_failure(monkeypatch):
    from app.db import mongo_store
    monkeypatch.setattr(mongo_store, "count_docs", lambda t: 90)

    class _PG(_FakePG):
        def one(self, sql, params=None):
            return (100,)

    c = pm.check_counts("t", "dual", _PG([]), None)
    assert c.status == pm.FAIL
    assert c.data["delta"] == -10


def test_an_unreachable_mongo_is_insufficient(monkeypatch):
    from app.db import mongo_store

    def _boom(t):
        raise RuntimeError("no server")

    monkeypatch.setattr(mongo_store, "count_docs", _boom)

    class _PG(_FakePG):
        def one(self, sql, params=None):
            return (100,)

    assert pm.check_counts("t", "dual", _PG([]), None).status == pm.INSUFFICIENT


def test_the_collection_is_resolved_through_collection_for(monkeypatch):
    """Never a name typed by hand: a name that bypasses the resolver silently
    starts a second, invisible collection."""
    from app.db import mongo_store
    monkeypatch.setattr(mongo_store, "count_docs", lambda t: 5)

    class _PG(_FakePG):
        def one(self, sql, params=None):
            return (5,)

    c = pm.check_counts("embeddings", "mongo", _PG([]), None)
    from app.db.collections import collection_for
    assert c.data["collection_for"] == collection_for("embeddings")
    assert c.data["target_collection"] == "state_embeddings"
