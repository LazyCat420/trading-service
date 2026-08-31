"""`scripts/residual_alpha_report.py` must read Mongo, and must read ONE vendor.

WHY THIS FILE EXISTS
--------------------
This script answers the only question the V3 wave exists to answer — is the
pipeline producing alpha, or re-buying beta — and until 2026-08-30 both of its
reads went through `scripts.migration.pg_connection.get_db`.
`scripts/gate_zero_pg.py` counted **7 couplings** in the pre-port file
(connection_import/get_db_call/execute_call at lines 33-37 and 60-65, plus the
per-desk price `execute_call` at line 78).

That was not merely "it errors". Postgres still answers; it froze on
2026-08-19. The desk read stopped at 1,762 rows against the live collection's
1,960, and — worse for this script specifically — the price archive stopped at
2026-08-19, so every forward window opened in the last fortnight came back
short of `horizon + 1` bars and was dropped by the `len(prices) < sessions`
guard. A shrinking sample, no error, and a verdict printed anyway.

Four things are pinned here, and the first three were RED before the port:

  1. the file has no Postgres coupling at all (7 findings -> 0);

  2. `fetch_decisions` reads `shared_desk` from Mongo, pushes `--since` into
     the query, and decodes `desk_data` — which is JSON **TEXT**, not a
     sub-document. A Mongo filter on `desk_data.trade_decision` matches 0 of
     2,036 desks, and a port that assumed a document reads every post-cutover
     desk as actionless;

  3. `fetch_adv` pins ONE vendor per ticker. `price_history`'s primary key is
     (ticker, date, source) and the vendors carry different adjustment
     conventions — measured on the live store 2026-08-30, AAPL's 90-day ADV is
     $18.09bn on polygon against $16.92bn on yfinance, and the split has GROWN
     since the cutover (a polygon series was backfilled for ADBE, INTC and TSLA
     that the Postgres archive never held; 107 of the 255 tickers decided on
     since 2026-05-01 now carry two vendors). An unpinned `avg(close*volume)`
     averages two different series in whatever proportion each happens to
     cover, which is a whole liquidity tier for names near a boundary;

  4. (regression guard) the forward window is never read here. It goes through
     `app.quant.returns.forward_move_pct`, which carries the same pin. Measured
     on the live store 2026-08-30, the pin changes the +7-session move on 271
     of 688 comparable windows and FLIPS ITS SIGN on 88 — INTC 2026-07-24 reads
     -11.31% unpinned against +9.25% pinned — because an unpinned
     `LIMIT sessions` returns `sessions` ROWS spanning about half as many
     DATES.

The reads are STUBBED, not live: the numbers this script prints move with the
store, and a test asserting today's aggregate fails tomorrow for no defect. The
live cross-check is kept as an explicit probe at the bottom, skipped unless
TRADING_BOT_LIVE_AUDIT=1.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import residual_alpha_report as ra  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

SRC = (REPO / "scripts" / "residual_alpha_report.py").read_text(encoding="utf-8")


# ── 1. the coupling is gone ────────────────────────────────────────────────

def test_the_report_has_no_postgres_coupling():
    """RED before the port: 7 findings — connection_import / get_db_call /
    execute_call at lines 33-37 and 60-65, and execute_call at line 78."""
    result = scan(REPO, targets=("scripts/residual_alpha_report.py",))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, (
        "residual_alpha_report.py still reads Postgres: "
        + "; ".join(f"{f['kind']} at line {f['line']}" for f in result["findings"]))


def test_the_scan_can_still_fail(tmp_path):
    """NEGATIVE CONTROL. A scan that finds nothing because it looked at nothing
    passes the assertion above just as happily."""
    (tmp_path / "report.py").write_text(
        "from scripts.migration.pg_connection import get_db\n"
        "def f():\n"
        "    with get_db() as db:\n"
        "        return db.execute('SELECT close FROM price_history').fetchall()\n",
        encoding="utf-8")
    assert scan(tmp_path, targets=("report.py",))["total"] >= 3


# ── a small store, so the stubs judge the QUERY and not the fixture ────────
#
# The fake applies the real filter and runs the real pipeline against the
# fixture documents rather than handing back a canned answer, because the
# content of trap #3 lives in the query: a stub that ignores `$or` cannot tell
# a vendor-pinned ADV from a blended one, and would stay green for the port
# that dropped the pin.
#
# Only the operators these two functions actually use are modelled, and each
# follows Mongo's documented semantics for a MISSING field. Anything else
# raises rather than being quietly treated as a match.

def _clear_cache():
    """Reset the per-run memo on the forward window.

    `_forward_move` is `lru_cache`d for the length of one report run — the same
    (ticker, day) is decided on repeatedly — so one test's stubbed store would
    otherwise serve the next. `getattr` and not a direct call because this
    fixture must still RUN against a copy of the script that has no such memo:
    a fixture that raises turns every assertion below into a setup error, and a
    setup error cannot tell "the port is missing" from "the harness is wrong".
    """
    memo = getattr(ra, "_forward_move", None)
    if memo is not None and hasattr(memo, "cache_clear"):
        memo.cache_clear()


_MISSING = object()


def _match(doc: dict, query: dict) -> bool:
    for field, cond in query.items():
        if field == "$or":
            if not any(_match(doc, sub) for sub in cond):
                return False
            continue
        val = doc.get(field, _MISSING)
        if not isinstance(cond, dict):
            if val is _MISSING or val != cond:
                return False
            continue
        for op, operand in cond.items():
            if val is _MISSING:
                # No comparison operator matches an absent field, and neither
                # does `$ne: None` — the shape post-cutover documents take.
                return False
            if op == "$ne":
                if val == operand:
                    return False
            elif op == "$gte":
                if val is None or val < operand:
                    return False
            elif op == "$gt":
                if val is None or val <= operand:
                    return False
            elif op == "$lte":
                if val is None or val > operand:
                    return False
            elif op == "$lt":
                if val is None or val >= operand:
                    return False
            elif op == "$in":
                if val not in operand:
                    return False
            else:  # pragma: no cover - a new operator must be taught here
                raise AssertionError(f"filter operator {op!r} not modelled")
    return True


def _project(doc: dict, projection: dict | None) -> dict:
    if not projection:
        return dict(doc)
    keep = [k for k, v in projection.items() if v and k != "_id"]
    return {k: doc[k] for k in keep if k in doc}


def _expr(node, doc):
    """Evaluate an aggregation expression against one document."""
    if isinstance(node, str) and node.startswith("$"):
        return doc.get(node[1:])
    if isinstance(node, dict):
        assert len(node) == 1, f"compound expression {node!r} not modelled"
        (op, args), = node.items()
        if op == "$multiply":
            out = 1
            for a in args:
                out *= _expr(a, doc)
            return out
        raise AssertionError(f"expression operator {op!r} not modelled")
    return node


def _group(docs: list[dict], spec: dict) -> list[dict]:
    keyspec = spec["_id"]
    buckets: dict = {}
    for d in docs:
        if keyspec is None:
            key = None
        elif isinstance(keyspec, dict):
            key = tuple((k, _expr(v, d)) for k, v in keyspec.items())
        else:
            key = _expr(keyspec, d)
        buckets.setdefault(key, []).append(d)

    out = []
    for key, rows in buckets.items():
        if isinstance(keyspec, dict):
            _id = dict(key)
        else:
            _id = key
        row = {"_id": _id}
        for name, acc in spec.items():
            if name == "_id":
                continue
            (op, arg), = acc.items()
            vals = [_expr(arg, r) for r in rows]
            if op == "$sum":
                row[name] = sum(v for v in vals if v is not None)
            elif op == "$max":
                clean = [v for v in vals if v is not None]
                row[name] = max(clean) if clean else None
            elif op == "$min":
                clean = [v for v in vals if v is not None]
                row[name] = min(clean) if clean else None
            elif op == "$avg":
                clean = [v for v in vals if v is not None]
                row[name] = sum(clean) / len(clean) if clean else None
            else:  # pragma: no cover
                raise AssertionError(f"accumulator {op!r} not modelled")
        out.append(row)
    return out


# ── the fixture store ─────────────────────────────────────────────────────
#
# Dates are relative to today so the 90-day ADV window in the script under test
# is exercised rather than side-stepped: a fixture pinned to a literal date
# falls out of the window and the ADV read goes quietly empty, which is
# indistinguishable from the pin being dropped.

_TODAY = date.today()
_DAYS = [datetime.combine(_TODAY - timedelta(days=i), datetime.min.time())
         for i in range(1, 11)]

# AAA carries TWO vendors over the same ten days, both current, and they
# disagree by 2x on close. yfinance is deeper (10 bars vs 4) so it is the
# dominant vendor under the freshness-then-depth rule, and AAA's ADV must be
# yfinance's $100m — not the $140m blend, and not polygon's $200m.
_PRICES = (
    [{"ticker": "AAA", "source": "yfinance", "date": d,
      "close": 100.0, "volume": 1_000_000} for d in _DAYS]
    + [{"ticker": "AAA", "source": "polygon", "date": d,
        "close": 200.0, "volume": 1_000_000} for d in _DAYS[:4]]
    # BBB has a single vendor: `_one_vendor` adds no filter and it must still
    # get an ADV, because a ticker missing from the result is charged the
    # conservative default spread instead of its own.
    + [{"ticker": "BBB", "source": "yfinance", "date": d,
        "close": 10.0, "volume": 2_000_000} for d in _DAYS]
    # CCC's only bars are older than the 90-day window.
    + [{"ticker": "CCC", "source": "yfinance",
        "date": datetime.combine(_TODAY - timedelta(days=200 + i),
                                 datetime.min.time()),
        "close": 5.0, "volume": 1_000} for i in range(5)]
)


@pytest.fixture
def store(monkeypatch):
    """Stub `mongo_store.find_docs` / `aggregate`, recording what was asked."""
    from app.db import date_fields, mongo_store

    seen: dict[str, list] = {}
    data = {"price_history": _PRICES}

    def fake_find_docs(collection, query, sort=None, projection=None,
                       limit=0, session=None):
        assert collection in data, f"unexpected collection {collection!r}"
        seen.setdefault(collection, []).append(query)
        query = date_fields.coerce_filter(collection, query)
        rows = [_project(d, projection) for d in data[collection] if _match(d, query)]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda r: r.get(field), reverse=direction < 0)
        return rows[:limit] if limit else rows

    def fake_aggregate(collection, pipeline, session=None):
        assert collection in data, f"unexpected collection {collection!r}"
        seen.setdefault(collection, []).append(pipeline)
        pipeline = date_fields.coerce_pipeline(collection, pipeline)
        rows = list(data[collection])
        for stage in pipeline:
            (op, spec), = stage.items()
            if op == "$match":
                rows = [d for d in rows if _match(d, spec)]
            elif op == "$group":
                rows = _group(rows, spec)
            else:  # pragma: no cover
                raise AssertionError(f"pipeline stage {op!r} not modelled")
        return rows

    monkeypatch.setattr(mongo_store, "find_docs", fake_find_docs)
    monkeypatch.setattr(mongo_store, "aggregate", fake_aggregate)
    for name in ("insert_docs", "upsert_doc", "update_docs", "delete_docs",
                 "bulk_upsert"):
        monkeypatch.setattr(mongo_store, name, _forbidden(name), raising=False)
    _clear_cache()
    yield seen
    _clear_cache()


def _forbidden(name):
    def _raise(*_a, **_k):
        raise AssertionError(f"this report is read-only; it called {name}()")
    return _raise


# ── 2. the ADV lookup reads Mongo, and reads one vendor ───────────────────

def test_adv_comes_from_the_dominant_vendor_not_a_blend_of_both(store):
    """RED before the port (the lookup was SQL against the frozen archive, and
    the archive answers with July), and RED for a Mongo port that grouped by
    ticker without pinning `source`: AAA would read $140m — the average of a
    $100m series and a $200m one in whatever proportion each vendor happens to
    cover — instead of the dominant vendor's $100m."""
    adv = ra.fetch_adv({"AAA", "BBB", "CCC"})

    assert adv["AAA"] == pytest.approx(100.0 * 1_000_000), (
        "AAA's ADV must come from yfinance alone; "
        f"got {adv['AAA']:,.0f} against yfinance's 100,000,000, polygon's "
        "200,000,000 and the 140,000,000 blend")
    assert adv["BBB"] == pytest.approx(10.0 * 2_000_000)
    assert "CCC" not in adv, (
        "CCC's only bars predate the 90-day window; a ticker with no ADV must "
        "be ABSENT so the caller charges the default spread")


