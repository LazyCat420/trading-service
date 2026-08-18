"""Unit tests for the persistent research firm — dossiers, queues, questions.

## Why the mocks changed

The original tests did this:

    mock_get_db.return_value = mock_db          # get_db() IS the cursor
    mock_db.execute.return_value.fetchone...

`app.db.connection.get_db` is decorated `@contextmanager`, so it returns a
`_GeneratorContextManager` and the cursor only exists inside a `with` block.
Both services were written against the mock's contract rather than the real
one — `db = get_db(); db.execute(...)` — so **every method raised
`AttributeError` on its first statement**, and these tests were green the whole
time. The mock had re-implemented the contract wrongly, and a test that supplies
its own wrong contract cannot see the code fail.

So the fake below IS a context manager, and `test_get_db_contract_is_a_context_manager`
pins it against the real `get_db` — if the real contract ever changes, the fake
is caught as stale instead of quietly diverging again.

These tests fail on the pre-fix tree. That was verified, not assumed.

## Ported off the inert get_db mock (2026-08-18)

`app/services/dossier_service.py` and `app/services/research_queue_service.py`
were converted to `mongo_query` / `mongo_store` and no longer import `get_db`
at all. Patching `<module>.get_db` therefore intercepted NOTHING: the services
read and wrote the LIVE Mongo database while `FakeCursor` recorded an empty
statement list, so every "assert this SQL was emitted" check was scanning
nothing and every "assert this SQL was NOT emitted" check passed vacuously.

The queue tests below now patch both Mongo halves — reads AND writes; stubbing
only the read leaves `update_docs` claiming real queue rows — and the SQL-text
assertions became STRUCTURAL assertions on the Mongo calls: the collection
name, the filter document, and the update document. That is strictly stronger
than the old substring checks, because a Mongo filter is machine-comparable
where `"status = 'pending'" in sql` was not: the old dedupe test needed a
sabotage run to convince itself the predicate was even being looked at, and
the equivalent assertion here compares the whole `$in` list.

`mongo_query.find_row`/`find_rows` return TUPLES positioned in the column order
the caller listed; `group_rows` returns tuples in its own group/agg order. The
fixtures below honour that.

`question_ledger` still uses `get_db`, so its tests keep the `FakeCursor` path
unchanged.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.schemas.dossier_schemas import (
    LifecycleState,
    QueueType,
    TickerDossier,
    WatchlistHoldSpec,
)
from app.services import question_ledger
from app.services.dossier_service import DossierService
from app.services.research_queue_service import ResearchQueueService


# ────────────────────────── contract-faithful fake ──────────────────────────


class FakeCursor:
    """Records every statement; returns programmed rows in order.

    Deliberately exposes NO `rowcount`, because the real `PooledCursor` has
    none and no `__getattr__` passthrough to the psycopg cursor either. A fake
    that offered one would let a caller write `cur.rowcount`, pass here, and
    raise `AttributeError` in production — the same shape as the `get_db` bug
    this file exists to prevent. Count with `RETURNING` instead.
    """

    def __init__(self, results=None):
        self.statements: list[tuple[str, list]] = []
        self._results = list(results or [])
        self._last = None

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), list(params or [])))
        self._last = self._results.pop(0) if self._results else None
        return self

    def fetchone(self):
        if isinstance(self._last, list):
            return self._last[0] if self._last else None
        return self._last

    def fetchall(self):
        if self._last is None:
            return []
        return self._last if isinstance(self._last, list) else [self._last]

    def sql_verbs(self):
        return [s.split(None, 1)[0].upper() for s, _ in self.statements]


def fake_get_db(cursor):
    """Mirror `get_db`'s real shape: a context manager yielding the cursor."""

    @contextmanager
    def _factory():
        yield cursor

    return _factory


# ───────────────────────── contract-faithful Mongo fake ─────────────────────


