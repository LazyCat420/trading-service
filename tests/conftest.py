"""
Shared test fixtures for trading-cycle-backend tests.

Provides:
  - Mocked settings that don't require .env
  - Isolated ToolRegistry for testing tool provisioning
"""
import os
import sys

# Set execution mode to staging during tests to bypass production API key validation check
os.environ["EXECUTION_MODE"] = "staging"

# Unit tests must execute locally-registered tool functions, never proxy tool
# calls to the live lazy-tool-service (lazycat.tool_registry checks this at call time)
os.environ["USE_LAZY_TOOL_SERVICE"] = "false"

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The container gets the lazycat SDK via PYTHONPATH=/app/lazycat-sdk (docker-compose.yml);
# mirror that locally from the sibling checkout so tests import the same code.
#
# Walk up rather than hardcoding "../..": a git worktree sits two levels deeper
# (trading-service/.worktrees/wt-x/), so the fixed relative path resolved to
# trading-service/.worktrees/lazycat-sdk, the SDK never loaded, and the suite
# reported 173 failures and 90 errors in files the branch had not touched —
# every one of them a missing `lazycat` import. Worktree-first is the standing
# workflow here, so the harness has to survive being run from one.
_sdk_dir = ""
for _ancestor in [os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * _n)))
                  for _n in range(1, 6)]:
    _candidate = os.path.join(_ancestor, "lazycat-sdk")
    if os.path.isdir(_candidate):
        _sdk_dir = _candidate
        break
if _sdk_dir and _sdk_dir not in sys.path:
    sys.path.insert(0, _sdk_dir)

# Configure yfinance cache location early to prevent race conditions and permission errors in tests
try:
    import yfinance as yf
    _test_local_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_dir = "/app/memory" if os.path.isdir("/app/memory") else os.path.join(_test_local_dir, "memory")
    yf_cache = os.path.join(cache_dir, "py-yfinance")
    os.makedirs(yf_cache, exist_ok=True)
    yf.set_tz_cache_location(yf_cache)
except Exception:
    pass

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

# ── Database Fixtures ──────────────────────────────────────────────────

@pytest.fixture(scope="session")
def real_test_db_engine():
    """Return the NAS test database URL if explicitly enabled."""
    if not os.environ.get("TRADING_BOT_TEST_DB"):
        return None

    from app.config import settings
    db_url = settings.TEST_DATABASE_URL

    try:
        import psycopg
        with psycopg.connect(db_url, autocommit=True, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        return db_url
    except Exception:
        return None


@pytest.fixture
def real_db(real_test_db_engine):
    """Yield a real cursor on the test database, truncating tables on exit.

    > [!WARNING]
    > This does NOT patch `get_db`. The autouse `patch_get_db` mock stays in
    > force, so the CODE UNDER TEST still talks to a MagicMock while this cursor
    > talks to Postgres. A test that requests only `real_db` and then asserts on
    > what the production code persisted is asserting against the mock and will
    > report a pass it did not earn — `test_pipeline_events_db_persistence` did
    > exactly that, and could not pass in either state.
    >
    > Use **`patch_real_get_db`** below whenever the code under test does its own
    > database access. Use `real_db` only when the test itself issues every query.
    """
    if not real_test_db_engine:
        pytest.skip("Test database not enabled. Set TRADING_BOT_TEST_DB=1 to enable.")

    from psycopg_pool import ConnectionPool
    from scripts.migration.pg_connection import PooledCursor

    pool = ConnectionPool(conninfo=real_test_db_engine, min_size=1, max_size=2, kwargs={"autocommit": True})
    pool.wait()

    with pool.connection() as conn:
        cursor = PooledCursor(conn)
        yield cursor

        # Explicitly close the cursor BEFORE cleanup to prevent
        # [DB LEAK] warnings from PooledCursor.__del__
        cursor.close()

        try:
            tables = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
            if tables:
                table_names = ", ".join([f'"{t[0]}"' for t in tables])
                conn.execute(f"TRUNCATE TABLE {table_names} CASCADE")
        except Exception:
            pass

    pool.close()

@pytest.fixture
def patch_real_get_db(real_db):
    """Patch get_db() to return the real test database cursor."""
    from unittest.mock import patch
    from contextlib import contextmanager
    @contextmanager
    def fake_get_db():
        yield real_db
    with patch("scripts.migration.pg_connection.get_db", fake_get_db):
        yield real_db


@pytest.fixture
def live_db():
    """Read-only cursor on the PRODUCTION database, for calibration replays.

    WHY THIS EXISTS
    ---------------
    `patch_get_db` below is autouse, so it patches `get_db` for EVERY test in
    the suite. That means a test which imports `get_db` and queries "the live
    database" actually queries a MagicMock: `fetchall()` returns `[]` and
    `fetchone()` returns `None`.

    A live audit built that way cannot fail. It reports "no completed cycles on
    record" — indistinguishable from a genuinely empty database — which is how
    the `-k live` checks in test_cycle_invariants.py came to pass their own
    silence test while measuring nothing. An empty result is not evidence of
    health; it is the absence of evidence.

    Requesting this fixture overrides the autouse patch (monkeypatch/patch
    applied later wins) with a REAL connection, so a live test can actually
    fail. It skips rather than silently degrading when the audit is not
    enabled — an audit that quietly turns into a no-op is the bug above.

    Read-only is enforced on the SESSION, not by convention: this points at
    production, and no test is worth a stray UPDATE there.
    """
    if not os.environ.get("TRADING_BOT_LIVE_AUDIT"):
        pytest.skip("live audit — set TRADING_BOT_LIVE_AUDIT=1")

    from contextlib import contextmanager

    import psycopg

    from app.config import settings
    from scripts.migration.pg_connection import PooledCursor

    try:
        conn = psycopg.connect(
            str(settings.DATABASE_URL), autocommit=True, connect_timeout=5
        )
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"production database unreachable: {e}")

    with conn:
        conn.execute("SET default_transaction_read_only = on")
        cursor = PooledCursor(conn)

        @contextmanager
        def _get_db():
            yield cursor

        with patch("scripts.migration.pg_connection.get_db", _get_db):
            yield cursor
        cursor.close()


