"""The Mongo read seam of the volatility-forecast race.

SPLIT OUT 2026-08-30. The Postgres-to-Mongo port of
`scripts/vol_forecast_race.py` REWROTE `tests/unit/test_vol_forecast_race.py`
in place — a tracked file — deleting the 13 tests that were there and leaving 7
in their place, without saying so. The deletion was not forced: the original 13
pass unmodified against the ported script (verified, `13 passed`), because they
exercise the forecasting maths, which the port did not touch.

Three properties the deleted suite held and the replacement did not, each
demonstrated by a mutant the old file killed and the new one let through:
`_VAR_FLOOR = 0.01 -> 100.0`, the leverage cap `np.minimum(1.0, ...)` dropped,
and the Diebold-Mariano differential sign flipped so every "better" verdict
inverts.

So the original file is back, unchanged, and the port's new tests — which cover
the READ, which the original could not have — live here beside it. Neither
replaces the other.


Three properties, each of which was WRONG in a form this port could plausibly
have shipped, and each pinned by exactly one assertion below.

1. ONE VENDOR. `price_history` is keyed `(ticker, date, source)` and the
   vendors disagree — measured on live SPY on 2026-08-30, an unpinned read
   returns 8,722 rows over 8,453 distinct days and reports the trailing
   250-session annualized vol as **10.05%** against **12.82%** pinned. This
   script exists to score volatility forecasts, so a 22% understatement of the
   thing being forecast is not a rounding error.

2. THE JOIN KEY SURVIVES A STRING. Postgres declared both `price_history.date`
   and `regime_hmm_posteriors.as_of` as `date`, so the two joined for free.
   MongoDB has no date type: `app/db/date_fields.py` keeps writes at naive
   midnight, but its string repair only matches a bare `YYYY-MM-DD`, so the
   posteriors written after the cutover carry `as_of` as the TEXT
   `"2026-08-19 00:00:00"`. Live count on 2026-08-30: 255 BSON dates, 4
   strings, zero overlap. Joining on the raw value drops those four — the
   NEWEST four.

3. TIME ORDER. `load_posteriors` sorts `as_of` ascending, and BSON sorts by
   TYPE first, where String ranks below Date — so the string-dated rows come
   back BEFORE the oldest real one. Live: the returned order begins
   2026-08-19, 2026-08-21, 2026-08-24, 2026-08-28, 2025-08-05. Turnover, the
   equity path and the Newey-West lag all read that list as chronological.

The fixture reproduces all three shapes with no database: two vendors whose
prices disagree, a posterior list in Mongo's type-order with one string
`as_of`, and a stub GARCH so the assertions are about the join and not about
an optimizer.
"""

from __future__ import annotations

import ast
import datetime as dt
import math
import re
from pathlib import Path

import numpy as np
import pytest

import scripts.vol_forecast_race as race

TICKER = "TEST"
DOMINANT = "alpha"          # deeper and fresher — the vendor the pin must keep
OTHER = "beta"              # a second print of the SAME days, at other prices


