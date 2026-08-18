"""
Real return/alpha computation for smart-money disclosures.

Replaces two fabricated metrics:
  - congress "estimated return" = 8.0 + (buys - sells) * 0.5   (routers/data.py)
  - 13F fund return            = 0.15 baseline + hardcoded alpha (performance_engine.py)

Neither touched a price. This module runs an event study against real OHLCV.

METHOD
  Entry is the first close on or after the date the trade became PUBLIC, not the
  date it happened. For congress that is disclosure_date (up to 45 days after the
  trade); for a 13F it is the filing date (~45 days after quarter end). Scoring
  from the trade date would credit actors with returns nobody could have acted
  on, which is the single most common way these trackers overstate performance.

  Forward returns at 1m/3m/6m/1y are benchmarked against SPY over the identical
  window. We report ALPHA (excess vs SPY) as the headline number — in a bull
  market raw return makes every actor look brilliant.

  A SELL is scored inverted: selling before a drop is skill, so
  alpha_sell = -(stock_excess). A sell that dodged a 20% underperformance scores
  +20, not -20.

RESULTS ARE MATERIALIZED into two tables so the dashboard and the agent tools
read the SAME numbers — a UI that disagrees with what an agent was told is worse
than no UI at all.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone

import pymongo

from app.db import mongo_query, mongo_store
from app.analytics.amount_parser import parse_amount_range

logger = logging.getLogger(__name__)

BENCHMARK_TICKER = "SPY"

# Forward windows, in calendar days. Approximate month lengths are fine — we take
# the first available close at or after the horizon, so weekends/holidays absorb.
WINDOWS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365}

# Below this many scored trades an actor's average is noise, not signal. We still
# store the row (so the UI can show "insufficient data") but flag it so nothing
# ranks an actor to the top of a leaderboard off three lucky trades.
MIN_SCORED_FOR_RANKING = 5


def _ensure_tables():
    """No DDL: Mongo creates a collection on first write, so the two CREATE
    TABLEs have no counterpart. What DID carry over are the three indexes —
    they are read keys (`_aggregate` scans by actor_type, the tools read by
    ticker/horizon), not schema decoration, and without them those reads become
    collection scans."""
    coll = mongo_store._coll
    for spec in (
        [("actor_type", pymongo.ASCENDING), ("actor_id", pymongo.ASCENDING)],
        [("ticker", pymongo.ASCENDING), ("event_date", pymongo.ASCENDING)],
    ):
        try:
            coll("smart_money_trade_scores").create_index(spec)
        except Exception as e:
            logger.warning("[returns] index %s failed (non-fatal): %s", spec, e)
    try:
        coll("smart_money_trade_scores").create_index("trade_key", unique=True)
    except Exception as e:
        logger.warning("[returns] trade_key index failed (non-fatal): %s", e)
    try:
        coll("smart_money_performance").create_index(
            [("horizon", pymongo.ASCENDING), ("rankable", pymongo.ASCENDING),
             ("avg_alpha", pymongo.ASCENDING)])
        coll("smart_money_performance").create_index(
            [("actor_type", pymongo.ASCENDING), ("actor_id", pymongo.ASCENDING),
             ("horizon", pymongo.ASCENDING)], unique=True)
    except Exception as e:
        logger.warning("[returns] performance indexes failed (non-fatal): %s", e)


_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")   # the SQL's `ticker ~ '^[A-Z]{1,5}$'`


def _as_date(value):
    """price_history.date / disclosure_date as a comparable `date`."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


class _PriceCurve:
    """The LATERAL `first close at/after a date` lookup, done once per ticker.

    The SQL reason for LATERAL was to avoid 300k round trips from a per-trade
    Python loop. That reason survives the port, so the shape does too: every
    ticker's closes are read ONCE into a sorted list, and each of the ten
    lookups per trade is a bisect over it, not a query.
    """

    def __init__(self):
        self._curves: dict[str, tuple[list, list]] = {}

    def load(self, tickers) -> None:
        wanted = sorted({t for t in tickers if t})
        missing = [t for t in wanted if t not in self._curves]
        if not missing:
            return
        # price_history's PK is (ticker, date, source) and the vendors disagree
        # by ~20% on adjusted closes, so a multi-ticker read must pin ONE vendor
        # per ticker or an event study reads vendor switches as price moves.
        # The SQL this replaces was one of the guard's two budgeted unpinned
        # reads in this file; the port fixes it rather than hiding it.
        import pandas as pd

        from app.quant.returns import keep_dominant_source

        raw = mongo_query.find_rows(
            "price_history", {"ticker": {"$in": missing}},
            ["ticker", "date", "close", "source"],
        )
        frame = pd.DataFrame(raw, columns=["ticker", "date", "close", "source"])
        if not frame.empty:
            frame = keep_dominant_source(frame)

        by_ticker: dict[str, list[tuple]] = {t: [] for t in missing}
        if not frame.empty:
            for tkr, d, close in zip(frame["ticker"], frame["date"],
                                     frame["close"]):
                dd = _as_date(d)
                if dd is not None and tkr in by_ticker:
                    by_ticker[tkr].append((dd, close))
        for tkr, pairs in by_ticker.items():
            pairs.sort(key=lambda x: x[0])
            self._curves[tkr] = ([p[0] for p in pairs], [p[1] for p in pairs])

    def at(self, ticker: str, when):
        """The first close at or after `when` for `ticker`.

        Returns None when the curve runs out — that is the LEFT JOIN's NULL,
        and it is what makes _score_rows emit a None return for the window.
        """
        import bisect

        curve = self._curves.get(ticker)
        if not curve or when is None:
            return None
        dates, closes = curve
        i = bisect.bisect_left(dates, when)
        return closes[i] if i < len(dates) else None