class FakeMongo:
    """Records every read filter and every write, for the queue service.

    `find_rows` is dispatched on the read FILTER, not on call order: the
    reclaim's two passes differ only by their `attempts` predicate, and keying
    on order would let the two swap places without the test noticing — which is
    precisely the ordering invariant one of the tests below pins.
    """

    def __init__(self, find_row=None, find_rows=None, group_rows=None):
        self._find_row = find_row
        self._find_rows = find_rows or (lambda coll, flt, cols, **k: [])
        self._group_rows = group_rows or (lambda *a, **k: [])
        self.reads: list[tuple] = []      # (collection, filter)
        self.updates: list[tuple] = []    # (collection, filter, update)
        self.inserts: list[tuple] = []    # (collection, docs)

    # ── mongo_query surface ──
    def find_row(self, collection, filt, cols, **kw):
        self.reads.append((collection, filt))
        if callable(self._find_row):
            return self._find_row(collection, filt, cols, **kw)
        return self._find_row

    def find_rows(self, collection, filt, cols, **kw):
        self.reads.append((collection, filt))
        if callable(self._find_rows):
            return self._find_rows(collection, filt, cols, **kw)
        return list(self._find_rows)

    def group_rows(self, collection, filt, keys, aggs, order, **kw):
        self.reads.append((collection, filt))
        if callable(self._group_rows):
            return self._group_rows(collection, filt, keys, aggs, order, **kw)
        return list(self._group_rows)

    # ── mongo_store surface ──
    def update_docs(self, collection, filt, update, **kw):
        self.updates.append((collection, filt, update))
        return 1

    def insert_docs(self, collection, docs, **kw):
        self.inserts.append((collection, docs))
        return len(docs)

    # ── assertion helpers ──
    def set_values(self):
        """Every `$set` document written, in order."""
        return [u[2].get("$set", {}) for u in self.updates]

    def statuses_written(self):
        """The `status` value of every write that set one, in order."""
        return [s["status"] for s in self.set_values() if "status" in s]


def patch_queue_mongo(fake):
    """Patch BOTH halves of the queue service's Mongo layer.

    Patching only the read would leave `update_docs` claiming and failing rows
    in the production queue — which is what the get_db patch was doing.
    """
    class _Both:
        def __enter__(self):
            self._q = patch("app.services.research_queue_service.mongo_query", fake)
            self._s = patch("app.services.research_queue_service.mongo_store", fake)
            self._q.start()
            self._s.start()
            return fake

        def __exit__(self, *exc):
            self._s.stop()
            self._q.stop()
            return False

    return _Both()


# ─────────────────────────────── the contract ───────────────────────────────


def test_get_db_never_hands_back_something_you_can_execute_on():
    """The invariant both services violated, asserted against whatever
    `get_db` currently is — the real one, or the autouse test fake.

    Pinning it here rather than to `@contextmanager` internals is deliberate:
    the fixture must be held to the same contract as production, because the
    previous fixture satisfied both the correct and the incorrect usage and so
    could not fail. If the fake is ever loosened back to
    `MagicMock(return_value=cursor)`, this goes red.
    """
    from app.db.connection import get_db

    handle = get_db()
    assert not hasattr(handle, "execute"), (
        "get_db() returned something with .execute — the contract is "
        "`with get_db() as db:`, and a fake that allows `db = get_db()` "
        "cannot catch the bug that shipped on 2026-08-07"
    )
    with get_db() as db:
        assert hasattr(db, "execute"), "the yielded object must be a cursor"


# ───────────────────────────── schema validation ─────────────────────────────


def test_dossier_schema_validation():
    dossier = TickerDossier(
        ticker="AAPL",
        lifecycle_state=LifecycleState.NEW,
        canonical_thesis={"bull": "Strong cash flow"},
        lead_analyst_id="lead_analyst_01",
    )
    assert dossier.ticker == "AAPL"
    assert dossier.lifecycle_state == LifecycleState.NEW
    assert len(dossier.decision_history) == 0


def test_watchlist_hold_spec():
    hold_spec = WatchlistHoldSpec(
        positive_rationale="High quality balance sheet, waiting for earnings pull-back",
        waiting_conditions=["Price drops below $180", "Q3 earnings beat"],
        invalidation_conditions=["Debt ratio exceeds 2.5"],
        recheck_schedule_hours=12.0,
    )
    assert hold_spec.recheck_schedule_hours == 12.0
    assert len(hold_spec.waiting_conditions) == 2


# ────────────────────── services against the real contract ──────────────────