@pytest.fixture
def mock_db():
    """Provide a mock PooledCursor that behaves like a real DB cursor."""
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    cursor.description = None
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    return cursor


class _ProductionMongoReached(RuntimeError):
    """Raised when a test touches the real MongoDB client."""


@pytest.fixture(autouse=True)
def block_production_mongo(request):
    """Fail any test that reaches the real MongoDB client.

    THE HOLE THIS CLOSES
    --------------------
    `patch_get_db` protects Postgres. Nothing protected Mongo, and every module
    converted off `get_db` silently left the protected set: the autouse mock
    still intercepted a `get_db` the module no longer calls, so the test read
    as isolated while `mongo_store` opened a real connection to the NAS.

    Measured on 2026-08-18, from a plain unit test with no fixtures requested:

        get_doc_db() -> pymongo.database.Database  name=trading_bot
        collections visible: 141

    That is production. Reads there are silent, and writes are not reversible.
    The conversion multiplies the exposure with every file it converts, so the
    guard has to land before the conversion continues, not after.

    WHY IT RAISES INSTEAD OF FAKING
    -------------------------------
    A fake client would have to imitate pymongo well enough that code passing
    against it also passes against the server — cursors, bulk_write results,
    Decimal128 round-trips, `$inc` semantics. A fake that drifts hands back a
    green suite for code the server would reject, which is the failure this
    migration keeps hitting. So the default is fail-closed: unpatched Mongo
    access raises and names the fixture to use. A test that wants a store must
    say so, either by patching `mongo_store`/`mongo_query` itself (the
    `test_watchlist.py` pattern — patch BOTH, since stubbing only the read
    leaves writes pointed at the real store) or by requesting `real_mongo`.

    Opt out for a whole module with `pytestmark = pytest.mark.real_mongo`, or
    per test with `@pytest.mark.real_mongo`.
    """
    if request.node.get_closest_marker("real_mongo"):
        yield
        return

    def _blocked(*_args, **_kwargs):
        raise _ProductionMongoReached(
            "This test reached the real MongoDB client.\n"
            "        Patch app.db.mongo_store (and app.db.mongo_query if the code reads),\n"
            "        or request the `real_mongo` fixture / mark the test\n"
            "        @pytest.mark.real_mongo to use an isolated test database."
        )

    # Patch BOTH the definition and the name `mongo_store` bound at import
    # time. `from app.db.mongo import get_mongo_client` copies the reference,
    # so patching only the source module leaves mongo_store calling the real
    # client — the first version of this guard did exactly that and its probe
    # test reported DID NOT RAISE while the connection went through.
    with patch("app.db.mongo.get_mongo_client", _blocked), \
         patch("app.db.mongo_store.get_mongo_client", _blocked):
        yield