def _congress_rows() -> list[tuple]:
    """CONGRESS_SOURCE. The WHERE clause is applied here rather than pushed into
    Mongo for the regex only: `^[A-Z]{1,5}$` is a Python re, and matching it in
    the app keeps one definition of what a ticker is."""
    out = []
    for doc in mongo_query.find_dicts(
        "congress_trades",
        {"disclosure_date": {"$ne": None}},
    ):
        ticker = doc.get("ticker")
        if not isinstance(ticker, str) or not _TICKER_RE.match(ticker):
            continue
        direction = (doc.get("transaction_type") or "").lower()
        if direction not in ("buy", "sell"):
            continue
        if doc.get("disclosure_date") is None:
            continue
        out.append((
            doc.get("id"),                                            # trade_key
            doc.get("bioguide_id") or doc.get("politician"),          # actor_id (COALESCE)
            doc.get("politician"),                                    # actor_name
            ticker,
            direction,
            _as_date(doc.get("disclosure_date")),                     # event_date
            None,                                                     # size_est_usd
            None,                                                     # size_confidence
            doc.get("amount_range"),                                  # size_raw
        ))
    return out


def _quarter_event_date(filing_quarter: str):
    """The SQL's filing-date arithmetic:

        (SUBSTRING(q,1,4) || '-' || LPAD((CAST(SUBSTRING(q,6,1) AS INT)*3), 2, '0')
         || '-01')::DATE + INTERVAL '1 month' + INTERVAL '45 days'

    i.e. the first of the quarter's LAST month, plus one month (= quarter end +
    1 day), plus 45 days. Returns None for a quarter string SQL could not have
    cast either.
    """
    try:
        year = int(filing_quarter[0:4])
        q = int(filing_quarter[5:6])
    except (TypeError, ValueError, IndexError):
        return None
    month = q * 3
    if not 1 <= month <= 12:
        return None
    # `<first of the quarter's last month> + INTERVAL '1 month'` is exactly the
    # first of the following month; + 45 days from there.
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return nxt + timedelta(days=45)


def _fund_rows() -> list[tuple]:
    """FUND_SOURCE: the per-filer quarter diff.

    `LAG(shares) OVER (PARTITION BY cik, ticker ORDER BY filing_quarter)` has no
    Mongo equivalent, so the window is evaluated in Python over the same
    partition and ordering. The `filer_start` CTE (a filer's FIRST quarter is an
    inventory snapshot, not decisions) is the min filing_quarter per cik,
    computed over the SAME `cik NOT LIKE 'yf_%'` population the SQL used.
    """
    holdings = [
        d for d in mongo_query.find_dicts("sec_13f_holdings", {})
        if isinstance(d.get("cik"), str) and not d["cik"].startswith("yf_")
    ]

    # filer_start: MIN(filing_quarter) per cik, over the pre-regex population —
    # the CTE has no ticker filter, so a filer whose only first-quarter holding
    # has a non-matching ticker still anchors on that quarter.
    first_quarter: dict[str, str] = {}
    for d in holdings:
        q = d.get("filing_quarter")
        if q is None:
            continue
        cur = first_quarter.get(d["cik"])
        if cur is None or q < cur:
            first_quarter[d["cik"]] = q

    filer_names = {
        cik: name for cik, name in mongo_query.find_rows(
            "sec_13f_filers", {}, ["cik", "filer_name"])
    }

    # `ranked`: the regex filter applies to the windowed set.
    ranked = [
        d for d in holdings
        if isinstance(d.get("ticker"), str) and _TICKER_RE.match(d["ticker"])
    ]
    partitions: dict[tuple, list[dict]] = {}
    for d in ranked:
        partitions.setdefault((d["cik"], d["ticker"]), []).append(d)

    out = []
    for (cik, ticker), docs in partitions.items():
        docs.sort(key=lambda d: (d.get("filing_quarter") is None,
                                 d.get("filing_quarter")))
        prev_shares = None
        for d in docs:
            q = d.get("filing_quarter")
            shares = d.get("shares")
            if q is not None and q == first_quarter.get(cik):
                direction = "initial"
            elif prev_shares is None:
                direction = "buy"
            elif shares is not None and shares > prev_shares:
                direction = "buy"
            elif shares is not None and shares < prev_shares:
                direction = "sell"
            else:
                direction = "hold"
            prev_shares = shares

            # JOIN sec_13f_filers f ON f.cik = r.cik — an INNER join, so a
            # holding whose filer is unknown produces no row.
            if cik not in filer_names:
                continue

            out.append((
                f"{cik}:{ticker}:{q}",           # trade_key
                cik,                             # actor_id
                filer_names[cik],                # actor_name
                ticker,
                direction,
                _quarter_event_date(q),          # event_date
                d.get("value_usd"),              # size_est_usd
                "reported",                      # size_confidence
                None,                            # size_raw
            ))
    return out


