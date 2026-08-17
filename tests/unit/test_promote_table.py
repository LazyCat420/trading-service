"""scripts/promote_table.py must refuse, and each refusal must be observed firing.

A promotion tool is judged by what it declines. Every gate here exists because
the migration has already been burned by its absence — a sampled parity check
read as a pass, a flag map that drifted from its twin, a table frozen at `mongo`
with Postgres still being written — so every gate below is exercised in BOTH
states: the sabotage that must be refused, and the nearby legal case that must
still go through. A gate that only ever refuses is indistinguishable from a
broken tool, and a gate that only ever passes is not a gate.

Everything runs against TEMP COPIES of app/db/mongo_backends.env and
app/db/migration_ledger.json. `real_files_untouched` is the autouse tripwire for
that: it hashes the committed files before and after every single test, so a
test that reaches the live map fails the moment it does, rather than being
discovered by a deploy.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "promote_table.py"
_REAL_ENV = _ROOT / "app" / "db" / "mongo_backends.env"
_REAL_LEDGER = _ROOT / "app" / "db" / "migration_ledger.json"

spec = importlib.util.spec_from_file_location("promote_table", _SCRIPT)
pt = importlib.util.module_from_spec(spec)
sys.modules["promote_table"] = pt
spec.loader.exec_module(pt)


# ── the tripwire ───────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def real_files_untouched():
    """No test may write the committed map or ledger. Checked, not trusted."""
    def digest():
        return {p: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (_REAL_ENV, _REAL_LEDGER)}
    before = digest()
    yield
    assert digest() == before, "a test wrote to the REAL flag map or ledger"


# ── the sandbox ────────────────────────────────────────────────────────────
@pytest.fixture
def tree(tmp_path):
    """A trading-service + trading-client pair, copied from the real files.

    Copied rather than synthesised: a fixture that invents its own map cannot
    catch the tool disagreeing with the committed one about what a table's
    current mode is.
    """
    svc = tmp_path / "trading-service"
    cli = tmp_path / "trading-client"
    for root in (svc, cli):
        (root / "app" / "db").mkdir(parents=True)
        (root / "app" / "db" / "mongo_backends.env").write_bytes(
            _REAL_ENV.read_bytes())
    (svc / "app" / "db" / "migration_ledger.json").write_bytes(
        _REAL_LEDGER.read_bytes())
    return svc, cli


@pytest.fixture
def run(tree, tmp_path):
    """Invoke main() against the sandbox; returns (rc, cutover record)."""
    svc, cli = tree
    counter = {"n": 0}

    def _run(*argv, client=True, json_out=True):
        counter["n"] += 1
        full = list(argv) + ["--repo-root", str(svc)]
        if client:
            full += ["--client", str(cli)]
        else:
            # A path that certainly has no checkout under it.
            full += ["--client", str(tmp_path / "no-such-client")]
        record_path = tmp_path / f"record_{counter['n']}.json"
        if json_out:
            full += ["--json", str(record_path)]
        rc = pt.main(full)
        record = (json.loads(record_path.read_text())
                  if json_out and record_path.exists() else None)
        return rc, record

    return _run


def modes_of(root: pathlib.Path) -> dict[str, str]:
    cbm = pt.load_backend_checker(root)
    return cbm.parse_map(root / "app" / "db" / "mongo_backends.env")


def ledger_of(root: pathlib.Path, table: str) -> dict:
    data = json.loads((root / "app" / "db" / "migration_ledger.json").read_text())
    return pt.ledger_row(data, table)


def verdicts(record: dict) -> dict[str, str]:
    return {c["name"]: c["status"] for c in record["checks"]}


def refusals(record: dict) -> str:
    return " ".join(c["detail"] for c in record["checks"]
                    if c["status"] == pt.REFUSE)


def make_bundle(path: pathlib.Path, table: str, *,
                mode="dual", mode_run="exhaustive (--verify-all)",
                parity_status="PASS", missing=0, drifted=0,
                age_hours=1.0, extra_checks=()):
    """A prove_mongo.py signoff bundle, in its emitted shape."""
    stamp = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    data = {"mode_run": mode_run, "missing_in_mongo": missing,
            "drifted_fields": drifted}
    checks = [{"name": "parity", "status": parity_status,
               "headline": "exhaustive", "lines": [], "data": data}]
    checks += list(extra_checks)
    path.write_text(json.dumps({
        "tool": "scripts/prove_mongo.py",
        "table": table, "mode": mode, "verdict": parity_status,
        "generated_at": stamp.isoformat(), "checks": checks,
    }, indent=2))
    return str(path)


# ── the happy paths (so a tool that always refused would fail here) ────────

def test_a_one_step_promotion_dry_runs_clean(run, tree):
    """cycle_audit_log is at `dual`; one step forward is `mongo_read`."""
    svc, cli = tree
    before = (svc / "app" / "db" / "mongo_backends.env").read_bytes()
    rc, record = run("--table", "cycle_audit_log", "--to", "mongo_read")
    assert rc == pt.EXIT_OK, record
    assert record["status"] == "WOULD APPLY"
    assert record["dry_run"] is True and record["applied"] is False
    # A dry run VERIFIES against a mirror -- and touches nothing.
    assert verdicts(record)["post_edit_invariant"] == pt.PASS
    assert (svc / "app" / "db" / "mongo_backends.env").read_bytes() == before


def test_apply_writes_both_repos_identically_and_bumps_the_ledger(run, tree):
    svc, cli = tree
    rc, record = run("--table", "cycle_audit_log", "--to", "mongo_read", "--apply")
    assert rc == pt.EXIT_OK, record
    assert record["applied"] is True and record["status"] == "APPLIED"
    assert modes_of(svc)["cycle_audit_log"] == "mongo_read"
    # The two containers stage their own copies; byte-identical is the invariant.
    assert ((svc / "app" / "db" / "mongo_backends.env").read_bytes()
            == (cli / "app" / "db" / "mongo_backends.env").read_bytes())
    row = ledger_of(svc, "cycle_audit_log")
    assert row["mode_now"] == "mongo_read"
    assert row["promoted_mongo_read"] is not None


def test_the_edit_only_touches_the_flag_line(run, tree):
    """Every comment above the map is documentation people read before a flip."""
    svc, _ = tree
    before = (svc / "app" / "db" / "mongo_backends.env").read_text().splitlines()
    run("--table", "cycle_audit_log", "--to", "mongo_read", "--apply")
    after = (svc / "app" / "db" / "mongo_backends.env").read_text().splitlines()
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(differing) == 1
    assert after[differing[0]].startswith("MONGO_STORE_BACKEND=")


# ── the ladder ─────────────────────────────────────────────────────────────

def test_skipping_a_step_is_refused(run):
    """agent_audit_log is at `dual`; `mongo` skips the mongo_read soak."""
    rc, record = run("--table", "agent_audit_log", "--to", "mongo")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["transition"] == pt.REFUSE
    assert "skips mongo_read" in refusals(record)


def test_force_allows_the_skip_but_says_so(run):
    rc, record = run("--table", "agent_audit_log", "--to", "mongo", "--force",
                     "--archive", str(_REAL_ENV))
    assert rc == pt.EXIT_OK
    assert verdicts(record)["transition"] == pt.WARN
    assert "FORCED" in " ".join(c["detail"] for c in record["checks"])


def test_promoting_to_the_mode_it_is_already_at_is_refused(run):
    rc, record = run("--table", "agent_audit_log", "--to", "dual")
    assert rc == pt.EXIT_REFUSED
    assert "already at" in refusals(record)


def test_going_backwards_without_rollback_is_refused(run):
    rc, record = run("--table", "agent_audit_log", "--to", "pg")
    assert rc == pt.EXIT_REFUSED
    assert "use --rollback" in refusals(record)


# ── the ledger's disposition ───────────────────────────────────────────────

def test_a_table_absent_from_the_ledger_is_refused(run):
    rc, record = run("--table", "not_a_real_table", "--to", "dual")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["ledger_row"] == pt.REFUSE


def test_an_absent_disposition_is_refused(run, tree):
    """rejected_symbols is named in the code and does not exist in the DB."""
    svc, _ = tree
    assert ledger_of(svc, "rejected_symbols")["disposition"] == "absent"
    rc, record = run("--table", "rejected_symbols", "--to", "dual")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["disposition"] == pt.REFUSE
    assert "does not exist in the database" in refusals(record)


def test_an_archive_only_disposition_is_refused(run, tree):
    svc, _ = tree
    table = next(r["table"] for r in
                 json.loads((svc / "app" / "db" / "migration_ledger.json").read_text())["tables"]
                 if r["disposition"] == "archive-only")
    rc, record = run("--table", table, "--to", "dual")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["disposition"] == pt.REFUSE
    assert "dump-and-drop" in refusals(record)


def test_a_migrate_disposition_passes_that_gate(run):
    """The positive control: the same gate must not refuse everything."""
    rc, record = run("--table", "watchlist", "--to", "dual")
    assert verdicts(record)["disposition"] == pt.PASS


# ── money ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tier", ["one-shot", "full", "drain"])
def test_a_money_table_refuses_every_tier_but_replay(run, tier):
    rc, record = run("--table", "positions", "--to", "dual", "--tier", tier)
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["tier_fits_shape"] == pt.REFUSE
    assert "--tier replay" in refusals(record)


def test_the_replay_tier_accepts_the_money_table(run):
    rc, record = run("--table", "positions", "--to", "dual", "--tier", "replay",
                     "--signoff", "lazycat")
    assert rc == pt.EXIT_OK, record
    assert verdicts(record)["tier_fits_shape"] == pt.PASS


def test_replay_refuses_without_a_signoff(run):
    rc, record = run("--table", "positions", "--to", "dual", "--tier", "replay")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["signoff"] == pt.REFUSE


@pytest.mark.parametrize("value", ["yes", "ok", "automated", "ci"])
def test_a_confirmation_is_not_a_signoff(run, value):
    rc, record = run("--table", "positions", "--to", "dual", "--tier", "replay",
                     "--signoff", value)
    assert rc == pt.EXIT_REFUSED
    assert "not a name" in refusals(record)


@pytest.mark.parametrize("marker", ["CI", "IS_TOOL_PROCESS"])
def test_replay_refuses_to_run_unattended(run, monkeypatch, marker):
    monkeypatch.setenv(marker, "true")
    rc, record = run("--table", "positions", "--to", "dual", "--tier", "replay",
                     "--signoff", "lazycat")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["unattended"] == pt.REFUSE


def test_the_unattended_marker_does_not_block_other_tiers(run, monkeypatch):
    """Negative control: only `replay` demands a human."""
    monkeypatch.setenv("CI", "true")
    rc, record = run("--table", "watchlist", "--to", "dual")
    assert rc == pt.EXIT_OK
    assert "unattended" not in verdicts(record)


def test_a_money_table_at_mongo_read_still_needs_replay(run, tree):
    """trade_results is dec128 AND already at mongo_read -- a live case."""
    svc, _ = tree
    assert modes_of(svc)["trade_results"] == "mongo_read"
    rc, record = run("--table", "trade_results", "--to", "mongo")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["tier_fits_shape"] == pt.REFUSE


# ── queues ─────────────────────────────────────────────────────────────────

def test_a_queue_refuses_a_non_drain_tier(run):
    rc, record = run("--table", "scraper_queue", "--to", "dual")
    assert rc == pt.EXIT_REFUSED
    assert "--tier drain" in refusals(record)


@pytest.mark.parametrize("mode", ["dual", "mongo_read"])
def test_a_queue_is_never_dual_written(run, mode):
    """Both intermediate modes write BOTH stores -- that is the whole hazard."""
    rc, record = run("--table", "scraper_queue", "--to", mode,
                     "--tier", "drain", "--drained")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["drain_state"] == pt.REFUSE
    assert "must never be dual-written" in refusals(record)


def test_the_drain_flip_needs_the_queue_observed_empty(run):
    rc, record = run("--table", "scraper_queue", "--to", "mongo", "--tier", "drain")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["drain_state"] == pt.REFUSE
    assert "--drained" in refusals(record)


def test_a_drained_queue_flips_straight_to_mongo(run):
    """pg → mongo in one move is the ceremony for this tier, not a skipped step."""
    rc, record = run("--table", "scraper_queue", "--to", "mongo",
                     "--tier", "drain", "--drained")
    assert rc == pt.EXIT_OK, record
    assert verdicts(record)["transition"] == pt.PASS


# ── the exhaustive-parity gate ─────────────────────────────────────────────

def test_full_tier_refuses_without_evidence(run):
    rc, record = run("--table", "context_blobs", "--to", "mongo_read",
                     "--tier", "full")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["evidence"] == pt.REFUSE
    assert "prove_mongo.py" in refusals(record)


def test_full_tier_accepts_an_exhaustive_pass(run, tmp_path):
    bundle = make_bundle(tmp_path / "b.json", "context_blobs", mode="dual")
    rc, record = run("--table", "context_blobs", "--to", "mongo_read",
                     "--tier", "full", "--evidence", bundle)
    assert rc == pt.EXIT_OK, record
    assert verdicts(record)["evidence"] == pt.PASS


def test_a_sampled_verdict_never_satisfies_the_gate(run, tmp_path):
    """The defect this gate exists for: --verify-fields scored context_blobs OK
    three runs out of four while a full sweep found 117 drifted timestamps."""
    bundle = make_bundle(tmp_path / "b.json", "context_blobs",
                         mode_run="sampled (--verify-fields 500)")
    rc, record = run("--table", "context_blobs", "--to", "mongo_read",
                     "--tier", "full", "--evidence", bundle)
    assert rc == pt.EXIT_REFUSED
    assert "not the exhaustive sweep" in refusals(record)


def test_an_unlabelled_verdict_is_refused(run, tmp_path):
    """No mode_run means a sampled number cannot be told from an exhaustive one."""
    bundle = make_bundle(tmp_path / "b.json", "context_blobs", mode_run="")
    rc, record = run("--table", "context_blobs", "--to", "mongo_read",
                     "--tier", "full", "--evidence", bundle)
    assert rc == pt.EXIT_REFUSED
    assert "does not record which mode produced it" in refusals(record)


def test_a_skipped_parity_check_is_refused(run, tmp_path):
    bundle = make_bundle(tmp_path / "b.json", "context_blobs",
                         mode_run="skipped", parity_status="INSUFFICIENT")
    rc, record = run("--table", "context_blobs", "--to", "mongo_read",
                     "--tier", "full", "--evidence", bundle)
    assert rc == pt.EXIT_REFUSED


def test_an_exhaustive_sweep_that_found_defects_is_refused(run, tmp_path):
    bundle = make_bundle(tmp_path / "b.json", "context_blobs",
                         missing=2, drifted=117)
    rc, record = run("--table", "context_blobs", "--to", "mongo_read",
                     "--tier", "full", "--evidence", bundle)
    assert rc == pt.EXIT_REFUSED
    assert "reported defects" in refusals(record)


def test_evidence_about_another_table_is_refused(run, tmp_path):
    bundle = make_bundle(tmp_path / "b.json", "trade_results")
    rc, record = run("--table", "context_blobs", "--to", "mongo_read",
                     "--tier", "full", "--evidence", bundle)
    assert rc == pt.EXIT_REFUSED
    assert "no entry for" in refusals(record)


def test_a_stale_exhaustive_verdict_is_refused(run, tmp_path):
    """Parity describes the data at sweep time; the table has taken writes since."""
    bundle = make_bundle(tmp_path / "b.json", "context_blobs", age_hours=400)
    rc, record = run("--table", "context_blobs", "--to", "mongo_read",
                     "--tier", "full", "--evidence", bundle)
    assert rc == pt.EXIT_REFUSED
    assert "old (limit" in refusals(record)


def test_a_missing_bundle_file_is_refused_not_ignored(run, tmp_path):
    rc, record = run("--table", "context_blobs", "--to", "mongo_read",
                     "--tier", "full", "--evidence", str(tmp_path / "nope.json"))
    assert rc == pt.EXIT_REFUSED
    assert "does not exist" in refusals(record)


def test_a_failing_check_elsewhere_in_the_bundle_is_refused(run, tmp_path):
    bundle = make_bundle(tmp_path / "b.json", "context_blobs", extra_checks=[
        {"name": "guard", "status": "FAIL", "headline": "writes still land in PG",
         "lines": [], "data": {}}])
    rc, record = run("--table", "context_blobs", "--to", "mongo_read",
                     "--tier", "full", "--evidence", bundle)
    assert rc == pt.EXIT_REFUSED
    assert "FAILED in the same" in refusals(record)


def test_the_parity_gate_does_not_apply_to_the_first_step(run):
    """pg → dual makes Mongo a mirror, not an authority."""
    rc, record = run("--table", "watchlist", "--to", "dual", "--tier", "full")
    assert rc == pt.EXIT_OK, record
    assert verdicts(record)["evidence"] == pt.SKIP


# ── one-shot's archive ─────────────────────────────────────────────────────

def test_one_shot_refuses_the_final_flip_with_no_archive(run):
    rc, record = run("--table", "trade_results", "--to", "mongo",
                     "--tier", "one-shot", "--force")
    assert rc == pt.EXIT_REFUSED
    assert "archive-and-cutover" in refusals(record)


def test_one_shot_accepts_an_archive_that_exists(run, tmp_path):
    dump = tmp_path / "llm_audit_logs.jsonl"
    dump.write_text('{"id": 1}\n')
    rc, record = run("--table", "llm_audit_logs", "--to", "mongo",
                     "--tier", "one-shot", "--archive", str(dump))
    assert rc == pt.EXIT_OK, record
    assert verdicts(record)["archive"] == pt.PASS


def test_an_empty_archive_file_is_refused(run, tmp_path):
    """An existence check is satisfied by a file that preserves nothing."""
    dump = tmp_path / "empty.jsonl"
    dump.write_text("")
    rc, record = run("--table", "llm_audit_logs", "--to", "mongo",
                     "--tier", "one-shot", "--archive", str(dump))
    assert rc == pt.EXIT_REFUSED
    assert "0 bytes" in refusals(record)


def test_a_named_archive_that_does_not_exist_is_refused(run, tmp_path):
    rc, record = run("--table", "llm_audit_logs", "--to", "mongo",
                     "--tier", "one-shot", "--archive", str(tmp_path / "gone.jsonl"))
    assert rc == pt.EXIT_REFUSED
    assert "is not a dump on disk" in refusals(record)


# ── rollback ───────────────────────────────────────────────────────────────

def test_rolling_out_of_mongo_is_refused_without_the_acknowledgement(run, tree):
    """embeddings is the live table at `mongo`."""
    svc, _ = tree
    assert modes_of(svc)["embeddings"] == "mongo"
    rc, record = run("--table", "embeddings", "--rollback")
    assert rc == pt.EXIT_REFUSED
    reason = refusals(record)
    assert "Postgres is FROZEN" in reason
    assert "stale rows that look current" in reason


def test_the_acknowledgement_lets_it_through_but_warns(run, tree):
    svc, _ = tree
    rc, record = run("--table", "embeddings", "--rollback",
                     "--accept-stale-reads", "--apply")
    assert rc == pt.EXIT_OK, record
    assert verdicts(record)["transition"] == pt.WARN
    assert modes_of(svc)["embeddings"] == "mongo_read"
    assert ledger_of(svc, "embeddings")["promoted_mongo"] is None


def test_rolling_back_to_pg_removes_the_table_from_the_map(run, tree):
    """Unlisted means pg; a leftover `:pg` entry reads as still in flight."""
    svc, cli = tree
    rc, record = run("--table", "agent_traces", "--rollback", "--apply")
    assert rc == pt.EXIT_OK, record
    assert "agent_traces" not in modes_of(svc)
    assert ledger_of(svc, "agent_traces")["mode_now"] == "pg"
    assert ((svc / "app" / "db" / "mongo_backends.env").read_bytes()
            == (cli / "app" / "db" / "mongo_backends.env").read_bytes())


def test_rollback_from_pg_has_nowhere_to_go(run):
    rc, record = run("--table", "watchlist", "--rollback")
    assert rc == pt.EXIT_REFUSED
    assert "nothing to roll back to" in refusals(record)


def test_rollback_is_not_tier_gated(run):
    """A money table must be able to retreat without a money ceremony."""
    rc, record = run("--table", "trade_results", "--rollback")
    assert rc == pt.EXIT_OK, record
    assert verdicts(record)["tier_fits_shape"] == pt.SKIP
    assert verdicts(record)["tier_gate"] == pt.SKIP


def test_rollback_and_to_together_are_rejected(run):
    rc, _ = run("--table", "agent_traces", "--rollback", "--to", "pg",
                json_out=False)
    assert rc == pt.EXIT_REFUSED


# ── the two files must already agree ───────────────────────────────────────

def test_a_pre_existing_drift_stops_the_run_before_any_edit(run, tree):
    """Promoting on top of a drift bakes it in."""
    svc, _ = tree
    p = svc / "app" / "db" / "migration_ledger.json"
    data = json.loads(p.read_text())
    pt.ledger_row(data, "watchlist")["mode_now"] = "mongo"
    p.write_text(json.dumps(data, indent=2) + "\n")
    before = (svc / "app" / "db" / "mongo_backends.env").read_bytes()

    rc, record = run("--table", "cycle_audit_log", "--to", "mongo_read", "--apply")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["preflight_invariant"] == pt.REFUSE
    assert (svc / "app" / "db" / "mongo_backends.env").read_bytes() == before


def test_a_table_whose_own_two_records_disagree_is_refused(run, tree):
    svc, _ = tree
    p = svc / "app" / "db" / "migration_ledger.json"
    data = json.loads(p.read_text())
    pt.ledger_row(data, "context_blobs")["mode_now"] = "mongo_read"
    p.write_text(json.dumps(data, indent=2) + "\n")
    rc, record = run("--table", "context_blobs", "--to", "mongo_read")
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["map_ledger_agree"] == pt.REFUSE


# ── the edit reverts itself ────────────────────────────────────────────────

def test_an_edit_that_breaks_the_invariant_is_reverted(run, tree, monkeypatch):
    """Fault injection: half the edit lands, so the map and ledger disagree.

    This is the only path that proves the revert works. Without it the revert
    is code nobody has watched run, and the failure it guards -- a half-applied
    cutover left in the tree -- is exactly the state a deploy would ship.
    """
    svc, cli = tree
    before = {p: p.read_bytes() for p in (
        svc / "app" / "db" / "mongo_backends.env",
        svc / "app" / "db" / "migration_ledger.json",
        cli / "app" / "db" / "mongo_backends.env")}

    monkeypatch.setattr(pt, "write_ledger_mode",
                        lambda *a, **k: None)  # the ledger half never lands
    rc, record = run("--table", "cycle_audit_log", "--to", "mongo_read", "--apply")

    assert rc == pt.EXIT_REVERTED
    assert record["status"] == "REVERTED"
    assert record["applied"] is False
    for p, blob in before.items():
        assert p.read_bytes() == blob, f"{p} was left edited after a revert"


def test_a_dry_run_reports_the_same_breakage_without_touching_anything(
        run, tree, monkeypatch):
    svc, _ = tree
    before = (svc / "app" / "db" / "mongo_backends.env").read_bytes()
    monkeypatch.setattr(pt, "write_ledger_mode", lambda *a, **k: None)
    rc, record = run("--table", "cycle_audit_log", "--to", "mongo_read")
    assert rc == pt.EXIT_REVERTED
    assert record["status"] == "WOULD BE REVERTED"
    assert (svc / "app" / "db" / "mongo_backends.env").read_bytes() == before


# ── the client copy ────────────────────────────────────────────────────────

def test_a_missing_client_is_reported_loudly_in_a_dry_run(run, capsys):
    rc, record = run("--table", "cycle_audit_log", "--to", "mongo_read",
                     client=False)
    assert rc == pt.EXIT_OK
    assert verdicts(record)["client_copy"] == pt.WARN
    assert "ABSENT" in record["client_state"]
    assert "client_copy" in capsys.readouterr().out


def test_applying_without_the_client_needs_an_explicit_acknowledgement(run):
    rc, record = run("--table", "cycle_audit_log", "--to", "mongo_read",
                     "--apply", client=False)
    assert rc == pt.EXIT_REFUSED
    assert verdicts(record)["client_copy"] == pt.REFUSE
    assert "disagree about which store is authoritative" in refusals(record)


def test_the_acknowledgement_allows_a_service_only_cutover(run, tree):
    svc, _ = tree
    rc, record = run("--table", "cycle_audit_log", "--to", "mongo_read",
                     "--apply", "--allow-missing-client", client=False)
    assert rc == pt.EXIT_OK, record
    assert modes_of(svc)["cycle_audit_log"] == "mongo_read"
    assert record["client_root"] is None


# ── the artifact ───────────────────────────────────────────────────────────

def test_the_record_carries_every_check_and_its_verdict(run):
    rc, record = run("--table", "cycle_audit_log", "--to", "mongo_read")
    assert rc == pt.EXIT_OK
    for key in ("table", "from_mode", "to_mode", "tier", "timestamp", "checks",
                "git", "map_line_before", "map_line_after"):
        assert key in record, key
    assert record["from_mode"] == "dual" and record["to_mode"] == "mongo_read"
    assert {"ledger_row", "disposition", "transition", "tier_fits_shape",
            "preflight_invariant", "client_copy",
            "post_edit_invariant"} <= set(verdicts(record))
    assert "cycle_audit_log:mongo_read" in record["map_line_after"]


def test_a_refusal_is_recorded_too(run):
    """The refusal is the artifact that matters most; it must not vanish."""
    rc, record = run("--table", "positions", "--to", "dual")
    assert rc == pt.EXIT_REFUSED
    assert record["status"] == "REFUSED"
    assert record["applied"] is False
    assert any(c["status"] == pt.REFUSE for c in record["checks"])


# ── pure helpers ───────────────────────────────────────────────────────────

def test_next_modes_drops_a_table_returning_to_pg():
    modes = {"a": "dual", "b": "mongo"}
    assert pt.next_modes(modes, "a", "pg") == {"b": "mongo"}
    assert pt.next_modes(modes, "a", "mongo_read") == {"a": "mongo_read", "b": "mongo"}
    assert pt.next_modes(modes, "c", "dual")["c"] == "dual"


def test_write_map_round_trips_through_the_real_parser(tmp_path):
    p = tmp_path / "mongo_backends.env"
    p.write_bytes(_REAL_ENV.read_bytes())
    modes = pt.next_modes(
        pt.load_backend_checker(_ROOT).parse_map(p), "watchlist", "dual")
    pt.write_map(p, modes)
    assert pt.load_backend_checker(_ROOT).parse_map(p)["watchlist"] == "dual"


def test_the_real_repo_is_consistent_before_any_promotion():
    """The committed tree must satisfy the tool's own precondition."""
    rc, _ = pt.run_invariant(_ROOT, _ROOT.parent / "trading-client")
    assert rc == 0