@pytest.fixture
def live_mongo():
    """READ-ONLY handle on the PRODUCTION Mongo database, for the live audit.

    The Mongo twin of `live_db`, and it exists for the same reason. The autouse
    `block_production_mongo` below raises on the real client, so a live audit
    written against Mongo without this fixture does not read production — it
    errors, or (if the error is swallowed, which is the shape that keeps
    happening in this repo) reports an empty result indistinguishable from a
    genuinely empty database. `live_db` documents that exact failure for the
    Postgres side; the audit it protected has since been pointed at a store
    frozen on 2026-08-19, which answers with July and never says so.

    Requesting this overrides the autouse block with a real client, so a live
    test can actually fail. It skips loudly when the audit is off rather than
    degrading into a check that always passes.

    READ-ONLY IS ENFORCED, not documented: every `mongo_store` write is patched
    to raise. This points at production, and no audit is worth a stray write
    there.
    """
    if not os.environ.get("TRADING_BOT_LIVE_AUDIT"):
        pytest.skip("live audit — set TRADING_BOT_LIVE_AUDIT=1")

    import pymongo

    from app.config import settings
    from app.db import mongo_store as _ms

    try:
        client = pymongo.MongoClient(
            settings.PRISM_MONGO_URI, serverSelectionTimeoutMS=5000
        )
        client.admin.command("ping")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"MongoDB unreachable: {e}")

    def _refuse(*a, **k):
        raise AssertionError(
            "the live audit is READ-ONLY — it is pointed at production Mongo"
        )

    writes = ("insert_docs", "upsert_doc", "bulk_upsert", "update_docs",
              "delete_docs", "find_one_and_update")
    patches = [patch("app.db.mongo.get_mongo_client", lambda: client),
               patch("app.db.mongo_store.get_mongo_client", lambda: client)]
    patches += [patch.object(_ms, name, _refuse) for name in writes]
    for p_ in patches:
        p_.start()
    try:
        yield client[_ms.TRADING_MONGO_DB]
    finally:
        for p_ in reversed(patches):
            p_.stop()
        client.close()


@pytest.fixture
def real_mongo():
    """A real Mongo database for tests that need one — never the production DB.

    Pinned to a dedicated database name and ASSERTED, not left to convention:
    the production database is `trading_bot`, one typo away from a test suite
    that truncates the live store. Skips unless explicitly enabled, because a
    fixture that silently degrades to a no-op is the `live_db` bug above.
    """
    if not os.environ.get("TRADING_BOT_MONGO_TEST"):
        pytest.skip("real Mongo test — set TRADING_BOT_MONGO_TEST=1")

    import pymongo

    from app.config import settings

    db_name = os.environ.get("TRADING_MONGO_TEST_DB", "trading_bot_pytest")
    if db_name in ("trading_bot", "prism"):
        pytest.fail(
            f"refusing to run tests against {db_name!r}: that is a production database"
        )

    try:
        client = pymongo.MongoClient(
            settings.PRISM_MONGO_URI, serverSelectionTimeoutMS=5000
        )
        client.admin.command("ping")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"MongoDB unreachable: {e}")

    db = client[db_name]
    with patch("app.db.mongo.get_mongo_client", lambda: client), \
         patch("app.db.mongo_store.get_mongo_client", lambda: client), \
         patch("app.db.mongo_store.TRADING_MONGO_DB", db_name):
        yield db

    client.drop_database(db_name)
    client.close()


@pytest.fixture(autouse=True)
def patch_get_db(mock_db):
    """Patch get_db() globally so no real DB connections are created.

    THE SHAPE MATTERS AS MUCH AS THE ISOLATION
    ------------------------------------------
    This used to be `patch(..., return_value=mock_db)`, which made `get_db()`
    hand back the cursor directly. The real `get_db` is a `@contextmanager`, so
    that fixture accepted a contract production does not offer — and because
    `mock_db` also carries `__enter__`/`__exit__`, BOTH of these passed:

        db = get_db(); db.execute(...)      # broken in production
        with get_db() as db: db.execute(...)  # correct

    A fixture that passes for both states is not a check. It let
    `dossier_service` and `research_queue_service` ship on 2026-08-07 with
    `db = get_db()` in every method — an `AttributeError` on the first
    statement of each — under a green suite.

    So the fake is now shaped like the real thing: a context manager. The wrong
    usage now fails here, which is the only reason it will not ship again.
    """
    from contextlib import contextmanager

    @contextmanager
    def _fake_get_db():
        yield mock_db

    with patch("scripts.migration.pg_connection.get_db", _fake_get_db), \
         patch("scripts.migration.pg_connection._ensure_pool"):
        yield mock_db


# ── vLLM Client Fixtures ──────────────────────────────────────────────