def _with_prices(source_rows: list[tuple]) -> list[tuple]:
    """The entry/forward/benchmark price columns _build_scoring_query appended.

    Emits exactly the tuple _score_rows unpacks:
        base 9 fields, entry_price, then the 4 forward closes, the benchmark
        entry, and the 4 benchmark forward closes — in that order.
    """
    curves = _PriceCurve()
    curves.load([r[3] for r in source_rows] + [BENCHMARK_TICKER])

    out = []
    for r in source_rows:
        ticker, event_date = r[3], r[5]
        if event_date is None:
            # `t.event_date + INTERVAL 'n days'` on a NULL date is NULL, so every
            # LATERAL matched nothing and every price column came back NULL.
            out.append(tuple(r) + (None,) * (2 + 2 * len(WINDOWS)))
            continue
        entry = curves.at(ticker, event_date)
        fwd = [curves.at(ticker, event_date + timedelta(days=d))
               for d in WINDOWS.values()]
        bench_entry = curves.at(BENCHMARK_TICKER, event_date)
        bench_fwd = [curves.at(BENCHMARK_TICKER, event_date + timedelta(days=d))
                     for d in WINDOWS.values()]
        out.append(tuple(r) + (entry,) + tuple(fwd) + (bench_entry,) + tuple(bench_fwd))
    return out


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / old * 100.0


def _score_rows(rows, actor_type: str) -> list[tuple]:
    scored = []
    for r in rows:
        (
            trade_key, actor_id, actor_name, ticker, direction, event_date,
            size_est, size_conf, size_raw, entry_price,
            *prices,
        ) = r

        n = len(WINDOWS)
        fwd = prices[0:n]
        bench_entry = prices[n]
        bench_fwd = prices[n + 1: n + 1 + n]

        if entry_price is None or entry_price == 0:
            continue

        # 'hold' rows exist only so the diff is complete, and 'initial' is a
        # first-sighting snapshot. Neither is a decision, so neither is scored.
        if direction in ("hold", "initial"):
            continue

        # Congress discloses a bracket string; 13F reports an exact dollar value.
        if size_raw is not None:
            size_est, size_conf = parse_amount_range(size_raw)

        rets, alphas = [], []
        for i, _label in enumerate(WINDOWS):
            stock_ret = _pct(fwd[i], entry_price)
            bench_ret = _pct(bench_fwd[i], bench_entry)

            if stock_ret is None:
                rets.append(None)
                alphas.append(None)
                continue

            rets.append(stock_ret)

            if bench_ret is None:
                # No benchmark for this window — we can report the raw return but
                # NOT alpha. Emitting stock_ret as alpha here would silently
                # inflate every score during any SPY gap.
                alphas.append(None)
                continue

            excess = stock_ret - bench_ret
            # Selling ahead of underperformance is a good decision, so invert.
            alphas.append(-excess if direction == "sell" else excess)

        scored.append(
            (
                trade_key, actor_type, str(actor_id), actor_name, ticker, direction,
                event_date, size_est, size_conf, entry_price,
                *rets, *alphas,
            )
        )
    return scored


_SCORE_FIELDS = (
    "trade_key", "actor_type", "actor_id", "actor_name", "ticker", "direction",
    "event_date", "size_est_usd", "size_confidence", "entry_price",
    "ret_1m", "ret_3m", "ret_6m", "ret_1y",
    "alpha_1m", "alpha_3m", "alpha_6m", "alpha_1y",
)


