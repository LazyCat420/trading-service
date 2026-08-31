"""`scripts/shadow_report.py` must read Mongo, and must date what it prints.

WHY THIS FILE EXISTS
--------------------
The shadow report is the empirical input for one decision: promote the
contradiction shadow into a real gate, or don't. Until 2026-08-30 it read
Postgres, which stopped taking writes at the 2026-08-19 cutover, and it did not
fail — it printed

    desks analyzed:            635
      ≥1 contradiction:        115  (18%)
      would_downgrade_to_hold: 31  (5%)

under the heading "all shadow-era desks", with no date anywhere above the
numbers and a most-recent row of 2026-08-07. `--hours 24` printed "No desks
carry shadow telemetry yet." Every one of those lines is what a current answer
looks like, and all of them described July.

Three things are pinned here, and each was red before the port:

  1. the file has no Postgres coupling at all (it had three: `import psycopg`
     and two `.execute()` sites);
  2. the reader decodes `desk_data` in Python and does NOT push a dotted path
     into the Mongo filter — `{"desk_data.agent_telemetry.agent":
     "contradiction_shadow"}` returns 635 on the live store, which is exactly
     the Postgres answer and therefore looks correct, while being the frozen
     archive with all 183 post-cutover desks silently dropped (818 is right);
  3. the output names the window it covers and dates its headline counts, and
     it separates the evidence that rests on `tournament_result` — a source
     that last produced a claim on 2026-07-29 and cannot produce another,
     because the tournament was deleted on 2026-08-28 and the two surviving
     writers of that artifact hardcode `action: HOLD`, which the shadow's
     `_norm_action` maps to NEUTRAL and `_extract_claims` refuses to turn into
     a claim. 81 of 115 flagged desks and 23 of 31 would-downgrade cases in the
     archive rest on it.

The reads are STUBBED, not live: the numbers this report produces move with the
store, and a test that asserts today's counts fails tomorrow for no defect. The
live comparison is kept as an explicit probe at the bottom.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# By path, not by putting scripts/ on sys.path — that directory holds ~120
# top-level modules that would shadow real ones for the rest of the session.
_SPEC = importlib.util.spec_from_file_location(
    "shadow_report", REPO / "scripts" / "shadow_report.py")
sr = importlib.util.module_from_spec(_SPEC)
sys.modules["shadow_report"] = sr
_SPEC.loader.exec_module(sr)

from scripts.gate_zero_pg import scan  # noqa: E402

NOW = datetime.now(timezone.utc)


# ── 1. the coupling is gone ────────────────────────────────────────────────

def test_the_report_has_no_postgres_coupling():
    """RED before the port: 3 findings — `import psycopg` at line 22 and the
    two `cur.execute(...)` sites at lines 32 and 39."""
    result = scan(REPO, targets=("scripts/shadow_report.py",))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, (
        "shadow_report.py still reads Postgres: "
        + "; ".join(f"{f['kind']} at line {f['line']}" for f in result["findings"]))


def test_the_scan_can_still_fail(tmp_path):
    """NEGATIVE CONTROL. A scan that finds nothing because it looked at nothing
    passes the assertion above just as happily."""
    (tmp_path / "reader.py").write_text(
        "import psycopg\n"
        "def f():\n"
        "    with psycopg.connect('x') as c:\n"
        "        c.execute('SELECT ticker FROM shared_desk')\n",
        encoding="utf-8")
    assert scan(tmp_path, targets=("reader.py",))["total"] > 0


# ── 2. both shapes of desk_data, and the filter that must not be pushed down ──

def _shadow(*, claims, contradictions, sentiment, action, downgrade):
    return {
        "agent": "contradiction_shadow",
        "claims_extracted": claims,
        "contradictions": contradictions,
        "contradiction_count": len(contradictions),
        "sentiment_by_source": sentiment,
        "final_action": action,
        "final_confidence": 70,
        "would_downgrade_to_hold": downgrade,
    }


_LIVE_DESK = _shadow(
    claims=4,
    contradictions=[{
        "description": "conflicting sentiment for AAA",
        "source_ref_1": "fundamental_report", "source_ref_2": "quant_report",
        "severity": "high",
    }],
    sentiment={"fundamental_report": "BULLISH", "quant_report": "BEARISH"},
    action="BUY", downgrade=True,
)

_ARCHIVE_DESK = _shadow(
    claims=3,
    contradictions=[{
        "description": "conflicting sentiment for BBB",
        "source_ref_1": "fundamental_report", "source_ref_2": "tournament_result",
        "severity": "high",
    }],
    sentiment={"fundamental_report": "BULLISH", "tournament_result": "BEARISH"},
    action="BUY", downgrade=True,
)


# ── the two shapes that broke the retired-source recount ──────────────────
#
# Both `_LIVE_DESK` and `_ARCHIVE_DESK` above carry a "conflicting sentiment"
# description AND a BULLISH/BEARISH split, so they satisfy the recount's
# conditions twice over and cannot fail it. The two desks below are the shapes
# that actually occur on the live store and that the first recount got wrong.

# LMT 2026-07-23 13:46 (also BLSH 2026-07-20 20:21, INTC 2026-07-20 13:52).
# Every source BULLISH — nothing to disagree about directionally — and the one
# contradiction is a PRICE-TARGET divergence citing nothing retired. The shadow
# flags it anyway, via the `zip(contradictions, clusters)` disjunct. Removing
# `tournament_result` cannot touch any of that, so the restated answer is the
# stored one; a recount built from the description and the sentiment split
# alone returns False and books the loss against the retirement.
_PRICE_TARGET_ONLY_DESK = _shadow(
    claims=6,
    contradictions=[{
        "description": "Price targets severely diverge: 1.25 vs 165.0",
        "source_ref_1": "final_decision.take_profit",
        "source_ref_2": "trade_decision.take_profit",
        "severity": "warning",
    }],
    sentiment={"quant_report": "BULLISH", "final_decision": "BULLISH",
               "trade_decision": "BULLISH", "tournament_result": "BULLISH",
               "fundamental_report": "BULLISH"},
    action="BUY", downgrade=True,
)

# MSCI 2026-07-23 07:59, and five more (MSFT, TSM, PEP, NFLX, DIS). The
# contradiction DOES cite `tournament_result` — but only because it is the
# first BULLISH voice in `_extract_claims` order, and `final_decision` /
# `trade_decision` are still BULLISH against a BEARISH `quant_report`. Drop
# `tournament_result` and the identical contradiction is re-emitted naming
# `final_decision`; the flag stands. Filtering contradictions by cited name
# deletes all six.
_RETIRED_CITED_BUT_NOT_NEEDED_DESK = _shadow(
    claims=5,
    contradictions=[{
        "description": "Conflicting sentiment detected for entity.",
        "source_ref_1": "tournament_result", "source_ref_2": "quant_report",
        "severity": "warning",
    }],
    sentiment={"quant_report": "BEARISH", "final_decision": "BULLISH",
               "trade_decision": "BULLISH", "tournament_result": "BULLISH"},
    action="BUY", downgrade=True,
)

_ARCHIVE_STAMP = datetime(2026, 7, 29, 20, 8)


@pytest.fixture
def rows(monkeypatch):
    """Three desks: one live (desk_data as JSON TEXT), one archived (desk_data
    as a sub-document), one carrying no shadow telemetry at all.

    Both shapes are real. Measured on the live collection 2026-08-30:
    274 documents store `desk_data` as a string, 1,762 as an object.
    """
    seen: dict = {}
    data = [
        ("AAA", "PM_DONE",
         json.dumps({"agent_telemetry": [{"agent": "other"}, _LIVE_DESK]}),
         NOW.replace(tzinfo=None) - timedelta(hours=3)),
        ("BBB", "PM_DONE",
         {"agent_telemetry": [_ARCHIVE_DESK]},
         _ARCHIVE_STAMP),
        ("CCC", "INIT", json.dumps({"agent_telemetry": []}),
         NOW.replace(tzinfo=None) - timedelta(hours=1)),
    ]

    def fake_find_rows(collection, query, columns, sort=None, limit=0):
        seen["collection"] = collection
        seen["query"] = query
        seen["columns"] = list(columns)
        seen["sort"] = sort
        seen["limit"] = limit
        # Honours `limit` the way the real seam does — `mongo_store.find_docs`
        # sorts server-side and then truncates. A stub that accepted `limit`
        # and ignored it made trap 2 (a sample of the PAST) invisible to every
        # offline test: injecting `limit=100` into `_iter_desks` left all of
        # them green and failed only the opt-in live probe.
        rows = sorted(data, key=lambda r: r[3], reverse=True)
        return rows[:limit] if limit else rows

    monkeypatch.setattr(sr.mongo_query, "find_rows", fake_find_rows)
    return seen


def test_the_json_text_half_is_decoded_in_python(rows, capsys):
    """Trap 1. `desk_data` is JSON TEXT on the live write path, so no
    server-side path can look inside it. Both desks must be counted."""
    sr.main([])
    out = capsys.readouterr().out
    assert "desks analyzed:            2" in out
    assert "desks scanned in window:   3" in out


def test_no_dotted_desk_data_path_reaches_the_query(rows):
    """The filter that returns exactly the old, wrong answer.

    `count({"desk_data.agent_telemetry.agent": "contradiction_shadow"})` is 635
    on the live store — identical to what the Postgres version printed, and
    therefore the most convincing wrong answer available. It can only match the
    1,762 migrated sub-document rows; the 274 live string rows are invisible to
    it."""
    sr.main([])
    assert rows["collection"] == "shared_desk"
    keys = json.dumps(rows["query"])
    assert "desk_data" not in keys, (
        f"the reader pushed a desk_data path into Mongo: {rows['query']}")
    assert rows["columns"] == ["ticker", "phase", "desk_data", "updated_at"]
    assert rows["sort"] == [("updated_at", -1)]


def test_the_reader_asks_for_every_document_not_a_sample(rows):
    """Trap 2. Natural order returns the OLDEST documents, so any `limit` on a
    growing collection samples the past — and on this collection the past is
    the frozen archive, which is the exact failure the port exists to fix. The
    reader must ask for all 2,036, and the assertion has to be on the argument:
    a `limit=100` against a 3-row stub still returns all 3 rows."""
    sr.main([])
    assert rows["limit"] == 0, (
        f"the reader capped its read at {rows['limit']} documents; on the live "
        "store natural order hands back the oldest desks first")


def test_the_hours_flag_bounds_the_query_server_side(rows):
    """`--hours` becomes a real filter on a real store again. Against the
    frozen archive it could only ever return nothing."""
    sr.main(["--hours", "24"])
    bound = rows["query"]["updated_at"]["$gt"]
    assert isinstance(bound, datetime)
    assert timedelta(hours=23) < (NOW - bound) < timedelta(hours=25)


# ── 3. the output cannot be mistaken for current ───────────────────────────

def test_the_report_names_the_window_it_covers(rows, capsys):
    """RED before: the old output's first line was the heading and its second
    was `desks analyzed`. There was no store, no clock and no window anywhere."""
    sr.main([])
    out = capsys.readouterr().out
    assert "MongoDB · shared_desk" in out
    assert "generated:" in out
    assert "window requested:" in out
    assert "desks scanned in window:" in out
    assert f"{_ARCHIVE_STAMP:%Y-%m-%d}" in out, "the window's lower bound is not printed"


def test_the_headline_counts_carry_the_date_of_their_newest_instance(rows, capsys):
    """A bare `31` reads as "31, lately". On the live store the most recent
    would-downgrade is 2026-07-27 while the telemetry feed is hours old."""
    sr.main([])
    out = capsys.readouterr().out
    assert "most recent contradiction:" in out
    assert "most recent would-downgrade:" in out
    assert "days ago" in out


def test_a_month_old_window_says_so_before_the_numbers(monkeypatch, capsys):
    """The whole defect, in one assertion. The Postgres version printed these
    same counts with nothing above them to say they were five weeks old."""
    old = datetime(2026, 7, 29, 20, 8)
    monkeypatch.setattr(sr.mongo_query, "find_rows", lambda *a, **k: [
        ("BBB", "PM_DONE", {"agent_telemetry": [_ARCHIVE_DESK]}, old)])
    sr.main([])
    out = capsys.readouterr().out
    head, _, tail = out.partition("desks analyzed:")
    assert "⚠ STALE" in head, "the staleness banner must precede the counts"
    assert "days old" in head
    assert tail, "the counts still have to be printed"


def test_a_fresh_window_does_not_cry_wolf(rows, capsys):
    """A banner on every run is a banner nobody reads. The live desk is hours
    old, so the feed itself is not stale — only the evidence in it is, and that
    is what the per-count dates say."""
    sr.main([])
    out = capsys.readouterr().out
    head = out.split("desks analyzed:")[0]
    assert "⚠ STALE" not in head


def test_the_downgrade_rate_gets_the_denominator_that_could_have_downgraded(rows, capsys):
    """`31 of 818 (4%)` and `31 of 124 (25%)` are the same 31 desks, and only
    the second answers "would this gate have changed a trade". A HOLD, and a
    desk that never decided, could not have been downgraded by anything —
    post-cutover only 2 of 183 shadow desks ended in a live BUY/SELL at all."""
    sr.main([])
    out = capsys.readouterr().out
    assert "desks that ended BUY/SELL: 2" in out
    assert "of which would_downgrade: 2 (100%)" in out


def test_an_empty_window_says_what_it_scanned(monkeypatch, capsys):
    """Trap 7. `[]` is a red result unless the output shows why it is right, and
    the Postgres version's "No desks carry shadow telemetry yet." could not tell
    'no desks' from 'desks, no telemetry'."""
    monkeypatch.setattr(sr.mongo_query, "find_rows",
                        lambda *a, **k: [("ZZZ", "INIT", "{}", NOW.replace(tzinfo=None))])
    sr.main([])
    out = capsys.readouterr().out
    assert "desks scanned in window:   1" in out
    assert "none carrying contradiction_shadow telemetry" in out


# ── 4. the tournament_result half is dead, and is accounted for as such ────

def test_tournament_result_is_recorded_as_retired():
    assert "tournament_result" in sr.RETIRED_SOURCES
    assert "2026-08-28" in sr.RETIRED_SOURCES["tournament_result"]


def test_the_source_of_a_field_ref_is_the_artifact():
    assert sr._source_of("final_decision.take_profit") == "final_decision"
    assert sr._source_of("tournament_result") == "tournament_result"
    assert sr._source_of(None) == "?"


def test_a_contradiction_citing_a_retired_source_is_identified():
    assert sr._cites_retired(_ARCHIVE_DESK["contradictions"][0]) is True
    assert sr._cites_retired(_LIVE_DESK["contradictions"][0]) is False


def test_removing_a_retired_source_can_unmake_the_conflict_it_caused():
    """Not a subtraction. `tournament_result` was the only BEARISH voice on the
    archived desk, so dropping it removes the directional conflict the
    would-downgrade flag needs — the recount has to re-run the shadow's three
    conditions, not decrement a counter."""
    assert _ARCHIVE_DESK["would_downgrade_to_hold"] is True
    assert sr._would_downgrade_without_retired(_ARCHIVE_DESK) is False
    assert sr._would_downgrade_without_retired(_LIVE_DESK) is True


def test_the_recount_reproduces_the_stored_flag_when_nothing_is_removed(
        monkeypatch):
    """The measurement that rejected the first port, as a test.

    RED before: with `RETIRED_SOURCES` emptied — nothing removed at all — the
    previous recount returned True for only 28 of the 31 live desks whose
    telemetry says `would_downgrade_to_hold: True`. A restatement that cannot
    reproduce the number it restates is not a restatement; the three desks it
    lost were booked against the retirement, and the headline came out 5 where
    the answer is 14.

    `_PRICE_TARGET_ONLY_DESK` is one of those three, in its real shape.

    Driven by emptying `RETIRED_SOURCES` rather than by an argument, so the
    assertion is about the recount's ANSWER and not about its signature.
    """
    monkeypatch.setattr(sr, "RETIRED_SOURCES", {})
    for name, desk in (
        ("live", _LIVE_DESK),
        ("archive", _ARCHIVE_DESK),
        ("price-target-only (LMT/BLSH/INTC)", _PRICE_TARGET_ONLY_DESK),
        ("cited-but-not-needed (MSCI/MSFT/TSM/PEP/NFLX/DIS)",
         _RETIRED_CITED_BUT_NOT_NEEDED_DESK),
    ):
        assert (sr._would_downgrade_without_retired(desk)
                == desk["would_downgrade_to_hold"]), (
            f"the recount cannot reproduce the {name} desk's own flag with "
            "nothing removed, so its restated value is not evidence")


def test_a_conflict_the_removal_cannot_touch_keeps_its_flag():
    """The LMT shape. Unanimous sentiment, one price-target contradiction, no
    retired source cited: deleting `tournament_result` changes nothing about
    this desk, so the restated answer is the stored answer.

    RED before: returned False, because the old recount ran only the
    description and BULLISH/BEARISH disjuncts of `has_directional_conflict`
    and this desk is flagged solely by the cluster-predicate one."""
    assert sr._cites_retired(_PRICE_TARGET_ONLY_DESK["contradictions"][0]) is False
    assert sr._would_downgrade_without_retired(_PRICE_TARGET_ONLY_DESK) is True


def test_citing_a_retired_source_is_not_depending_on_one():
    """The shape the rejection did not find, and it is six desks, not three.

    A sentiment contradiction names the FIRST bullish and FIRST bearish voice.
    `tournament_result` is merely first here; `final_decision` and
    `trade_decision` are still BULLISH against a BEARISH `quant_report`, so the
    same contradiction is re-emitted without it. RED before: the recount kept
    only contradictions that cite nothing retired, so this desk's only
    contradiction vanished and the flag flipped to False."""
    c = _RETIRED_CITED_BUT_NOT_NEEDED_DESK["contradictions"][0]
    assert sr._cites_retired(c) is True, "the fixture must cite the retired source"
    assert sr._would_downgrade_without_retired(
        _RETIRED_CITED_BUT_NOT_NEEDED_DESK) is True
    assert sr._surviving_contradictions(_RETIRED_CITED_BUT_NOT_NEEDED_DESK) == 1


def test_a_price_target_contradiction_citing_a_retired_source_is_undecidable():
    """The one gap, reported as a gap rather than as a False.

    Only the winning min/max pair is persisted, so once a retired source is one
    of them there is no way to tell whether another pair still diverges by more
    than 2x. `None` keeps that desk out of both the survived and the removed
    column instead of quietly counting it as disposed of. Zero live desks are
    in this state today; the branch exists so that stays a measured fact."""
    desk = _shadow(
        claims=4,
        contradictions=[{
            "description": "Price targets severely diverge: 10.0 vs 90.0",
            "source_ref_1": "tournament_result.price_target",
            "source_ref_2": "final_decision.take_profit",
            "severity": "warning",
        }],
        sentiment={"final_decision": "BULLISH", "tournament_result": "BULLISH"},
        action="BUY", downgrade=True,
    )
    assert sr._surviving_contradictions(desk) is None
    assert sr._would_downgrade_without_retired(desk) is None


def test_the_restated_headline_reconciles_with_the_depending_count(
        monkeypatch, capsys):
    """Defect 2, end to end. The rejected output printed
    `would_downgrade_to_hold: 31`, then `23 of 31 ... cite a retired source`,
    then `restated ... 5` — and 31 − 23 is not 5. Whatever the counts are, the
    restated line and the line above it must subtract to each other on screen,
    and the report must say how much of its own recount it can vouch for."""
    stamp = datetime(2026, 7, 23, 13, 46)
    desks = [_LIVE_DESK, _ARCHIVE_DESK, _PRICE_TARGET_ONLY_DESK,
             _RETIRED_CITED_BUT_NOT_NEEDED_DESK]
    monkeypatch.setattr(sr.mongo_query, "find_rows", lambda *a, **k: [
        (f"TK{i}", "PM_DONE", {"agent_telemetry": [d]},
         stamp - timedelta(minutes=i))
        for i, d in enumerate(desks)])
    sr.main([])
    out = capsys.readouterr().out

    assert "would_downgrade_to_hold: 4  (100%)" in out
    # two cite a retired source; only ONE depends on it
    assert "2 of 4 would-downgrade cases (50%)" in out
    depending = out.split("evidence DEPENDING on one")[1]
    assert "1 of 4 would-downgrade cases (25%)" in depending
    assert "citing ≠ depending" in out

    restated = out.split("restated with retired sources removed:")[1]
    assert "would_downgrade_to_hold: 3  (75%)   ← 4 − 1" in restated, restated
    assert "≥1 contradiction:        3  (75%)   ← 4 − 1" in restated, restated
    assert "recount fidelity: re-derives 4 of 4 stored " \
           "would_downgrade_to_hold flags with nothing removed" in out


def test_the_report_restates_its_headline_without_the_dead_source(rows, capsys):
    """Both fixture desks are flagged and both would downgrade; only one of them
    still could. The archive's real ratios are 81/115 and 23/31."""
    sr.main([])
    out = capsys.readouterr().out
    assert "retired sources" in out
    assert "tournament_result: last claim 2026-07-29 20:08 UTC" in out
    assert "1 of 2 flagged desks (50%)" in out
    assert "1 of 2 would-downgrade cases (50%)" in out
    assert "restated with retired sources removed:" in out
    body = out.split("restated with retired sources removed:")[1]
    assert "≥1 contradiction:        1" in body
    assert "would_downgrade_to_hold: 1" in body
    assert "[retired source]" in out


