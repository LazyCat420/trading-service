"""`scripts/gate_ablation.py` reads MongoDB, and reads it correctly.

WHAT WAS WRONG BEFORE
---------------------
Both of the script's statements went to Postgres through
`scripts.migration.pg_connection.get_db`. Since `DATABASE_URL` was removed from
settings that raises `AttributeError` on the first call, so the script answered
nothing at all — but the failure mode this file exists to prevent is the other
one: a port that runs, prints a report, and is wrong. Each test below pins one
way this particular port could look healthy and be wrong, and every one of them
FAILS against a deliberately broken copy of the ported code (verified by
mutating the module in-place — see the docstring on each test for the mutation
that kills it).

Nothing here touches a live store: both seams (`mongo_query`, `mongo_store`)
are replaced with recorders, so the tests assert the QUERY that would be sent,
which is where all four of the traps live.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import scripts.gate_ablation as G  # noqa: E402
from app.quant import returns as _returns  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_logging():
    """`gate_ablation` calls `logging.disable(CRITICAL)` at import — importing it
    must not silence the rest of the suite."""
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture(autouse=True)
def _clear_cache():
    G._FWD.clear()
    yield
    G._FWD.clear()


# ── the window bound ────────────────────────────────────────────────────

def test_the_since_bound_is_a_datetime_not_a_string(monkeypatch):
    """MUTATION THAT KILLS THIS: pass `since` straight through to `$gte`.

    `shared_desk.created_at` is a BSON Date. Mongo orders values by TYPE before
    value, so a Date is never `>=` a String: the filter matches ZERO documents
    and the script prints "loaded 0 desks — nothing to do" and exits 1. No
    exception, no stack trace, and the number it reports is a lie about the
    store rather than about the code.
    """
    seen = []

    def fake(collection, query, columns, sort=None, limit=0):
        seen.append((collection, query))
        return []

    monkeypatch.setattr(G.mongo_query, "find_rows", fake)
    G.load_desks("2026-06-18")

    coll, query = seen[0]
    assert coll == "shared_desk"
    bound = query["created_at"]["$gte"]
    assert isinstance(bound, datetime), (
        f"the window bound is a {type(bound).__name__}; a string bound matches "
        "no BSON Date and the script reports an empty window as if it were data")
    assert bound == datetime(2026, 6, 18)


def test_a_bad_since_is_refused_rather_than_matching_nothing():
    with pytest.raises(SystemExit):
        G._since_datetime("last tuesday")


# ── the LEFT JOIN on a COMPOSITE key ────────────────────────────────────

_DESK = json.dumps({"ticker": "AAA", "final_decision": {"action": "BUY"}})


def _two_desks_one_result(monkeypatch):
    """One cycle, two tickers, and a trade_results row for only ONE of them."""
    def fake(collection, query, columns, sort=None, limit=0):
        if collection == "shared_desk":
            return [("cyc-1", "AAA", datetime(2026, 8, 1), _DESK),
                    ("cyc-1", "BBB", datetime(2026, 8, 1), _DESK)]
        assert collection == "trade_results"
        assert query == {"cycle_id": {"$in": ["cyc-1"]}}
        return [("cyc-1", "AAA", "EXECUTE_BUY")]

    monkeypatch.setattr(G.mongo_query, "find_rows", fake)
    return {r["ticker"]: r for r in G.load_desks("2026-06-18")}


def test_the_join_matches_on_ticker_as_well_as_cycle(monkeypatch):
    """MUTATION THAT KILLS THIS: key `stored` on cycle_id alone (or use
    `mongo_query.left_join_rows`, whose ON clause is a single equality).

    The SQL joined `t.cycle_id = s.cycle_id AND t.ticker = s.ticker`. Dropping
    the ticker hands BBB the action AAA's desk produced — a fidelity MISMATCH
    that is indistinguishable from a genuine replay divergence, and one that
    would push the fidelity number under the 95% refusal threshold for reasons
    that have nothing to do with the gates.
    """
    by = _two_desks_one_result(monkeypatch)
    assert by["AAA"]["stored"] == "EXECUTE_BUY"
    assert by["BBB"]["stored"] is None, (
        "BBB was handed another ticker's policy_action — the join lost its "
        "second key")


def test_a_desk_with_no_trade_result_survives(monkeypatch):
    """MUTATION THAT KILLS THIS: an inner join instead of a LEFT one.

    `n_fired` is counted over every desk; only the ones with a stored action
    are `checked` for fidelity. An inner join silently deletes the unverified
    population, so both the funnel and every gate's `n_fired` shrink while the
    fidelity percentage goes UP — the report looks better for losing data.
    """
    by = _two_desks_one_result(monkeypatch)
    assert set(by) == {"AAA", "BBB"}


def test_desk_data_is_parsed_as_json_text(monkeypatch):
    """`shared_desk.desk_data` is a JSON *string*, not a subdocument (0 of the
    2,036 stored desks would match a filter on `desk_data.<field>`)."""
    by = _two_desks_one_result(monkeypatch)
    assert by["AAA"]["desk"] == {"ticker": "AAA",
                                 "final_decision": {"action": "BUY"}}


# ── the forward-return window ───────────────────────────────────────────

def _series(*days):
    """(dates, closes) for a run of consecutive closes 100, 101, 102, ..."""
    dates = [datetime.fromisoformat(d) for d in days]
    return dates, [100.0 + i for i in range(len(days))]


def test_entry_is_the_bar_on_the_day_and_exit_is_the_nth_bar_after():
    """MUTATION THAT KILLS THIS: `bisect_left`, or `after + horizon`.

    The SQL was `date <= day ORDER BY date DESC LIMIT 1` for the entry and
    `date > day ORDER BY date ASC OFFSET horizon-1 LIMIT 1` for the exit. Both
    off-by-ones are silent: the return keeps its sign and rough size, so the
    good/bad split the whole report is built on shifts by one session with
    nothing to show for it.
    """
    dates, closes = _series("2026-08-03", "2026-08-04", "2026-08-05",
                            "2026-08-06", "2026-08-07", "2026-08-10")
    # entry = the 08-03 bar (100.0); h=1 -> the next bar, 08-04 (101.0)
    assert G._forward_pct("T", "2026-08-03", dates, closes, 1) == pytest.approx(1.0)
    # h=3 -> the third bar after 08-03, i.e. 08-06 (103.0)
    assert G._forward_pct("T", "2026-08-03", dates, closes, 3) == pytest.approx(3.0)
    # h=5 -> 08-10 (105.0); h=6 would run off the end
    assert G._forward_pct("T", "2026-08-03", dates, closes, 5) == pytest.approx(5.0)
    assert G._forward_pct("T", "2026-08-03", dates, closes, 6) is None


def test_a_desk_day_with_no_bar_enters_on_the_previous_close():
    """A desk raised on a Saturday still has an entry: the last bar on/before."""
    dates, closes = _series("2026-08-07", "2026-08-10", "2026-08-11")
    # 08-08 is a Saturday: entry = the 08-07 bar (100.0), h=1 -> 08-10 (101.0)
    assert G._forward_pct("T", "2026-08-08", dates, closes, 1) == pytest.approx(1.0)


def test_a_zero_or_missing_entry_close_is_not_a_return():
    dates, closes = _series("2026-08-03", "2026-08-04")
    assert G._forward_pct("T", "2026-08-03", dates, [0.0, 101.0], 1) is None
    assert G._forward_pct("T", "2026-08-03", dates, [None, 101.0], 1) is None


def test_the_entry_falls_back_beyond_the_primed_window(monkeypatch):
    """MUTATION THAT KILLS THIS: `entry = closes[after - 1] if after else None`.

    The primed series starts 30 days before the earliest desk day; a ticker
    whose most recent bar is older than that — a stale or delisted name, which
    is exactly the population HOLD_POLICY_BLOCKED_STALE_PRICE_DATA exists for —
    has no bar inside it. The unbounded SQL still found one. Dropping the
    fallback scores those desks as "no forward return" and quietly removes the
    stale names from every gate's `n_scored`. It fires on 3 of 607 ticker-days
    in the default window, so it is neither dead code nor common enough to
    notice by eye.
    """
    called = []

    def fake_entry(ticker, on):
        called.append((ticker, on))
        return 50.0

    monkeypatch.setattr(G, "_entry_close", fake_entry)
    dates, closes = _series("2026-08-10", "2026-08-11")   # both AFTER the day
    got = G._forward_pct("T", "2026-08-03", dates, closes, 1)

    assert called == [("T", datetime(2026, 8, 3))]
    assert got == pytest.approx(100.0 * (100.0 - 50.0) / 50.0)


# ── the vendor pin (trap 8) ─────────────────────────────────────────────

def test_every_price_history_read_pins_one_vendor(monkeypatch):
    """MUTATION THAT KILLS THIS: drop `_one_vendor(...)` from either read.

    `price_history` is keyed (ticker, date, SOURCE) and the vendors' closes
    disagree by ~20% on average. Measured against this window on 2026-08-30:
    the unpinned statement this replaced disagreed with the pinned read on
    **192 of 578** scored ticker-days (33.2%, mean |delta| 2.85pp, max 13.2pp),
    and **46 of them (8.0%) changed SIGN** — i.e. moved a blocked trade between
    `blocked_good` and `blocked_bad`, which are the two numbers the entire
    report is a split of.
    """
    monkeypatch.setattr(_returns, "dominant_source_for", lambda t: "yfinance")

    queries = []

    def fake_find_docs(collection, query, sort=None, projection=None, limit=0):
        queries.append((collection, query))
        return []

    monkeypatch.setattr(G.mongo_store, "find_docs", fake_find_docs)
    G.prime_forward_returns(
        [{"ticker": "AAA", "created_at": datetime(2026, 8, 3)}], horizon=5)

    assert queries, "prime_forward_returns issued no read at all"
    for collection, query in queries:
        assert collection == "price_history"
        assert query.get("source") == "yfinance", (
            f"unpinned read {query!r}: one ticker-date carries several vendor "
            "prints and they disagree by ~20%")
    # the bulk read, then the fallback for a day the empty series cannot cover
    assert len(queries) == 2


def test_the_pin_is_omitted_for_a_single_vendor_ticker(monkeypatch):
    """`dominant_source_for` returns None when there is nothing to choose
    between. Merging `{"source": None}` anyway would filter for documents whose
    source is null — of which there are none — and every read would come back
    empty."""
    monkeypatch.setattr(_returns, "dominant_source_for", lambda t: None)

    queries = []
    monkeypatch.setattr(
        G.mongo_store, "find_docs",
        lambda collection, query, **kw: queries.append(query) or [])
    G.prime_forward_returns(
        [{"ticker": "AAA", "created_at": datetime(2026, 8, 3)}], horizon=5)

    assert queries and all("source" not in q for q in queries)


# ── no Postgres left ────────────────────────────────────────────────────

def test_no_postgres_coupling_remains():
    """The scan `test_soak_instruments_read_mongo.py` ratchets on, pointed at
    this one file. It was 1 before the port (a `pg_connection` import plus two
    `db.execute` calls)."""
    from scripts.gate_zero_pg import scan

    result = scan(REPO, targets=("scripts/gate_ablation.py",))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, result["findings"]