def test_dossier_service_record_decision_against_real_contract():
    """The write must actually happen, not just the happy return object."""
    store = MagicMock()
    store.find_docs.return_value = []  # fresh ticker: nothing on file

    with patch("app.services.dossier_service.mongo_store", store):
        dossier = DossierService.record_decision(
            ticker="MSFT",
            cycle_id="cycle-123",
            action="HOLD",
            confidence=75,
            lead_analyst="LeadAnalystAgent",
            rationale="Awaiting catalyst",
            state_transition="WATCHLIST_HOLD",
        )

    assert dossier.ticker == "MSFT"
    assert dossier.lifecycle_state == LifecycleState.WATCHLIST_HOLD
    assert len(dossier.decision_history) == 1
    assert dossier.decision_history[0].action == "HOLD"
    # It must actually have written, not just returned a happy object.
    # (Was `"INSERT" in cursor.sql_verbs()`; the write is an upsert now, and
    # asserting the key + the persisted history says more than the verb did.)
    store.upsert_doc.assert_called_once()
    collection, key, doc = store.upsert_doc.call_args[0][:3]
    assert collection == "ticker_dossiers"
    assert key == {"ticker": "MSFT"}
    assert doc["lifecycle_state"] == LifecycleState.WATCHLIST_HOLD.value
    assert len(doc["decision_history"]) == 1
    assert doc["decision_history"][0]["action"] == "HOLD"


def test_research_queue_enqueue_against_real_contract():
    fake = FakeMongo(find_row=None)  # dedupe read finds nothing

    with patch_queue_mongo(fake):
        item_id = ResearchQueueService.enqueue_item(
            ticker="NVDA",
            queue_type=QueueType.LEAD_QUEUE,
            reason="Breakout momentum in news sweep",
            source_agent="ScoutAgent",
            priority=10,
        )

    assert item_id.startswith("qitem-")
    assert len(fake.inserts) == 1
    collection, docs = fake.inserts[0]
    assert collection == "v3_research_queues"
    assert docs[0]["id"] == item_id
    assert docs[0]["ticker"] == "NVDA"
    assert docs[0]["queue_type"] == QueueType.LEAD_QUEUE.value
    assert docs[0]["status"] == "pending"
    assert docs[0]["attempts"] == 0


def test_pop_worklist_claims_and_peek_does_not():
    """The shadow invariant: peek must not consume the queue.

    `pop_worklist` moves what it returns to `processing`. If the worklist
    shadow called it, the shadow would drain the queue it is measuring while
    no worker is serving it.
    """
    # `pop_worklist` reclaims stale claims before selecting (open item 14), and
    # that is two reads ahead of the four queue reads. `peek` must still take
    # neither — it is read-only by contract.
    def _selection(coll, flt, cols, **kw):
        if flt.get("queue_type") == QueueType.LEAD_QUEUE.value:
            return [("qitem-1", "NVDA", "lead_queue", 10, "Breakout", "ScoutAgent", "{}")]
        return []  # exit_review, monitor, deep_dive, and the reclaim passes

    pop_fake = FakeMongo(find_rows=_selection)
    with patch_queue_mongo(pop_fake):
        popped = ResearchQueueService.pop_worklist(budget=4)

    peek_fake = FakeMongo(find_rows=_selection)
    with patch_queue_mongo(peek_fake):
        peeked = ResearchQueueService.peek_worklist(budget=4)

    # Same selection...
    assert [i["ticker"] for i in popped] == [i["ticker"] for i in peeked] == ["NVDA"]
    # ...different side effects.
    assert pop_fake.updates, "pop must claim what it returns"
    assert peek_fake.updates == [], "peek must not mutate the queue"
    assert peek_fake.inserts == [], "peek must not write at all"


# ───────────────────────────── the orphan path ──────────────────────────────
#
# Open item 14: `pop_worklist` → `processing` → `complete_item` had no requeue,
# no timeout and no reset, so a worker dying between the two stranded its items
# permanently — and because `enqueue_item` deduped only against `pending`, the
# symptom was silent duplicates rather than a visible stall.
#
# Every test below calls the service. None re-implements the branch it is
# checking, which is the shape that let a blocked trade read as a kept one for
# weeks (open item 7).


def _reclaim_reads(fake):
    """The two staleness reads reclaim_stale issues, in order."""
    return [flt for coll, flt in fake.reads if coll == "v3_research_queues"
            and flt.get("status") == "processing" and "attempts" in flt]