def _sessions(n: int) -> list[dt.date]:
    """`n` consecutive weekdays."""
    out, d = [], dt.date(2024, 1, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


@pytest.fixture
def store(monkeypatch):
    """A fake `price_history` with two disagreeing vendors, plus the posteriors.

    Returns the day list so the tests can name specific sessions.
    """
    days = _sessions(400)
    rng = np.random.default_rng(20260830)
    # alpha: a normal-looking series. beta: the same days at a different level
    # AND a different shape, so a mixed read is detectable in the returns.
    alpha = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.008, len(days))))
    beta = alpha * 1.20 * np.exp(np.cumsum(rng.normal(0.0, 0.03, len(days))))

    docs = []
    for i, d in enumerate(days):
        stamp = dt.datetime(d.year, d.month, d.day)     # BSON midnight
        docs.append({"ticker": TICKER, "date": stamp, "source": DOMINANT,
                     "close": float(alpha[i])})
        docs.append({"ticker": TICKER, "date": stamp, "source": OTHER,
                     "close": float(beta[i])})

    seen: list[tuple] = []

    def fake_find_docs(collection, query, sort=None, limit=0, **kw):
        seen.append((collection, dict(query)))
        assert collection == "price_history", collection
        rows = [d for d in docs
                if d["ticker"] == query.get("ticker")
                and ("source" not in query or d["source"] == query["source"])]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda r: r[field], reverse=direction < 0)
        return rows

    monkeypatch.setattr("app.db.mongo_store.find_docs", fake_find_docs)
    monkeypatch.setattr("app.quant.returns.dominant_source_for",
                        lambda t: DOMINANT)
    monkeypatch.setattr("app.quant.garch.garch_forecast",
                        lambda hist: {"converged": True,
                                      "predicted_vol_annualized_pct": 15.0})
    return {"days": days, "alpha": alpha, "beta": beta, "calls": seen}


def _posterior(as_of):
    """A well-formed two-state posterior whose `as_of` is whatever is passed."""
    return {
        "as_of": as_of,
        "regime": "CALM",
        "confidence": 80.0,
        "mean_daily_return_pct": 0.05,
        "annualized_vol_pct": 11.0,
        "state_probabilities": {"CALM": 0.9, "STORM": 0.1},
        "state_stats": {
            "CALM": {"mean_daily_return_pct": 0.05, "annualized_vol_pct": 11.0},
            "STORM": {"mean_daily_return_pct": -0.10, "annualized_vol_pct": 30.0},
        },
        "transition_matrix": [[0.95, 0.05], [0.20, 0.80]],
        "stale_sessions": 0,
    }


@pytest.fixture
def posteriors(monkeypatch, store):
    """Posteriors in the order MongoDB actually returns them.

    Four days: three carry a BSON datetime, one carries the post-cutover STRING
    form. Sorted `as_of` ascending, BSON puts the string FIRST — so the list
    handed to the script is 380(str), 300, 301, 302, which is not chronological
    and whose newest member is at index 0.
    """
    days = store["days"]
    picked = [days[300], days[301], days[302], days[380]]
    rows = [_posterior(f"{days[380]} 00:00:00")]                # string as_of
    rows += [_posterior(dt.datetime(d.year, d.month, d.day))
             for d in picked[:3]]
    monkeypatch.setattr("app.quant.regime_grading.load_posteriors",
                        lambda t: list(rows))
    return picked


# ── 1. the vendor pin ────────────────────────────────────────────────

def test_the_read_pins_one_vendor(store, posteriors):
    rows = race.build_series(TICKER)
    assert rows, "empty series — the fixture should produce four paired days"

    days, alpha = store["days"], store["alpha"]
    idx = {d: i for i, d in enumerate(days)}
    for r in rows:
        i = idx[r["as_of"]]
        expect = (math.log(alpha[i + 1]) - math.log(alpha[i])) * 100.0
        assert r["realized_pct"] == pytest.approx(expect, rel=1e-9), (
            f"realized return on {r['as_of']} is not the {DOMINANT} return — "
            "the read mixed vendors"
        )

    # ...and it asked the store for the vendor, by the POSTGRES TABLE NAME.
    assert store["calls"], "price_history was never read"
    collection, query = store["calls"][0]
    assert collection == "price_history"
    assert query.get("source") == DOMINANT, (
        f"the price_history filter carried no vendor: {query}")


def test_a_mixed_read_would_change_the_answer(store, posteriors):
    """The negative control for the test above.

    An assertion about vendor pinning is worth nothing if the fixture's two
    vendors happen to give the same returns. Show that dropping the pin moves
    the number the script scores.
    """
    pinned = race.build_series(TICKER)

    import app.quant.returns as returns
    monkey = pytest.MonkeyPatch()
    monkey.setattr(returns, "dominant_source_for", lambda t: None)  # no pin
    try:
        mixed = race.build_series(TICKER)
    finally:
        monkey.undo()

    assert len(mixed) == len(pinned)
    assert any(m["realized_pct"] != pytest.approx(p["realized_pct"], rel=1e-9)
               for m, p in zip(mixed, pinned)), (
        "the two fixture vendors produce identical returns, so the pinning "
        "assertion above proves nothing — fix the fixture")