def _persist_scores(scored: list[tuple]):
    if not scored:
        return
    ops = []
    for row in scored:
        doc = dict(zip(_SCORE_FIELDS, row))
        ev = doc.get("event_date")
        # BSON has no date type; store the midnight datetime so range reads on
        # event_date stay comparable.
        if isinstance(ev, date) and not isinstance(ev, datetime):
            doc["event_date"] = datetime(ev.year, ev.month, ev.day,
                                         tzinfo=timezone.utc)
        doc["computed_at"] = datetime.now(timezone.utc)
        ops.append(pymongo.UpdateOne({"trade_key": doc["trade_key"]},
                                     {"$set": doc}, upsert=True))
    # ON CONFLICT (trade_key) DO UPDATE, in one round trip — the executemany
    # this replaces ran 79k statements.
    for i in range(0, len(ops), 1000):
        mongo_store._coll("smart_money_trade_scores").bulk_write(
            ops[i:i + 1000], ordered=False)


def _median(values: list[float]):
    """PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x).

    PERCENTILE_CONT ignores NULLs and interpolates; at p=0.5 that is the mean of
    the two middle values for an even count. Returns None for an empty set,
    which is what the SQL returned.
    """
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _aggregate(actor_type: str):
    """Roll per-trade scores up to an actor leaderboard, one row per horizon.

    The SQL was one INSERT..SELECT..GROUP BY per horizon with PERCENTILE_CONT
    and a FILTER'd COUNT. The grouping keys, the NULL-skipping counts and the
    sums are done in a $group pipeline; the median needs every value, so the
    alphas are collected with $push and reduced in Python.
    """
    for horizon in WINDOWS:
        alpha = f"alpha_{horizon}"
        ret = f"ret_{horizon}"
        pipeline = [
            {"$match": {"actor_type": actor_type}},
            {"$group": {
                "_id": {"actor_type": "$actor_type", "actor_id": "$actor_id"},
                "actor_name": {"$max": "$actor_name"},
                # COUNT(*) counts every row; COUNT(alpha_x) skips NULLs.
                "trade_count": {"$sum": 1},
                "scored_count": {"$sum": {"$cond": [
                    {"$eq": [f"${alpha}", None]}, 0, 1]}},
                "avg_return": {"$avg": f"${ret}"},
                "avg_alpha": {"$avg": f"${alpha}"},
                # COUNT(*) FILTER (WHERE alpha > 0). NOT `{"$gt": [alpha, None]}`:
                # in BSON's total order every number sorts above null, so that
                # would count every scored row, negative alphas included.
                "wins": {"$sum": {"$cond": [
                    {"$and": [{"$ne": [f"${alpha}", None]},
                              {"$gt": [f"${alpha}", 0]}]}, 1, 0]}},
                "alphas": {"$push": f"${alpha}"},
                "total_size_est": {"$sum": "$size_est_usd"},
                # SUM() over all-NULL is NULL in SQL but 0 in $sum; track whether
                # any row had a size at all so the distinction survives.
                "size_present": {"$sum": {"$cond": [
                    {"$eq": ["$size_est_usd", None]}, 0, 1]}},
            }},
        ]

        for doc in mongo_store.aggregate("smart_money_trade_scores", pipeline):
            actor_id = doc["_id"]["actor_id"]
            trade_count = doc.get("trade_count") or 0
            scored_count = doc.get("scored_count") or 0
            wins = doc.get("wins") or 0

            coverage_pct = (round((scored_count / trade_count) * 100, 1)
                            if trade_count else None)
            win_rate = (round((wins / scored_count) * 100, 1)
                        if scored_count else None)

            key = {"actor_type": actor_type, "actor_id": actor_id,
                   "horizon": horizon}
            mongo_store.upsert_doc("smart_money_performance", key, {
                **key,
                "actor_name": doc.get("actor_name"),
                "trade_count": trade_count,
                "scored_count": scored_count,
                "coverage_pct": coverage_pct,
                "avg_return": doc.get("avg_return"),
                "avg_alpha": doc.get("avg_alpha"),
                "median_alpha": _median(doc.get("alphas") or []),
                "win_rate": win_rate,
                "total_size_est": (doc.get("total_size_est")
                                   if doc.get("size_present") else None),
                "rankable": scored_count >= MIN_SCORED_FOR_RANKING,
                "computed_at": datetime.now(timezone.utc),
            })


def compute_all() -> dict:
    """Full recompute of both actor types. Idempotent."""
    _ensure_tables()
    stats = {}

    for actor_type, source in (("congress", _congress_rows), ("fund", _fund_rows)):
        logger.info("[returns] scoring %s...", actor_type)
        rows = _with_prices(source())

        scored = _score_rows(rows, actor_type)
        _persist_scores(scored)
        _aggregate(actor_type)

        stats[actor_type] = {"candidates": len(rows), "scored": len(scored)}
        logger.info("[returns] %s: %s", actor_type, stats[actor_type])

    stats["computed_at"] = datetime.now(timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(compute_all())