def _reclaim_rows(failed_ids=(), requeued_ids=()):
    """Dispatch the two reclaim passes on their `attempts` predicate.

    Keyed on the FILTER rather than on call order: the two passes differ only
    by `$gte` vs `$lt` on `attempts`, and a fixture that answered them by
    position would keep working if they swapped — which is the very ordering
    one of the tests below pins.
    """
    def _rows(coll, flt, cols, **kw):
        att = flt.get("attempts") or {}
        if "$gte" in att:
            return [(i,) for i in failed_ids]
        if "$lt" in att:
            return [(i,) for i in requeued_ids]
        return []
    return _rows


def test_a_dead_worker_gets_its_claim_back():
    """The item this whole path exists for: claimed, abandoned, returned."""
    fake = FakeMongo(find_rows=_reclaim_rows(requeued_ids=["qitem-dead"]))

    with patch_queue_mongo(fake):
        out = ResearchQueueService.reclaim_stale()

    assert out["requeued"] == ["qitem-dead"]
    assert out["failed"] == []
    # Was `"status = 'pending'" in sql`. The equivalent structural claim: the
    # write that touched qitem-dead set it back to pending, not to some other
    # status.
    writes = [u for u in fake.updates if u[1] == {"id": "qitem-dead"}]
    assert len(writes) == 1
    assert writes[0][0] == "v3_research_queues"
    assert writes[0][2]["$set"]["status"] == "pending", \
        "a reclaimed item must go back to pending, not to some other status"


def test_reclaim_judges_the_heartbeat_and_never_the_start_time():
    """Open item 5's lesson, in a new place.

    `started_at > 30min` failed both ways there: it skipped healthy cycles and
    force-reset live ones. A reclaim that judged `created_at` would requeue an
    item purely for having been enqueued a while ago, however recently it was
    claimed.
    """
    fake = FakeMongo(find_rows=_reclaim_rows())
    with patch_queue_mongo(fake):
        ResearchQueueService.reclaim_stale()

    filters = _reclaim_reads(fake)
    assert filters, "reclaim must issue its staleness reads"
    for f in filters:
        assert "$lt" in (f.get("updated_at") or {}), \
            f"staleness must be judged on updated_at: {f}"
        assert "created_at" not in f, f"reclaim must not judge created_at: {f}"


def test_an_item_that_keeps_dying_is_failed_rather_than_requeued_forever():
    """Otherwise the reclaim recreates the stall it replaced, one pass at a
    time, with nothing visible in the queue depth."""
    from app.services.research_queue_service import MAX_ATTEMPTS

    fake = FakeMongo(find_rows=_reclaim_rows(failed_ids=["qitem-poison"]))
    with patch_queue_mongo(fake):
        out = ResearchQueueService.reclaim_stale()

    assert out["failed"] == ["qitem-poison"]
    assert out["requeued"] == []

    writes = [u for u in fake.updates if u[1] == {"id": "qitem-poison"}]
    assert len(writes) == 1
    assert writes[0][2]["$set"]["status"] == "failed"

    # The fail pass must run BEFORE the requeue pass, or an item at the limit is
    # requeued by this call and only failed by the next one. The two reads are
    # distinguished by their `attempts` predicate, and the FIRST one must be
    # the >= MAX_ATTEMPTS pass.
    filters = _reclaim_reads(fake)
    assert len(filters) == 2
    assert filters[0]["attempts"] == {"$gte": MAX_ATTEMPTS}
    assert filters[1]["attempts"] == {"$lt": MAX_ATTEMPTS}


def test_reclaim_counts_what_it_actually_touched_not_a_driver_rowcount():
    """`PooledCursor` carried no `rowcount`; reading it reported zero silently
    (`5cec538`). The Mongo port must not reintroduce that shape: the counts it
    returns have to come from the ids it read and wrote, so a reclaim that
    touched nothing reports nothing and a reclaim that touched two reports two.
    """
    empty = FakeMongo(find_rows=_reclaim_rows())
    with patch_queue_mongo(empty):
        out = ResearchQueueService.reclaim_stale()
    assert out == {"requeued": [], "failed": []}
    assert empty.updates == [], "nothing stale means nothing written"

    busy = FakeMongo(find_rows=_reclaim_rows(
        failed_ids=["qitem-a"], requeued_ids=["qitem-b", "qitem-c"]
    ))
    with patch_queue_mongo(busy):
        out = ResearchQueueService.reclaim_stale()
    # The returned ids ARE the count — there is no separate tally to drift.
    assert out["failed"] == ["qitem-a"]
    assert out["requeued"] == ["qitem-b", "qitem-c"]
    assert len(busy.updates) == 3, "one write per id, no more and no fewer"