def test_decode_handles_both_stored_shapes():
    assert sr._decode('{"a": 1}') == {"a": 1}
    assert sr._decode({"a": 1}) == {"a": 1}
    assert sr._decode(None) == {}
    assert sr._decode("not json") == {}


# ── 5. the live probe, run by hand ─────────────────────────────────────────

@pytest.mark.real_mongo
@pytest.mark.skipif(not os.environ.get("SHADOW_REPORT_LIVE"),
                    reason="live probe — set SHADOW_REPORT_LIVE=1")
def test_the_dotted_path_undercounts_the_live_collection():
    """The measurement the stubs stand in for. Recorded 2026-08-30:

        dotted-path filter                      635   ← the Postgres answer
        decoded in Python (this script)         818
        difference                              183   post-cutover desks
    """
    from app.db import mongo_store

    dotted = mongo_store.count_docs(
        "shared_desk", {"desk_data.agent_telemetry.agent": "contradiction_shadow"})
    decoded = sum(1 for *_rest, shadow, _d in sr._iter_desks() if shadow is not None)
    assert decoded > dotted, (
        f"the dotted path found {dotted} and the decode found {decoded}; if these "
        "are equal, either every desk_data is a sub-document again or the reader "
        "regressed onto the archive half")


@pytest.mark.real_mongo
@pytest.mark.skipif(not os.environ.get("SHADOW_REPORT_LIVE"),
                    reason="live probe — set SHADOW_REPORT_LIVE=1")
def test_the_recount_reproduces_every_stored_flag_on_the_live_store():
    """The rejection's own measurement, against the whole collection.

    Recorded 2026-08-30 with nothing retired:

        previous recount   reproduced 28 of 31 stored would-downgrade flags
                           (lost LMT 07-23 13:46, BLSH 07-20 20:21,
                            INTC 07-20 13:52 — all price-target-only)
        this recount       reproduces 818 of 818 shadow desks, 0 mismatches

    Fixtures can only stand in for shapes someone thought of; this one asks the
    store whether a fourth shape exists."""
    bad = []
    for ticker, _phase, upd, shadow, _d in sr._iter_desks():
        if shadow is None:
            continue
        got = sr._would_downgrade_without_retired(shadow, frozenset())
        if got != bool(shadow.get("would_downgrade_to_hold")):
            bad.append((ticker, upd, got, shadow.get("would_downgrade_to_hold")))
    assert not bad, (
        f"{len(bad)} desk(s) the recount cannot reproduce with nothing "
        f"removed: {bad[:5]}")
