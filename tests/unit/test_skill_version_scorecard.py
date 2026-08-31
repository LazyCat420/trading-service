"""The skill scorecard must count the half of `skill_versions` that is a STRING.

`decision_outcomes.skill_versions` was a `jsonb` column, and it landed in Mongo
in TWO different shapes:

  * the backfill (`table_spec._coerce`) json-decodes every json/jsonb column, so
    every MIGRATED row is a subdocument — 445 documents, all created on or
    before 2026-08-18;
  * the live writer does not use that path. `outcome_tracker.record_decisions`
    builds the snapshot with `json.dumps(...)` and `mongo_store.insert_docs`
    stores the string it is handed, so every row written since the cutover is
    JSON **TEXT** — 56 documents, all created on or after 2026-08-20.

Measured on the live collection 2026-08-30: `{"skill_versions.v3_bear_agent":
{"$exists": True}}` matches 445 of the 501 stamped documents. Dot notation
cannot look inside a string, so the obvious Mongo port of
`LATERAL jsonb_each(skill_versions)` — a nested field filter, or
`$objectToArray` in a pipeline — drops the only half that is still growing, and
drops it silently: the report still prints, with numbers, and the newest version
of every agent is simply absent from it.

That is the failure this file exists to hold shut. Each test below is RED
against a scorecard that only understands the subdocument shape.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import skill_version_scorecard as svs  # noqa: E402

# One cycle's stamp, in the two shapes the collection actually holds.
SUBDOC = {"v3_bear_agent": 26, "v3_bull_agent": 19}
JSON_TEXT = '{"v3_bear_agent": 26, "v3_bull_agent": 19}'

D1 = dt.datetime(2026, 8, 18, 15, 2, 20)   # archive shape, pre-cutover
D2 = dt.datetime(2026, 8, 20, 3, 46, 47)   # live shape, post-cutover


@pytest.fixture
def rows(monkeypatch):
    """Stub `find_rows` and hand back the (query, columns) it was called with."""
    seen: dict = {}

    def _install(payload):
        def _find_rows(collection, query, columns, **kw):
            seen["collection"] = collection
            seen["query"] = query
            seen["columns"] = columns
            return payload
        monkeypatch.setattr(svs.mongo_query, "find_rows", _find_rows)
        return seen

    return _install


def test_a_json_text_stamp_is_counted_beside_a_subdocument(rows):
    """The regression. Both shapes are one bucket per (agent, version).

    RED against a dict-only decode: `n` comes back 1 instead of 2 and the
    window ends 2026-08-18 — i.e. the report stops at the cutover while
    claiming to describe the present.
    """
    rows([(SUBDOC, 4.0, "WIN", D1), (JSON_TEXT, -3.63, "LOSS", D2)])

    out, unreadable, _empty = svs.scorecard()

    assert unreadable == 0
    by_key = {(a, v): r for a, v, *r in
              [(r[0], r[1], *r[2:]) for r in out]}
    assert set(by_key) == {("v3_bear_agent", 26), ("v3_bull_agent", 19)}
    for key, (n, avg, wins, losses, first, last) in by_key.items():
        assert n == 2, f"{key}: the JSON-text row was dropped"
        assert wins == 1 and losses == 1
        assert avg == pytest.approx((4.0 - 3.63) / 2)
        assert (first, last) == (D1.date(), D2.date())


def test_the_query_never_filters_on_a_nested_skill_versions_field(rows):
    """`skill_versions.<agent>` is blind to the JSON-text half — 445 of 501.

    A filter with a dotted `skill_versions` key would still return rows and
    still print a scorecard, which is why this is asserted on the QUERY and not
    only on the output.
    """
    seen = rows([])
    svs.scorecard()

    dotted = [k for k in seen["query"] if k.startswith("skill_versions.")]
    assert not dotted, f"dot notation cannot see a JSON string: {dotted}"
    # IS NOT NULL, which `{"$ne": None}` already is — it matches neither a null
    # nor a missing field.
    assert seen["query"]["skill_versions"]["$ne"] is None
    assert seen["query"]["resolved_at"]["$ne"] is None
    assert seen["query"]["action"] == {"$in": ["BUY", "SELL"]}
    assert seen["collection"] == "decision_outcomes"   # the TABLE name, resolved once
    assert list(seen["columns"]) == ["skill_versions", "pnl_pct", "outcome", "created_at"]


def test_count_star_and_avg_keep_different_denominators(rows):
    """`count(*)` counts the row; `avg(pnl_pct)` skips a NULL. Postgres column
    DEFAULTs are gone, so a post-cutover document can simply lack the field."""
    rows([(SUBDOC, 4.0, "WIN", D1), (JSON_TEXT, None, "FLAT", D2)])

    out, _, _empty = svs.scorecard()

    n, avg = out[0][2], out[0][3]
    assert n == 2, "count(*) must count a row with no pnl"
    assert avg == pytest.approx(4.0), "avg() must not treat a missing pnl as 0"


def test_an_undecodable_stamp_is_reported_not_silently_dropped(rows):
    """A payload that does not parse must not pass for an absent one."""
    rows([(SUBDOC, 1.0, "WIN", D1),
          ("{not json", 9.0, "WIN", D2),
          ("[1, 2, 3]", 9.0, "WIN", D2),     # parses, but is not {agent: version}
          ({"v3_bear_agent": "twenty-six"}, 9.0, "WIN", D2)])

    out, unreadable, _empty = svs.scorecard()

    assert unreadable == 3
    assert [(r[0], r[1], r[2]) for r in out] == [
        ("v3_bear_agent", 26, 1), ("v3_bull_agent", 19, 1)]


def test_skill_map_normalises_the_version_the_way_the_sql_cast_did():
    """`(value #>> '{}')::int` — `->>` yields TEXT, so "26" and 26 were equal."""
    assert svs.skill_map({"a": "26"}) == {"a": 26}
    assert svs.skill_map('{"a": 26}') == {"a": 26}
    assert svs.skill_map({"a": 26}) == {"a": 26}
    assert svs.skill_map(None) == {}
    assert svs.skill_map("") == {}


def test_the_scorecard_reads_no_postgres():
    """The module must not reach the frozen archive. Before this port it did —
    and it did so successfully, printing an August scorecard that stopped at
    the cutover with no error anywhere."""
    src = (REPO / "scripts" / "skill_version_scorecard.py").read_text(encoding="utf-8")
    for needle in ("pg_connection", "DATABASE_URL", "get_db(", "import psycopg"):
        assert needle not in src, f"Postgres coupling left in place: {needle}"


# ── the two states the report used to describe wrongly ───────────────────
#
# Both found by the adversarial review of the Mongo port, both live-reachable,
# both red on the pre-fix tree.

def _run_main(monkeypatch, rows):
    """Drive main() with `rows` as the whole decision_outcomes read."""
    import io
    import contextlib

    import scripts.skill_version_scorecard as mod

    monkeypatch.setattr(mod.mongo_query, "find_rows",
                        lambda *a, **k: list(rows))
    monkeypatch.setattr(mod.mongo_query, "agg_row", lambda *a, **k: (1.0,))
    monkeypatch.setattr(sys, "argv", ["skill_version_scorecard.py"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = mod.main()
    return code, buf.getvalue()


def test_every_stamp_unreadable_is_not_reported_as_nothing_accrued(monkeypatch):
    """The warning used to sit after `if not rows: return 0`.

    So the one state it exists to describe — every stamped decision carrying a
    payload that will not decode — printed "No resolved decisions carry a skill
    version yet" and exited 0, which is exactly what a healthy empty store
    prints. A fault must not be reportable as an absence.
    """
    rows = [("{not json", 1.0, "WIN", "2026-08-01")] * 7
    code, out = _run_main(monkeypatch, rows)
    assert "WARNING" in out, out
    assert "7 stamped decision(s)" in out
    assert code == 1, "an all-unreadable store is not a clean exit"


def test_an_empty_stamp_is_not_called_a_broken_one(monkeypatch):
    """`{}` decodes fine and simply governs nobody.

    `LATERAL jsonb_each('{}')` yielded zero rows under Postgres and the report
    said nothing about it. Counting it as "does not decode" accuses the writer
    of corruption for a row that is merely uninteresting.
    """
    rows = [
        ('{"v3_bear_agent": 26}', 1.0, "WIN", "2026-08-01"),
        ("{}", 1.0, "WIN", "2026-08-01"),
        ({}, 1.0, "LOSS", "2026-08-01"),
        (None, 1.0, "WIN", "2026-08-01"),
    ]
    code, out = _run_main(monkeypatch, rows)
    assert "does not decode" not in out, out
    assert "EMPTY skill_versions" in out
    assert "3 decision(s)" in out
    assert code == 0


def test_a_genuinely_broken_stamp_is_still_called_broken(monkeypatch):
    """Negative control for the test above — the distinction must cut both ways."""
    rows = [
        ('{"v3_bear_agent": 26}', 1.0, "WIN", "2026-08-01"),
        ("{not json", 1.0, "LOSS", "2026-08-01"),
    ]
    code, out = _run_main(monkeypatch, rows)
    assert "does not decode" in out
    assert "1 stamped decision(s)" in out