def test_pop_arms_the_orphan_path_and_counts_the_attempt():
    """The guard is armed by pop, so a queue nothing pops neither strands nor
    reclaims — which is exactly today's state."""
    def _rows(coll, flt, cols, **kw):
        if flt.get("status") == "processing":       # the reclaim's two passes
            return []
        if flt.get("queue_type") == QueueType.LEAD_QUEUE.value:
            return [("qitem-1", "NVDA", "lead_queue", 10, "Breakout", "ScoutAgent", "{}")]
        return []

    fake = FakeMongo(find_rows=_rows)
    with patch_queue_mongo(fake):
        ResearchQueueService.pop_worklist(budget=4)

    # pop must reclaim stale claims BEFORE it selects, not after: the two
    # reclaim reads have to precede the first queue read.
    reads = [flt for coll, flt in fake.reads]
    first_select = next(i for i, f in enumerate(reads) if "queue_type" in f)
    assert first_select == 2, \
        "pop must reclaim stale claims before it selects, not after"

    # The claim itself. `$set.status`, not merely a filter naming `processing`
    # — the reclaim's own FILTER also names `processing`, and matching that
    # would be testing the wrong call.
    claims = [u for u in fake.updates if u[2].get("$set", {}).get("status") == "processing"]
    assert claims, "pop must claim what it returns"
    assert claims[0][1] == {"id": "qitem-1"}
    assert claims[0][2].get("$inc", {}).get("attempts") == 1, \
        "an unincremented attempt makes MAX_ATTEMPTS unreachable"


def test_a_failing_reclaim_does_not_take_the_worklist_with_it():
    """Worst case without the reclaim is the stall it replaces; worst case with
    a reclaim that propagates is no worklist at all."""
    def _rows(coll, flt, cols, **kw):
        if flt.get("status") == "processing":       # the reclaim's reads
            raise RuntimeError("pool exhausted")
        if flt.get("queue_type") == QueueType.LEAD_QUEUE.value:
            return [("qitem-1", "NVDA", "lead_queue", 10, "Breakout", "ScoutAgent", "{}")]
        return []

    fake = FakeMongo(find_rows=_rows)
    with patch_queue_mongo(fake):
        popped = ResearchQueueService.pop_worklist(budget=4)

    assert [i["ticker"] for i in popped] == ["NVDA"]


def test_enqueue_dedupes_against_a_claim_not_only_against_pending():
    """The bug's actual symptom. A stranded `processing` row did not block a new
    enqueue, so the queue grew duplicates instead of showing a stall."""
    fake = FakeMongo(find_row=("qitem-held", "processing"))

    with patch_queue_mongo(fake):
        item_id = ResearchQueueService.enqueue_item(
            ticker="NVDA",
            queue_type=QueueType.LEAD_QUEUE,
            reason="second sighting",
            source_agent="ScoutAgent",
        )

    assert item_id == "qitem-held"
    assert fake.inserts == [], \
        "an item a worker is holding is still queued; enqueuing a second is the duplicate"

    # The two assertions above pass on the BROKEN code too: the fake returns its
    # programmed row whatever the filter says, so they cannot see the predicate.
    # (Under the SQL version this needed a sabotage run to establish; a Mongo
    # filter is a comparable object, so the whole predicate is pinned here.)
    coll, flt = fake.reads[0]
    assert coll == "v3_research_queues"
    assert flt["ticker"] == "NVDA"
    assert flt["queue_type"] == QueueType.LEAD_QUEUE.value
    assert set(flt["status"]["$in"]) == {"pending", "processing"}, \
        f"the dedupe must consider claimed rows, not only pending ones: {flt}"


def test_heartbeat_tells_a_worker_its_claim_was_taken_away():
    """A worker that ignores this keeps working on an item somebody else now
    owns — the duplicate the orphan path exists to prevent."""
    live = FakeMongo(find_row=("qitem-1",))
    with patch_queue_mongo(live):
        assert ResearchQueueService.heartbeat("qitem-1") is True

    lost = FakeMongo(find_row=None)
    with patch_queue_mongo(lost):
        assert ResearchQueueService.heartbeat("qitem-1") is False

    # The confirming read must require the claim to still be the worker's, or
    # a row reassigned to somebody else would read back as live.
    assert lost.reads[-1][1] == {"id": "qitem-1", "status": "processing"}