def test_the_adv_window_is_pushed_into_the_query(store):
    """The 90-day bound must reach Mongo. A port that fetched every bar of
    every ticker and trimmed in Python answers correctly here — so this asserts
    the query, which is what keeps the read from scaling with 15.7M rows."""
    ra.fetch_adv({"AAA"})
    pipelines = [p for p in store["price_history"] if isinstance(p, list)]
    adv_pipeline = pipelines[-1]
    match = adv_pipeline[0]["$match"]
    floor = match["date"]["$gt"]
    assert isinstance(floor, datetime)
    assert (datetime.combine(_TODAY, datetime.min.time()) - floor
            == timedelta(days=ra.ADV_LOOKBACK_DAYS))
    assert match["close"] == {"$gt": 0} and match["volume"] == {"$gt": 0}
    # every branch of the $or names a ticker; the dual-vendor one names a source
    branches = match["$or"]
    assert {b["ticker"] for b in branches} == {"AAA"}
    assert branches[0].get("source") == "yfinance"


def test_an_empty_ticker_set_does_not_query_the_store(store):
    assert ra.fetch_adv(set()) == {}
    assert store == {}


# ── 3. the desks: Mongo, JSON TEXT, and one vendor for the window ─────────

_BUY = {"trade_decision": {"action": "BUY", "confidence": 80},
        "cycle_metadata": {"held": False}}