@pytest.fixture
def mock_llm():
    """Provide a mock VLLMClient with a pre-configured chat() response."""
    client = MagicMock()
    client.chat = AsyncMock(return_value=("mock response", 100, 500))
    client.chat_with_tools = AsyncMock(return_value={"text": "mock response", "total_tokens": 100, "elapsed_ms": 500})
    client.model = "test-model"
    client.discover_roles = MagicMock(return_value={})
    client.get_least_busy_model = MagicMock(return_value="test-model")
    client.get_trader_model = MagicMock(return_value="test-model")
    client.queue_status.return_value = {
        "jetson": {"active": 0, "max_concurrent": 2, "queued": 0},
        "dgx_spark": {"active": 0, "max_concurrent": 4, "queued": 0},
    }
    return client


@pytest.fixture(autouse=True)
def reset_peer_session_cache():
    """The peer-session probe in technical_baseline memoises per (market,
    latest, as_of) with a 5-minute TTL — correct in production, but a
    process-global. Without this reset, one test's stubbed `distinct_values`
    answer is served to the NEXT test asking for the same key: measured as 3
    spurious failures in TestTradingDayAgeUsesPeers plus the freshness-seam
    tests the day the cache shipped — shared state crossing test boundaries,
    the same genus as the autouse fixture that handed back a cursor."""
    try:
        from app.quant import technical_baseline as _tb
        getattr(_tb, "_PEER_SESSION_CACHE", {}).clear()
    except Exception:
        pass
    yield
    try:
        from app.quant import technical_baseline as _tb
        getattr(_tb, "_PEER_SESSION_CACHE", {}).clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def patch_llm(mock_llm):
    """Patch the LLM singleton so all modules share the mock.
    
    Originally patched app.services.vllm_client (now removed).
    Now patches lazycat.llm.prism_client which is the current LLM entry point.
    Falls back to no-op if neither module is available.
    """
    try:
        from lazycat.llm import prism_client
        with patch.object(prism_client, "call_agent", mock_llm.chat), \
             patch.object(prism_client, "check_health", AsyncMock(return_value=True)):
            yield mock_llm
    except (ImportError, AttributeError):
        # If lazycat-sdk is not installed in the test env, yield the mock as-is
        yield mock_llm


def pytest_sessionfinish(session, exitstatus):
    """Record the real exit status — config has no `exitstatus` attribute, so
    pytest_unconfigure used to always exit 0, hiding every test failure from
    scripts and CI."""
    session.config._forced_exitstatus = int(exitstatus)


def pytest_unconfigure(config):
    """Force exit to prevent hanging on background threads or connections."""
    import os
    import sys
    # os._exit skips atexit AND stdio flushing — without these flushes the
    # failure report is silently lost when output is piped (block-buffered).
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(getattr(config, "_forced_exitstatus", 0))



@pytest.fixture
def mock_prism_agent(respx_mock):
    """Mocks the /agent endpoint of prism-service with a streaming SSE response."""
    import httpx

    # A generator that simulates the SSE events
    async def sse_generator():
        yield b'data: {"status": "starting"}\n\n'
        yield b'data: {"status": "running", "chunk": "I am a mocked response."}\n\n'
        yield b'data: {"status": "completed", "result": "I am a mocked response."}\n\n'

    respx_mock.post(
        re.compile(r"^.*/agent$")
    ).mock(
        return_value=httpx.Response(
            200, 
            headers={"Content-Type": "text/event-stream"},
            content=sse_generator()
        )
    )
    return respx_mock


@pytest.fixture(autouse=True)
def _policy_gate_price_history_probe(monkeypatch, request):
    """Assume test tickers HAVE price history unless a test says otherwise.

    `_apply_policy_gates` gained a HOLD_NO_PRICE_DATA check before EXECUTE
    (2026-07-27) for the ASIC case: zero price_history rows, full panel run,
    BUY at 68 confidence. The probe is a real DB query, and fixture tickers
    ("TEST", "ASIC", "MP", ...) genuinely have no rows — so without this stub
    every policy-gate assertion in the suite comes back HOLD_NO_PRICE_DATA and
    stops testing what it means to test.

    Autouse so no existing test had to change. Tests that exercise the gate
    itself opt out by patching `has_price_history` directly, which wins
    because it is applied later.
    """
    try:
        import app.quant.technical_baseline as _tb
    except Exception:  # noqa: BLE001 — module may be unavailable in some envs
        return
    monkeypatch.setattr(_tb, "has_price_history", lambda _t: True, raising=False)
