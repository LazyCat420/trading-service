"""`GET /api/v1/cycles` reads `cycle_run_summaries`, not all of `pipeline_events`.

Measured 2026-09-06: `/api/v1/cycles?limit=8&offset=0&include_total=false`
took 1.5-6.8s live. The page was a `$group` over EVERY pipeline_events row
(210k docs — a covered index scan, still 531ms on its own) followed by four
per-cycle fan-outs (tickers, agent rows, trade actions, sometimes distinct
trade tickers), so the cost grew with BOTH the event history and `limit`.

`cycle_run_summaries` already holds one row per finished cycle with
started_at/finished_at/status/tickers/elapsed_ms, indexed. The page is now one
indexed read of that collection plus a FIXED number of batched side reads
(triggers, agent outcomes, trade actions) whose count does not depend on
`limit`. The currently-running cycle has no summary row yet (the row is
upserted at cycle END by log_manager.log_cycle_summary), so it is prepended
from the live pipeline_state singleton on the first page.

Every test here drives the router with a recording fake store so the shape of
the reads — not just the response — is pinned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# The wire shape the client reads (cycleKinds.js / PipelineReplaysPanel.jsx /
# useCycleRuns.js). `summary_status` is additive: the raw store status beside
# the mapped one.
CYCLE_KEYS = {
    "cycle_id", "started_at", "finished_at", "total_ms", "status", "tickers",
    "ticker_count", "agent_count", "outcomes", "actions", "trigger",
    "summary_status",
}

T0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _summary(i: int, status: str = "done", **over) -> dict:
    d = {
        "cycle_id": f"cycle-v3-{i}",
        "started_at": T0 - timedelta(hours=i),
        "finished_at": T0 - timedelta(hours=i) + timedelta(minutes=5),
        "status": status,
        "elapsed_ms": 1000,
        "tickers_final": ["AAPL", "MSFT"],
        "tickers_requested": ["AAPL", "MSFT", "NVDA"],
    }
    d.update(over)
    return d


class _Store:
    """Records every call; serves cycle_run_summaries like Mongo would
    (sort/skip/limit) and canned side data for the other collections."""

    def __init__(self, summaries, telemetry_groups=None, trades=None, triggers=None):
        self.summaries = list(summaries)
        self.telemetry_groups = telemetry_groups or []
        self.trades = trades or []
        self.triggers = triggers or []
        self.calls: list[tuple] = []

    # ── mongo_store surface ─────────────────────────────────────────────
    def find_docs(self, collection, query, sort=None, projection=None,
                  limit=0, skip=0, **kw):
        self.calls.append(("find_docs", collection,
                           {"query": query, "sort": sort, "limit": limit, "skip": skip}))
        if collection == "cycle_run_summaries":
            rows = list(self.summaries)
            if sort:
                k, direction = sort[0]
                rows.sort(key=lambda d: d.get(k) or _MIN, reverse=(direction == -1))
            rows = rows[skip:]
            if limit:
                rows = rows[:limit]
            return [dict(r) for r in rows]
        if collection == "pipeline_events":
            ids = set((query.get("cycle_id") or {}).get("$in") or [])
            return [t for t in self.triggers if t["cycle_id"] in ids]
        if collection == "trade_results":
            ids = set((query.get("cycle_id") or {}).get("$in") or [])
            return [t for t in self.trades if t["cycle_id"] in ids]
        if collection == "pipeline_state":
            return []
        return []

    def aggregate(self, collection, pipeline, **kw):
        self.calls.append(("aggregate", collection, pipeline))
        if collection == "v3_agent_telemetry":
            match = pipeline[0].get("$match", {})
            ids = set((match.get("cycle_id") or {}).get("$in") or [])
            return [g for g in self.telemetry_groups if g["_id"]["cycle_id"] in ids]
        if collection == "pipeline_events" and any("$group" in st for st in pipeline):
            # Emulate the OLD page read so the pre-change code still yields
            # rows here — that is what makes the call-count test honest: on
            # the old code the per-cycle fan-out is visible, not hidden
            # behind an empty page.
            rows = sorted(self.summaries, key=lambda d: d["started_at"], reverse=True)
            skip = next((st["$skip"] for st in pipeline if "$skip" in st), 0)
            limit = next((st["$limit"] for st in pipeline if "$limit" in st), 0)
            rows = rows[skip:skip + limit] if limit else rows[skip:]
            return [{"_id": d["cycle_id"], "started_at": d["started_at"],
                     "finished_at": d["finished_at"], "steps": [], "total_ms": 0}
                    for d in rows]
        return []

    def count_docs(self, collection, query=None):
        self.calls.append(("count_docs", collection, query))
        if collection == "cycle_run_summaries":
            return len(self.summaries)
        return 0

    def distinct_values(self, collection, field, query=None):
        self.calls.append(("distinct_values", collection, {"field": field, "query": query}))
        return []


def _wire(monkeypatch, store: _Store, live: dict | None = None):
    from app.routers import cycle_replay_router as mod
    from app.services.pipeline_service import PipelineService

    for name in ("find_docs", "aggregate", "count_docs", "distinct_values"):
        monkeypatch.setattr(mod.mongo_store, name, getattr(store, name))
    monkeypatch.setattr(PipelineService, "get_current_state",
                        classmethod(lambda cls, summary_only=False: dict(live or {"status": "idle"})))
    return mod


def _summary_reads(store: _Store):
    return [c for c in store.calls if c[0] == "find_docs" and c[1] == "cycle_run_summaries"]


# ── The read itself ──────────────────────────────────────────────────────────

def test_page_never_groups_pipeline_events(monkeypatch):
    """The whole point: no `$group` over pipeline_events on the list path."""
    store = _Store([_summary(i) for i in range(3)])
    mod = _wire(monkeypatch, store)

    mod.list_cycles(limit=8, offset=0, include_total=False)

    grouped = [c for c in store.calls
               if c[0] == "aggregate" and c[1] == "pipeline_events"
               and any("$group" in stage for stage in c[2])]
    assert grouped == [], f"pipeline_events was grouped: {grouped}"
    # And the total is not a DISTINCT over pipeline_events either.
    mod.list_cycles(limit=8, offset=0, include_total=True)
    assert not [c for c in store.calls if c[0] == "distinct_values" and c[1] == "pipeline_events"]


def test_page_is_one_summaries_read_sorted_newest_first_with_limit_and_skip(monkeypatch):
    store = _Store([_summary(i) for i in range(10)])
    mod = _wire(monkeypatch, store)

    out = mod.list_cycles(limit=3, offset=4, include_total=False)

    reads = _summary_reads(store)
    assert len(reads) == 1, reads
    kw = reads[0][2]
    assert kw["sort"] == [("started_at", -1)]
    assert kw["limit"] == 3
    assert kw["skip"] == 4
    # Newest-first: offset 4 of a page sorted by started_at desc is cycle 4..6.
    assert [c["cycle_id"] for c in out["cycles"]] == ["cycle-v3-4", "cycle-v3-5", "cycle-v3-6"]
    assert out["limit"] == 3 and out["offset"] == 4 and out["total"] is None


def test_total_is_the_summaries_count(monkeypatch):
    store = _Store([_summary(i) for i in range(7)])
    mod = _wire(monkeypatch, store)

    out = mod.list_cycles(limit=2, offset=0, include_total=True)

    assert out["total"] == 7
    counts = [c for c in store.calls if c[0] == "count_docs"]
    assert counts == [("count_docs", "cycle_run_summaries", {})]


# ── Status mapping ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("done", "completed"),
    ("stopped", "failed"),
    ("error", "failed"),
    ("cancelled", "failed"),
    ("unknown", "failed"),
])
def test_summary_status_maps_to_the_client_vocabulary(monkeypatch, raw, expected):
    """The client's cycleKinds.js knows 'completed' / 'running' / anything
    else = failed. The store's own status rides along untouched as
    `summary_status` so nothing has to reverse the mapping."""
    store = _Store([_summary(0, status=raw)])
    mod = _wire(monkeypatch, store)

    row = mod.list_cycles(limit=8, offset=0, include_total=False)["cycles"][0]

    assert row["status"] == expected
    assert row["summary_status"] == raw


# ── The live cycle ───────────────────────────────────────────────────────────

def test_live_cycle_without_a_summary_row_is_prepended_as_running(monkeypatch):
    store = _Store([_summary(i) for i in range(3)])
    live = {"status": "analyzing", "cycle_id": "cycle-v3-live",
            "tickers": ["TRMB"], "started_at": "2026-09-06T13:00:00+00:00"}
    mod = _wire(monkeypatch, store, live=live)

    out = mod.list_cycles(limit=3, offset=0, include_total=True)

    first = out["cycles"][0]
    assert first["cycle_id"] == "cycle-v3-live"
    assert first["status"] == "running"
    assert first["summary_status"] is None
    assert first["tickers"] == ["TRMB"] and first["ticker_count"] == 1
    assert first["started_at"] == "2026-09-06T13:00:00+00:00"
    assert first["finished_at"] is None
    # The page itself is still `limit` summary rows; the live row is extra.
    assert [c["cycle_id"] for c in out["cycles"][1:]] == ["cycle-v3-0", "cycle-v3-1", "cycle-v3-2"]
    # ...and the total accounts for it so pagination stays consistent.
    assert out["total"] == 4


def test_live_cycle_with_a_summary_row_is_not_duplicated(monkeypatch):
    """A stale pipeline_state (or the window between the summary upsert and
    the state flip) must not show the same cycle twice."""
    store = _Store([_summary(0, cycle_id="cycle-v3-live", status="done"), _summary(1)])
    live = {"status": "running", "cycle_id": "cycle-v3-live", "tickers": ["X"]}
    mod = _wire(monkeypatch, store, live=live)

    out = mod.list_cycles(limit=8, offset=0, include_total=True)

    ids = [c["cycle_id"] for c in out["cycles"]]
    assert ids.count("cycle-v3-live") == 1
    # The summary row is the source of truth for a cycle that has one.
    assert out["cycles"][0]["status"] == "completed"
    assert out["total"] == 2


def test_live_cycle_is_not_prepended_past_the_first_page(monkeypatch):
    store = _Store([_summary(i) for i in range(5)])
    live = {"status": "running", "cycle_id": "cycle-v3-live", "tickers": []}
    mod = _wire(monkeypatch, store, live=live)

    out = mod.list_cycles(limit=2, offset=2, include_total=False)

    assert [c["cycle_id"] for c in out["cycles"]] == ["cycle-v3-2", "cycle-v3-3"]
    assert all(c["status"] == "completed" for c in out["cycles"])


# ── Fixed query count ────────────────────────────────────────────────────────

def test_store_calls_do_not_grow_with_limit(monkeypatch):
    """Per-cycle fan-out was the second half of the 1.5-6.8s. The side data
    (triggers, agent outcomes, trade actions) is batched over the page's ids,
    so limit=2 and limit=8 cost the same number of round trips."""
    def run(limit):
        store = _Store([_summary(i) for i in range(8)])
        mod = _wire(monkeypatch, store)
        mod.list_cycles(limit=limit, offset=0, include_total=False)
        return store.calls

    two, eight = run(2), run(8)
    assert len(two) == len(eight), (two, eight)
    # Sanity: the calls are the same collections in the same order.
    assert [(c[0], c[1]) for c in two] == [(c[0], c[1]) for c in eight]


def test_side_data_is_batched_over_the_page_ids(monkeypatch):
    store = _Store(
        [_summary(0), _summary(1)],
        telemetry_groups=[
            {"_id": {"cycle_id": "cycle-v3-0", "agent": "bull_agent"}, "outcome": "SUCCESS"},
            {"_id": {"cycle_id": "cycle-v3-0", "agent": "bear_agent"}, "outcome": "AGENT_ERROR"},
            {"_id": {"cycle_id": "cycle-v3-1", "agent": "bull_agent"}, "outcome": "SUCCESS"},
        ],
        trades=[
            {"cycle_id": "cycle-v3-0", "ticker": "AAPL", "action": "BUY", "confidence": 72},
            {"cycle_id": "cycle-v3-1", "ticker": "MSFT", "action": "HOLD", "confidence": 55},
        ],
        triggers=[
            {"cycle_id": "cycle-v3-1", "data": {"source": "watch_desk", "trigger_type": "price_below"}},
        ],
    )
    mod = _wire(monkeypatch, store)

    out = mod.list_cycles(limit=8, offset=0, include_total=False)

    # ONE aggregate over v3_agent_telemetry, matched on the page's ids, sorted
    # the way _cycle_agent_rows sorted (created_at, attempt_no) so `$last`
    # picks the same row per agent the per-cycle reader did.
    aggs = [c for c in store.calls if c[0] == "aggregate" and c[1] == "v3_agent_telemetry"]
    assert len(aggs) == 1
    pipeline = aggs[0][2]
    assert pipeline[0] == {"$match": {"cycle_id": {"$in": ["cycle-v3-0", "cycle-v3-1"]}}}
    assert pipeline[1] == {"$sort": {"created_at": 1, "attempt_no": 1}}
    group = pipeline[2]["$group"]
    assert group["_id"] == {"cycle_id": "$cycle_id", "agent": "$agent_name"}
    assert group["outcome"] == {"$last": "$outcome"}

    # ONE trade_results read over the page's ids.
    trades = [c for c in store.calls if c[0] == "find_docs" and c[1] == "trade_results"]
    assert len(trades) == 1
    assert trades[0][2]["query"] == {"cycle_id": {"$in": ["cycle-v3-0", "cycle-v3-1"]}}

    # No per-cycle readers at all.
    assert not [c for c in store.calls if c[0] == "distinct_values"]

    c0 = next(c for c in out["cycles"] if c["cycle_id"] == "cycle-v3-0")
    c1 = next(c for c in out["cycles"] if c["cycle_id"] == "cycle-v3-1")
    assert c0["agent_count"] == 2
    assert c0["outcomes"] == {"bull_agent": "SUCCESS", "bear_agent": "AGENT_ERROR"}
    assert c0["actions"] == {"AAPL": {"action": "BUY", "confidence": 72}}
    assert c0["trigger"] is None
    assert c1["agent_count"] == 1
    assert c1["actions"] == {"MSFT": {"action": "HOLD", "confidence": 55}}
    assert c1["trigger"] == {"source": "watch_desk", "trigger_type": "price_below"}


# ── Field derivation ─────────────────────────────────────────────────────────

def test_tickers_prefer_final_then_requested_then_traded(monkeypatch):
    store = _Store(
        [
            _summary(0),                                             # tickers_final wins
            _summary(1, tickers_final=[]),                            # falls back to requested
            _summary(2, tickers_final=[], tickers_requested=None),    # falls back to trades
            _summary(3, tickers_final=None, tickers_requested=[]),    # nothing anywhere
        ],
        trades=[{"cycle_id": "cycle-v3-2", "ticker": "NVDA", "action": "BUY", "confidence": 60}],
    )
    mod = _wire(monkeypatch, store)

    rows = {c["cycle_id"]: c for c in mod.list_cycles(limit=8, offset=0, include_total=False)["cycles"]}

    assert rows["cycle-v3-0"]["tickers"] == ["AAPL", "MSFT"]
    assert rows["cycle-v3-1"]["tickers"] == ["AAPL", "MSFT", "NVDA"]
    assert rows["cycle-v3-2"]["tickers"] == ["NVDA"]
    assert rows["cycle-v3-3"]["tickers"] == [] and rows["cycle-v3-3"]["ticker_count"] == 0


def test_total_ms_is_the_larger_of_elapsed_and_span(monkeypatch):
    span_5min = _summary(0, elapsed_ms=1000)                       # span 300000 wins
    elapsed_wins = _summary(1, elapsed_ms=900_000)                 # elapsed wins
    no_finish = _summary(2, elapsed_ms=None, finished_at=None)     # nothing to span
    store = _Store([span_5min, elapsed_wins, no_finish])
    mod = _wire(monkeypatch, store)

    rows = {c["cycle_id"]: c for c in mod.list_cycles(limit=8, offset=0, include_total=False)["cycles"]}

    assert rows["cycle-v3-0"]["total_ms"] == 300_000
    assert rows["cycle-v3-1"]["total_ms"] == 900_000
    assert rows["cycle-v3-2"]["total_ms"] == 0
    assert rows["cycle-v3-2"]["finished_at"] is None


def test_response_keys_are_unchanged_for_summary_and_live_rows(monkeypatch):
    store = _Store([_summary(0)])
    live = {"status": "running", "cycle_id": "cycle-v3-live", "tickers": ["A"]}
    mod = _wire(monkeypatch, store, live=live)

    out = mod.list_cycles(limit=8, offset=0, include_total=True)

    assert set(out.keys()) == {"cycles", "total", "limit", "offset"}
    assert len(out["cycles"]) == 2
    for row in out["cycles"]:
        assert set(row.keys()) == CYCLE_KEYS, row.keys()
    summary_row = out["cycles"][1]
    assert summary_row["started_at"] == T0.isoformat()
    assert summary_row["finished_at"] == (T0 + timedelta(minutes=5)).isoformat()
    assert isinstance(summary_row["started_at"], str)


def test_a_failed_summaries_read_is_an_empty_page_not_a_500(monkeypatch):
    store = _Store([])
    attempted = []

    def boom(collection, *a, **k):
        attempted.append(collection)
        raise RuntimeError("mongo down")

    mod = _wire(monkeypatch, store)
    monkeypatch.setattr(mod.mongo_store, "find_docs", boom)

    out = mod.list_cycles(limit=8, offset=0, include_total=False)
    assert "cycle_run_summaries" in attempted
    assert out["cycles"] == []