# ── 2. the string `as_of` still joins ────────────────────────────────

def test_a_string_as_of_is_not_dropped(store, posteriors):
    """The post-cutover posterior joins to its price row."""
    rows = race.build_series(TICKER)
    got = [r["as_of"] for r in rows]
    assert posteriors[3] in got, (
        f"the string-dated posterior {posteriors[3]} was dropped; "
        f"series covers {got}")
    assert len(rows) == 4, f"expected all four posteriors, got {len(rows)}: {got}"


def test_every_as_of_is_a_calendar_date(store, posteriors):
    """What the SQL returned, and what the printed header and JSON need."""
    for r in race.build_series(TICKER):
        assert isinstance(r["as_of"], dt.date)
        assert not isinstance(r["as_of"], dt.datetime), (
            "a datetime keeps a time component that a calendar day does not "
            "have, and compares unequal to the date the archive returned")


# ── 3. the series is in time order ───────────────────────────────────

def test_the_series_is_chronological(store, posteriors):
    """Turnover, the equity path and the HAC lag all assume this."""
    got = [r["as_of"] for r in race.build_series(TICKER)]
    assert got == sorted(got), (
        f"series is in the order Mongo returned it, not in time order: {got}")
    assert got[0] < got[-1]


def test_as_day_normalises_every_shape_the_store_holds():
    """The helper itself, including the exact string the live store carries."""
    assert race.as_day(dt.datetime(2026, 8, 19, 0, 0)) == dt.date(2026, 8, 19)
    assert race.as_day(dt.date(2026, 8, 19)) == dt.date(2026, 8, 19)
    assert race.as_day("2026-08-19 00:00:00") == dt.date(2026, 8, 19)
    assert race.as_day("2026-08-19") == dt.date(2026, 8, 19)
    assert race.as_day(None) is None
    assert race.as_day("not a date") is None


# ── 4. no Postgres left ──────────────────────────────────────────────

def test_no_postgres_coupling():
    """The porting contract's own grep, plus the import it exists to forbid.

    The regex is case-SENSITIVE on purpose — it is the exact expression the
    port is checked with, and the prose above this line legitimately says
    "Postgres". Prose cannot open a connection; an import can, so the AST scan
    is the half with teeth.
    """
    src = Path(race.__file__).read_text(encoding="utf-8")
    hits = [f"{n}: {line.strip()}" for n, line in enumerate(src.splitlines(), 1)
            if re.search(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres",
                         line)]
    assert not hits, f"Postgres coupling still in the file: {hits}"

    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    banned = {m for m in imported
              if m.startswith(("psycopg", "scripts.migration"))}
    assert not banned, f"still imports the archive layer: {sorted(banned)}"
    # The read has to still BE here, addressed by the Postgres table name and
    # resolved exactly once — `mongo_store._coll` calls `collection_for()`
    # itself, so handing it an already-resolved name resolves twice and, the
    # day renames are switched on, reads a collection nothing writes.
    reads = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id in ("mongo_query", "mongo_store")
        and n.args and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "price_history"
    ]
    assert len(reads) == 1, (
        f"expected exactly one price_history read, found {len(reads)} — if the "
        "read moved, this file needs re-reading (and the combined floor in "
        "test_price_history_one_vendor_guard.py counts it)")
    assert any("_one_vendor(" in ast.unparse(a) or "'source'" in ast.unparse(a)
               or '"source"' in ast.unparse(a) for a in reads[0].args), (
        "the surviving read does not pin a vendor")