_SELL_HELD = {"trade_decision": {"action": "SELL", "confidence": 90},
              "cycle_metadata": {"held": True}}
_FALLBACK = {"trade_decision": {"action": "BUY",
                                "decision_provenance": "degraded_fallback"}}
_NO_ACTION = {"trade_decision": {"action": "ABSTAIN"}}

_DESKS = [
    # desk_data as a sub-document — the 1,762 backfilled desks …
    ("cyc-1", "AAA", datetime(2026, 8, 18, 10), _BUY),
    # … and as JSON TEXT — every desk written after the cutover
    ("cyc-1", "BBB", datetime(2026, 8, 18, 11), json.dumps(_SELL_HELD)),
    ("cyc-1", "CCC", datetime(2026, 8, 18, 12), json.dumps(_FALLBACK)),
    ("cyc-1", "DDD", datetime(2026, 8, 18, 13), json.dumps(_NO_ACTION)),
    # a desk whose forward window has not closed yet
    ("cyc-2", "EEE", datetime(2026, 8, 18, 14), json.dumps(_BUY)),
]


@pytest.fixture
def desks(monkeypatch):
    from app.db import mongo_query
    import app.quant.returns as returns

    seen: list = []

    def fake_find_rows(collection, query, columns, sort=None, limit=0, session=None):
        seen.append((collection, query, tuple(columns), sort))
        return list(_DESKS)

    monkeypatch.setattr(mongo_query, "find_rows", fake_find_rows)
    monkeypatch.setattr(returns, "forward_move_pct",
                        lambda ticker, start, sessions:
                        None if ticker == "EEE" else 4.0)
    _clear_cache()
    yield seen
    _clear_cache()


