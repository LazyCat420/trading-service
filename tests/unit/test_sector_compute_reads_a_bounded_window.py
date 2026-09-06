"""The sector compute read 4.37 million rows to use the last 30 of each ticker.

MEASURED 2026-09-06, verification cycle cycle-v3-1788660665, check #6. After
b873016 moved `compute_sector_performance` into a worker thread, the post-boot
run still took 160 s and the container logged NOTHING for 53 s inside it. The
read was `price_history` with an EMPTY filter, joined in Python (`join_rows` is
deliberately not `$lookup`): 4,365,690 rows for 509 tickers, to compute
`pct_change(periods=30)`, `rolling(30)` and then keep only `latest_date`.

Measured on the real function against the real store, one write patched out:

    strategy             wall     loop max gap   output
    thread + full        179.7 s  3.49 s         11 sectors
    thread + 90 d        1.3 s    0.08 s         IDENTICAL to full
    process + full       163.3 s  0.79 s         11 sectors
    process + 90 d       2.4 s    0.47 s         IDENTICAL to full

A process pool frees the loop and keeps the 163 s / 9 GB read. The bound
removes the read. Both tests here are the offline halves of that measurement:
the query carries a lower bound, and the bound changes no output.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.data import sector_aggregator as sa

TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE"]
DAYS = 400  # more than a year of rows per ticker, so a bound has something to cut


def _rows(since=None):
    """A deterministic price_history JOIN ticker_metadata, honouring a date bound."""
    out = []
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=DAYS - 1)  # ends TODAY, so a bound cuts history, not the present
    for d in range(DAYS):
        date = start + timedelta(days=d)
        if since is not None and date < since:
            continue
        for i, t in enumerate(TICKERS):
            close = 100 + i * 10 + (d % 17) * 0.5 - (d % 5)
            out.append((t, date, close, 1_000_000 + d * 1000 + i, "Sector%d" % (i % 2), 1e9 * (i + 1)))
    return out


def _run(capture_query: list, honour_bound: bool):
    docs = []

    def fake_join(left, left_query, *a, **kw):
        capture_query.append(dict(left_query))
        since = (left_query.get("date") or {}).get("$gte") if honour_bound else None
        return _rows(since)

    def fake_upsert(coll, flt, doc):
        d = dict(doc); d.pop("computed_at", None); docs.append(d)

    with patch.object(sa.mongo_query, "join_rows", fake_join), \
         patch.object(sa.mongo_store, "upsert_doc", fake_upsert):
        asyncio.run(sa.compute_sector_performance())
    return sorted(docs, key=lambda d: d["sector"])


class TestTheReadIsBounded:
    def test_the_price_history_query_carries_a_date_lower_bound(self):
        q = []
        _run(q, honour_bound=False)
        assert q, "compute never called join_rows"
        lower = (q[0].get("date") or {}).get("$gte")
        assert lower is not None, f"price_history read with no lower bound: {q[0]!r}"

    def test_the_bound_covers_the_thirty_row_lookback_with_margin(self):
        """pct_change(periods=30) and rolling(30) need 30 ROWS per ticker; a
        trading week is 5 of 7 days, so 30 rows is ~42 calendar days. Anything
        under 60 days risks a short frame after a holiday cluster."""
        q = []
        _run(q, honour_bound=False)
        lower = q[0]["date"]["$gte"]
        span = datetime.now(timezone.utc) - lower
        assert timedelta(days=60) <= span <= timedelta(days=200), span


class TestTheBoundChangesNothing:
    def test_bounded_output_equals_unbounded_output_for_every_sector(self):
        full = _run([], honour_bound=False)
        bounded = _run([], honour_bound=True)
        assert full and bounded
        assert bounded == full