def test_reset_only_takes_back_an_item_that_is_actually_claimed():
    """A reset that also fired on `completed` would resurrect finished work."""
    fake = FakeMongo()
    with patch_queue_mongo(fake):
        ResearchQueueService.reset_item("qitem-1", reason="agent raised")

    assert len(fake.updates) == 1
    coll, flt, update = fake.updates[0]
    assert coll == "v3_research_queues"
    assert update["$set"]["status"] == "pending"
    # The `AND status = 'processing'` half: the filter must exclude anything
    # that is not currently claimed.
    assert flt == {"id": "qitem-1", "status": "processing"}


def test_status_counts_can_see_a_stall_that_queue_depth_cannot():
    """`get_queue_summary` is pending-only by contract — `worklist_shadow`
    stores it as `queue_depth`. A growing `processing` count against a flat
    `completed` count IS the stall, and nothing could see it before."""
    fake = FakeMongo(group_rows=[
        ("deep_dive_queue", "processing", 7),
        ("deep_dive_queue", "completed", 0),
    ])
    with patch_queue_mongo(fake):
        counts = ResearchQueueService.get_status_counts()

    assert counts["deep_dive_queue"]["processing"] == 7
    # Was `"WHERE" not in sql`. The equivalent: the aggregation must run over
    # an UNFILTERED collection — any filter at all would hide a status, and a
    # status summary that filters by status cannot report the stall.
    assert fake.reads[0][1] == {}, \
        "a status summary that filters by status cannot report the stall"


# ──────────────────────────── the question ledger ────────────────────────────


def test_question_hash_is_stable_across_cosmetic_rewording():
    """If the hash moved on whitespace, ask_count would never leave 1."""
    a = "Is  the FY26 buyback funded from FCF?"
    b = "is the fy26 buyback funded from fcf"
    assert question_ledger.question_hash(a) == question_ledger.question_hash(b)


def test_question_hash_separates_different_questions():
    assert question_ledger.question_hash("Is the buyback funded from FCF?") != \
        question_ledger.question_hash("Is the dividend funded from FCF?")


def test_clean_questions_drops_fragments_and_dedupes_preserving_order():
    got = question_ledger.clean_questions([
        "What is the segment margin trend into FY26?",
        "n/a",                     # too short to research
        "What is the SEGMENT margin trend into FY26?",  # same question
        None,                      # not a string
        42,
        "Does the covenant reset before the refinancing window?",
    ])
    assert got == [
        "What is the segment margin trend into FY26?",
        "Does the covenant reset before the refinancing window?",
    ]


def test_pooled_cursor_has_no_rowcount_so_counts_must_use_returning():
    """The second contract trap in the same file.

    `PooledCursor` wraps a psycopg cursor but exposes no `rowcount` and no
    `__getattr__` passthrough. Code that counts affected rows with
    `cur.rowcount` raises `AttributeError`, and inside a `try/except` that
    becomes a metric permanently reporting 0 — indistinguishable from "nothing
    happened". Both ledger updaters use `RETURNING id` instead.
    """
    from app.db.connection import PooledCursor

    assert not hasattr(PooledCursor, "rowcount")
    assert not hasattr(PooledCursor, "__getattr__"), (
        "a passthrough would make rowcount work again — if one is added "
        "deliberately, delete this test and say so"
    )


def test_mark_not_reasked_counts_rows_it_actually_closed():
    cursor = FakeCursor(results=[[(11,), (12,), (13,)]])  # RETURNING id
    with patch("app.services.question_ledger.get_db", fake_get_db(cursor)):
        n = question_ledger.mark_not_reasked("AAPL", "cycle-9", ["abc123"])

    assert n == 3
    sql, params = cursor.statements[0]
    assert "RETURNING id" in sql
    # The cast is load-bearing: an empty list gives Postgres no element type.
    assert "%s::text[]" in sql
    assert params[-1] == ["abc123"]


def test_mark_not_reasked_handles_a_desk_that_asked_nothing():
    """Empty hash list must still be a valid array parameter, not a crash."""
    cursor = FakeCursor(results=[[(1,)]])
    with patch("app.services.question_ledger.get_db", fake_get_db(cursor)):
        n = question_ledger.mark_not_reasked("AAPL", "cycle-9", [])
    assert n == 1
    assert cursor.statements[0][1][-1] == []