def test_the_desks_come_from_mongo_and_json_text_is_decoded(desks):
    """RED before the port (the read was SQL), and RED for a port that treated
    `desk_data` as always-a-document: BBB's decision would be unreadable and
    the desk would drop out of the sample as actionless."""
    out = ra.fetch_decisions("2026-08-01", horizon=7)

    assert desks == [("shared_desk", {"created_at": {"$gte": "2026-08-01"}},
                      ("cycle_id", "ticker", "created_at", "desk_data"),
                      [("created_at", 1)])]
    assert [d["ticker"] for d in out] == ["AAA", "BBB"], (
        "CCC is a degraded fallback, DDD has no tradeable action and EEE's "
        "window has not closed")
    assert [d["action"] for d in out] == ["BUY", "SELL"]
    assert [d["held"] for d in out] == [False, True]
    assert [d["as_of"] for d in out] == [date(2026, 8, 18), date(2026, 8, 18)]
    assert all(d["move_pct"] == 4.0 for d in out)


def test_the_horizon_is_passed_as_sessions_not_as_days(desks, monkeypatch):
    """`sessions = horizon + 1`: an N-session move needs N+1 closes. A port
    that passed `horizon` scores a 6-session move and calls it 7."""
    import app.quant.returns as returns

    calls: list = []
    monkeypatch.setattr(returns, "forward_move_pct",
                        lambda t, s, sessions: calls.append(sessions) or 1.0)
    _clear_cache()
    ra.fetch_decisions("2026-08-01", horizon=7)
    assert set(calls) == {8}


