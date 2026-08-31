"""`inflight_peak.py --cycle` must resolve its window from the LIVE store.

The script asks two questions of two different stores. The prism request
ledger half was Mongo from the start. The other half — turning a cycle id into
a time window — was

    SELECT min(created_at), max(created_at)
    FROM v3_agent_telemetry WHERE cycle_id = %s

against Postgres, which froze at the 2026-08-19 cutover. That read did not
break loudly. It answered `(None, None)` for every cycle run after the
cutover, and the script turned that into `sys.exit("no telemetry rows for
<id>")` and exit 1 — an operator reading it would conclude the CYCLE produced
no telemetry, not that the STORE stopped taking writes. Measured 2026-08-30:
340 cycle ids in the archive, 430 in Mongo, 90 reachable only in Mongo, 0 the
other way, so `--cycle` refused every cycle of the last eleven days.

WHY EACH TEST HERE WOULD HAVE BEEN RED BEFORE THE PORT
------------------------------------------------------
  test_the_script_has_no_postgres_coupling
        `scan` found 2 sites: `import psycopg2` (line 50) and the
        `.execute(<sql>)` inside cycle_window (line 93).
  test_no_hardcoded_production_dsn
        line 56 carried a full production DSN — host 10.0.0.16, port 5433,
        database trading_bot, password included — as an os.getenv default,
        so it connected whenever DATABASE_URL was unset.
  test_cycle_window_reads_the_telemetry_collection
        the old cycle_window never touched `mongo_query`, so the stub below
        records zero calls; it opened a socket to the frozen archive instead.
  test_a_cycle_with_no_telemetry_still_exits_with_the_same_message
        the old function reached that branch only via Postgres, so with
        psycopg2 stubbed out it exited "psycopg2 is required for --cycle".
  test_the_ledger_read_stays_in_prisms_database
        the pre-port file had no `get_mongo_db` at all — it built its own
        `MongoClient(MONGO_URI)["prism"]` — so the binding this asserts did
        not exist to assert.
  test_a_string_typed_created_at_is_refused_with_a_reason
        Postgres' column type made a string `created_at` impossible, and the
        first port kept that assumption in a DOCSTRING. `(lo - pad)` then
        raised a bare `TypeError: unsupported operand type(s) for -: 'str'
        and 'datetime.timedelta'` instead of exiting with the reason.
  test_an_empty_result_exits_non_zero_and_names_who_does_have_rows
  test_an_unchecked_empty_is_not_reported_as_an_empty_window
        both the pre-port script and the first port printed ONE bare line
        ("no ledger rows for provider=vllm-2 in …") and returned 0.
  test_a_non_empty_result_still_exits_zero
        pins the other direction of that change.

THE TEST THAT WAS DECORATION, AND WHAT REPLACED IT
--------------------------------------------------
The first version of `test_the_ledger_read_stays_in_prisms_database` said in
its own docstring that it pinned the prism/trading-DB boundary, and it did
not: it monkeypatched `inflight_peak.get_mongo_db` with a stub, so the real
name was never resolved and only the collection string and the bound types
were checked. Proven by mutation on 2026-08-30 — changing the import to
`from app.db.mongo_store import get_doc_db as get_mongo_db` (the exact blur
the docstring names) left all 7 tests GREEN, while the mutant live-printed
"no ledger rows for provider=all in 2026-08-25T03:25:18..2026-08-25T05:23:11"
and exited 0 for a window that really holds 1,131 rows.

A stub for the resolver cannot see a resolver swap, because the stub IS the
swap. So the fake here is one layer lower: the CLIENT. `get_mongo_db()` and
`get_doc_db()` both run their real bodies and pick their own database name off
their own settings; the fake client records which name each asked for. The
mutant then picks `trading_bot` and the test goes red, and
`test_the_prism_boundary_check_is_not_vacuous` performs that mutation in
process and asserts it does.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import app.db.mongo as app_mongo  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import mongo_store  # noqa: E402
from scripts import inflight_peak  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

REL = "scripts/inflight_peak.py"


def test_the_script_has_no_postgres_coupling():
    result = scan(REPO, targets=(REL,))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, "; ".join(
        f"{f['kind']} at line {f['line']}" for f in result["findings"]
    )


def test_the_scan_would_have_caught_this_file_before(tmp_path):
    """NEGATIVE CONTROL. A scan that finds nothing because it looked at
    nothing passes the assertion above just as happily. This runs the same
    call against the shape this file used to have.

    The planted DSN is deliberately a placeholder rather than the real one:
    `scripts/pg_script_inventory.py` scans `tests/` too and keys on a literal
    `postgres…://` in CODE (docstrings and comments are stripped, string
    literals are not). Pasting the production DSN in here would file THIS file
    as a new unclassified Postgres reader and turn the inventory gate red —
    verified 2026-08-30, it did exactly that on the first draft."""
    bad = tmp_path / "inflight_peak.py"
    bad.write_text(
        "import psycopg2\n"
        "DSN = 'redacted-dsn'\n"
        "def cycle_window(cid):\n"
        "    with psycopg2.connect(DSN) as c, c.cursor() as cur:\n"
        "        cur.execute('SELECT min(created_at) FROM v3_agent_telemetry')\n",
        encoding="utf-8",
    )
    assert scan(tmp_path, targets=("inflight_peak.py",))["total"] > 0


def test_no_hardcoded_production_dsn():
    source = (REPO / REL).read_text(encoding="utf-8")
    for needle in ("5433", "trading_bot_pass", "10.0.0.16", "DATABASE_URL"):
        assert needle not in source, f"{REL} still carries {needle!r}"


class _Recorder:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, collection, query, aggs):
        self.calls.append((collection, query, aggs))
        return self.answer


def test_cycle_window_reads_the_telemetry_collection(monkeypatch):
    # The real min/max for cycle-v3-1788074145, a cycle that exists ONLY in
    # Mongo (Postgres answers (None, None) for it). Naive-UTC, as BSON hands
    # a Date back.
    stamp = datetime(2026, 8, 30, 7, 21, 55, 173000)
    rec = _Recorder((stamp, stamp))
    monkeypatch.setattr(inflight_peak.mongo_query, "agg_row", rec)

    frm, to = inflight_peak.cycle_window("cycle-v3-1788074145", pad_min=5)

    assert rec.calls, "cycle_window did not read Mongo at all"
    collection, query, aggs = rec.calls[0]
    assert collection == "v3_agent_telemetry"
    assert query == {"cycle_id": "cycle-v3-1788074145"}
    assert aggs == [("min", "created_at"), ("max", "created_at")]
    # The padded window, as the live script prints it.
    assert (frm, to) == ("2026-08-30T07:16:55", "2026-08-30T07:26:55")


def test_the_window_string_does_not_shift_with_tzinfo(monkeypatch):
    """The archive handed back aware-UTC and Mongo hands back naive-UTC. The
    window is a lexical bound against prism's `createdAt` STRING, so a shift
    of any size silently selects the wrong rows rather than erroring."""
    naive = datetime(2026, 8, 19, 22, 44, 43, 675000)
    aware = naive.replace(tzinfo=timezone.utc)

    monkeypatch.setattr(inflight_peak.mongo_query, "agg_row", _Recorder((naive, naive)))
    a = inflight_peak.cycle_window("c", pad_min=5)
    monkeypatch.setattr(inflight_peak.mongo_query, "agg_row", _Recorder((aware, aware)))
    b = inflight_peak.cycle_window("c", pad_min=5)

    assert a == b == ("2026-08-19T22:39:43", "2026-08-19T22:49:43")


def test_a_cycle_with_no_telemetry_still_exits_with_the_same_message(monkeypatch):
    monkeypatch.setattr(inflight_peak.mongo_query, "agg_row", _Recorder((None, None)))
    with pytest.raises(SystemExit) as exc:
        inflight_peak.cycle_window("cycle-v3-nope")
    assert exc.value.code == "no telemetry rows for cycle-v3-nope"


def test_a_string_typed_created_at_is_refused_with_a_reason(monkeypatch):
    """Postgres' column type is what used to guarantee `created_at` is a date.

    Nothing guarantees it now, and BSON orders String BELOW Date, so ONE
    string-typed row would win `$min` outright and start the window in the
    wrong place. It is currently clean — {'date': 8787} of 8787, typed and
    counted on 2026-08-30 — which is exactly why it needs a gate rather than a
    sentence: the day it stops being clean, nobody re-runs the docstring."""
    monkeypatch.setattr(
        inflight_peak.mongo_query, "agg_row",
        _Recorder(("2026-08-30T07:21:55", "2026-08-30T07:22:55")),
    )
    with pytest.raises(SystemExit) as exc:
        inflight_peak.cycle_window("cycle-v3-1788074145")
    message = str(exc.value.code)
    assert "not datetimes" in message, message
    assert "str/str" in message, message


# ── the prism/trading-DB boundary ──────────────────────────────────────────

class _FakeCol:
    def __init__(self, seen):
        self.seen = seen

    def find(self, query, projection):
        self.seen["query"] = query
        self.seen["projection"] = projection
        return iter(())


class _FakeDb:
    def __init__(self, name, seen):
        self.name = name
        self.seen = seen

    def __getitem__(self, collection):
        self.seen["collection"] = collection
        return _FakeCol(self.seen)


def _install_fake_client(monkeypatch) -> dict:
    """Fake the CLIENT, not the resolver.

    `get_mongo_db()` and `mongo_store.get_doc_db()` differ only in the
    database name they ask their client for, so faking either function throws
    away the one bit under test. Faking the client one layer down lets both
    run their real bodies — settings lookup included — and records the name.
    """
    seen: dict = {}

    class _FakeClient:
        def __getitem__(self, name):
            seen["db"] = name
            return _FakeDb(name, seen)

    monkeypatch.setattr(app_mongo, "get_mongo_client", lambda: _FakeClient())
    monkeypatch.setattr(mongo_store, "get_mongo_client", lambda: _FakeClient())
    return seen


def test_the_ledger_read_stays_in_prisms_database(monkeypatch):
    """The ledger is prism's, and prism's DB is not the trading DB.

    `mongo_query`/`mongo_store` are bound to TRADING_MONGO_DB. Routing the
    ledger through them compiles, returns [], renders "no ledger rows" and
    exits 0 — a wrong answer wearing the costume of a real one, which is the
    exact silent-empty this migration exists to catch.
    """
    assert inflight_peak.get_mongo_db is app_mongo.get_mongo_db, (
        "the ledger resolver must be app.db.mongo.get_mongo_db"
    )
    assert inflight_peak.get_mongo_db.__module__ == "app.db.mongo", (
        "the ledger resolver must come from app.db.mongo, not a trading-DB module"
    )
    # The two databases are different names, and neither is a guess.
    assert (settings.PRISM_MONGO_DB or "prism") == "prism"
    assert mongo_store.TRADING_MONGO_DB == "trading_bot"
    assert (settings.PRISM_MONGO_DB or "prism") != mongo_store.TRADING_MONGO_DB

    seen = _install_fake_client(monkeypatch)
    inflight_peak.fetch("2026-08-30T07:16:55", "2026-08-30T07:26:55", "vllm-2")

    assert seen["db"] == "prism", (
        f"the ledger read resolved to database {seen['db']!r}; app.db.mongo "
        "must pick prism's"
    )
    assert seen["collection"] == "requests"
    # String bounds, not datetimes: prism stores createdAt as a string and a
    # BSON Date compared against it matches nothing.
    assert seen["query"] == {
        "createdAt": {"$gte": "2026-08-30T07:16:55", "$lte": "2026-08-30T07:26:55"},
        "provider": "vllm-2",
    }
    assert all(isinstance(v, str) for v in seen["query"]["createdAt"].values())
    # `--provider all` means every provider, not a provider literally named
    # "all"; main() passes None and the filter must drop the key entirely.
    inflight_peak.fetch("a", "b", None)
    assert "provider" not in seen["query"]


def test_the_prism_boundary_check_is_not_vacuous(monkeypatch):
    """MUTATION CONTROL for the test above.

    Performs, in process, the blur that the previous version of that test was
    blind to — the ledger resolved through the trading DB — and asserts the
    test goes red. Without this, "it passes" says nothing about whether it
    could ever fail.
    """
    monkeypatch.setattr(inflight_peak, "get_mongo_db", mongo_store.get_doc_db)
    with pytest.raises(AssertionError, match="app.db.mongo"):
        test_the_ledger_read_stays_in_prisms_database(monkeypatch)

    # And the database-name half discriminates on its own, not merely the
    # identity check above: the mutant resolver really does pick the other DB.
    seen = _install_fake_client(monkeypatch)
    assert mongo_store.get_doc_db().name == "trading_bot"
    assert seen["db"] != "prism"


# ── an empty answer is a red result, not a zero measurement ────────────────

def _run_main(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["inflight_peak.py"] + argv)
    return inflight_peak.main()


WINDOW = ["--from", "2026-08-30T07:16:55", "--to", "2026-08-30T07:26:55"]


def test_an_empty_result_exits_non_zero_and_names_who_does_have_rows(
    monkeypatch, capsys
):
    """The default provider is stale, so this is the majority path.

    vllm-2 has taken no ledger row since 2026-08-27; of the 90 cycles the port
    newly makes reachable, 65 are empty under the default provider and 1 under
    `--provider all` (measured 2026-08-30). Exit 0 plus one bare line reads as
    "measured, and the box was idle" for a question that was never answered.
    """
    monkeypatch.setattr(inflight_peak, "fetch", lambda *a, **k: [])
    monkeypatch.setattr(
        inflight_peak, "providers_in_window",
        lambda frm, to, **k: [("vllm-3", 1104), ("vllm", 27)],
    )
    code = _run_main(monkeypatch, WINDOW)
    out = capsys.readouterr().out

    assert code == 1, "an empty answer must not exit 0"
    assert "no ledger rows for provider=vllm-2" in out
    assert "vllm-3 (1104)" in out, out
    assert "--provider vllm-3" in out, out


def test_an_unchecked_empty_is_not_reported_as_an_empty_window(monkeypatch, capsys):
    """`[]` is CHECKED AND EMPTY; `None` is COULD NOT CHECK.

    Collapsing them would let a failed follow-up query print "the window
    itself is empty" — a probe failing open on the evidence it exists to
    produce.
    """
    monkeypatch.setattr(inflight_peak, "fetch", lambda *a, **k: [])
    monkeypatch.setattr(inflight_peak, "providers_in_window", lambda *a, **k: None)
    code = _run_main(monkeypatch, WINDOW)
    out = capsys.readouterr().out

    assert code == 1
    assert "unexplained" in out, out
    assert "no provider has rows" not in out, out


def test_a_checked_empty_window_says_so_and_names_the_ledger(monkeypatch, capsys):
    """The third state: the window really is empty, everywhere.

    "the window itself is empty" is a claim about ONE store, and it is false if
    the read was misrouted — so the line names the database it is a claim
    about. Live, that reads `prism.requests`; under the mongo_store blur it
    would read `trading_bot.requests`, which does not exist.
    """
    monkeypatch.setattr(inflight_peak, "fetch", lambda *a, **k: [])
    monkeypatch.setattr(inflight_peak, "providers_in_window", lambda *a, **k: [])
    monkeypatch.setattr(inflight_peak, "_ledger_name", lambda: "prism.requests")
    code = _run_main(monkeypatch, WINDOW)
    out = capsys.readouterr().out

    assert code == 1
    assert "no provider has rows in this window in prism.requests" in out, out
    assert "unexplained" not in out, out


def test_a_non_empty_result_still_exits_zero(monkeypatch, capsys):
    """The other direction of the exit-code change: a real answer stays 0."""
    monkeypatch.setattr(
        inflight_peak, "fetch",
        lambda *a, **k: [{"createdAt": "2026-08-30T07:20:00.000Z", "totalTime": 12.0,
                          "provider": "vllm-2", "project": "p", "operation": "chat"}],
    )
    monkeypatch.setattr(
        inflight_peak, "providers_in_window",
        lambda *a, **k: pytest.fail("providers_in_window must not run on a hit"),
    )
    code = _run_main(monkeypatch, WINDOW)
    out = capsys.readouterr().out

    assert code == 0
    assert "PEAK OVERLAP    1" in out, out