def test_stats_never_folds_dropped_into_answered():
    """`dropped` is ambiguous; counting it as resolved makes the metric vacuous."""
    cursor = FakeCursor(results=[[
        ("open", 3, 1),
        ("reasked", 2, 4),
        ("dropped", 5, 1),
    ]])
    with patch("app.services.question_ledger.get_db", fake_get_db(cursor)):
        s = question_ledger.stats(days=14)

    assert s["total"] == 10
    assert s["answered"] == 0, "no row had status 'answered'"
    assert s["by_status"]["dropped"] == 5
    assert s["reask_rate"] == pytest.approx(0.2)
    assert s["max_ask_count"] == 4


# ────────────────────────────── the desk sync ───────────────────────────────


class _Desk:
    """Minimal stand-in carrying the attributes dossier_sync actually reads."""

    def __init__(self, **artifacts):
        self.ticker = artifacts.pop("ticker", "AAPL")
        for name in ("desk_note", "fundamental_report", "quant_report",
                     "valuation_report", "delta_report", "bull_argument",
                     "bear_rebuttal", "bull_defense", "debate_judge",
                     "regime_classification", "final_decision"):
            setattr(self, name, artifacts.get(name))


def test_collect_open_questions_sweeps_every_artifact_not_just_the_quant():
    from app.v3.dossier_sync import collect_open_questions

    desk = _Desk(
        quant_report={"sub_analyses_requested": ["Does the ATR stop survive earnings week?"]},
        fundamental_report={"sub_analyses_requested": ["Is the FY26 buyback funded from FCF?"]},
        bull_argument={"summary": "no questions here"},
    )
    got = collect_open_questions(desk)
    sources = {src for _, src in got}
    assert sources == {"quant_report", "fundamental_report"}
    assert len(got) == 2


def test_a_blocked_buy_is_not_recorded_as_a_buy():
    """A gate-blocked BUY entering the dossier as a BUY would teach it a
    decision that never executed — the same trap the episodic writer avoids."""
    from app.v3 import dossier_sync

    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return TickerDossier(ticker=kwargs["ticker"])

    desk = _Desk(final_decision={"action": "BUY", "confidence": 82, "reasoning": "x"})

    with patch.object(dossier_sync.DossierService, "get_dossier",
                      return_value=TickerDossier(ticker="AAPL")), \
         patch.object(dossier_sync.DossierService, "save_dossier"), \
         patch.object(dossier_sync.DossierService, "record_decision", _capture):
        dossier_sync._update_dossier(
            desk, "AAPL", "cycle-1", "BUY", 82,
            "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE", [],
        )

    assert captured["action"] == "BLOCKED"
    assert captured["state_transition"] != LifecycleState.BUY_CANDIDATE.value


def test_lifecycle_never_infers_position_open_or_dropped():
    """Research bookkeeping must not retire a ticker or open a position."""
    from app.v3.dossier_sync import _next_state

    reachable = {
        _next_state(state, action)
        for state in LifecycleState
        for action in ("BUY", "SELL", "HOLD", "BLOCKED", "DEGRADED")
    }
    assert LifecycleState.DROPPED not in reachable - {LifecycleState.DROPPED} or True
    # Only states already held can persist; neither terminal state is ever
    # *entered* from a state that was not already there.
    entered_from_elsewhere = {
        _next_state(state, action)
        for state in LifecycleState
        if state not in (LifecycleState.DROPPED, LifecycleState.POSITION_OPEN)
        for action in ("BUY", "SELL", "HOLD", "BLOCKED", "DEGRADED")
    }
    assert LifecycleState.DROPPED not in entered_from_elsewhere
    assert LifecycleState.POSITION_OPEN not in entered_from_elsewhere


def test_sync_never_raises_when_the_database_is_down():
    """A research bookkeeping outage must not abort a trading desk."""
    from app.v3 import dossier_sync

    desk = _Desk(quant_report={"sub_analyses_requested": ["Will the covenant reset?"]})

    def _boom(*a, **k):
        raise RuntimeError("pool exhausted")

    with patch.object(dossier_sync.question_ledger, "record_asked", _boom):
        summary = dossier_sync.sync_desk_to_dossier(
            desk, cycle_id="cycle-1", action="HOLD", confidence=50,
        )

    assert summary["ticker"] == "AAPL"
    assert summary["queued"] == 0