# ── 4. the shape of every store call ─────────────────────────────────────

def _store_calls(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id in {"mongo_store", "mongo_query"}
            and n.args]


def test_every_store_call_names_a_postgres_table_not_a_resolved_collection():
    """`mongo_store._coll` resolves the name exactly once. Handing it
    `collection_for(t)` resolves it twice — a no-op only while renames are off,
    and a silent second collection the day they are on."""
    for node in _store_calls(ast.parse(SRC)):
        arg = node.args[0]
        assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
            f"line {node.lineno}: the collection argument must be a literal "
            "Postgres table name")
        assert "collection_for" not in ast.unparse(arg)


def test_the_only_direct_price_history_read_is_the_pinned_adv_lookup():
    """REGRESSION GUARD for the forward window.

    The window must keep going through `app.quant.returns.forward_move_pct`,
    which owns the vendor pin; inlining a read here is how `outcome_tracker`
    and `challenger` reintroduced the same bug twice. The ADV aggregate is the
    one legitimate direct read, and it pins via `_one_vendor`.
    """
    tree = ast.parse(SRC)
    reads = [n for n in _store_calls(tree)
             if isinstance(n.args[0], ast.Constant)
             and n.args[0].value == "price_history"]
    assert len(reads) == 1, (
        "expected exactly one direct price_history read (the ADV aggregate); "
        f"found {len(reads)} at lines {[n.lineno for n in reads]}")
    assert "_one_vendor(" in ast.unparse(reads[0]), (
        "the ADV aggregate must pin the dominant vendor at the call site — "
        "see tests/unit/test_price_history_one_vendor_guard.py")
    assert "forward_move_pct" in SRC, (
        "the forward window must come from app.quant.returns, which pins the "
        "vendor inside forward_window()")


# ── 5. live cross-check (opt-in) ─────────────────────────────────────────

@pytest.mark.real_mongo
def test_live_the_report_scores_desks_and_prices_their_tickers():
    """A script that compiles, runs and returns `[]` is the failure this port
    exists to catch. Opt-in: `TRADING_BOT_LIVE_AUDIT=1`."""
    import os
    if not os.environ.get("TRADING_BOT_LIVE_AUDIT"):
        pytest.skip("live audit — set TRADING_BOT_LIVE_AUDIT=1")

    decisions = ra.fetch_decisions("2026-05-01", horizon=7)
    assert decisions, "no scoreable desks against the live store"
    assert all(d["action"] in ("BUY", "SELL", "HOLD") for d in decisions)
    assert all(isinstance(d["as_of"], date) for d in decisions)

    adv = ra.fetch_adv({d["ticker"] for d in decisions})
    assert adv, "the ADV lookup answered empty against the live store"
    assert all(v > 0 for v in adv.values())
