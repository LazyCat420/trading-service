"""The configured store must point at the NAS, never at localhost.

This used to assert on `settings.DATABASE_URL`. That field was deleted on
2026-08-28 (app/config/config.py:255), so from that day the test raised
`AttributeError` — a red that says nothing about the runtime, in a file named
"runtime health". Before 08-28 it was worse than useless: it was checking the
address of the store the cycle had already stopped using on 08-19, and passing.

The check itself is still worth having, and for the original reason: the value
comes out of `.env`, and a `.env` that fails to parse leaves the default in
place. The default is a localhost URI, and a localhost Mongo on this box is
EMPTY — so the failure mode is a service that starts cleanly, reads nothing,
and reports no error. What changed is which setting carries the address.
"""
import pytest

from app.config.config import settings


def test_production_store_is_not_localhost():
    uri = settings.PRISM_MONGO_URI
    assert "localhost" not in uri and "127.0.0.1" not in uri, (
        f"PRISM_MONGO_URI fell back to a local default ({uri!r}) — .env did not "
        "supply one. A localhost Mongo here is empty, so the service will start "
        "healthy and read nothing."
    )
    assert "10.0.0.16" in uri, f"PRISM_MONGO_URI is not the NAS instance: {uri!r}"


def test_the_trading_database_is_named():
    assert settings.TRADING_MONGO_DB == "trading_bot", settings.TRADING_MONGO_DB


def test_the_postgres_settings_stay_deleted():
    """A regression guard, not a formality.

    `DATABASE_URL` was removed from the settings object deliberately: it had no
    reader, and a live-looking postgresql:// default in the place a session
    looks for "where is the data" is how the frozen archive kept being treated
    as current. Putting it back would re-arm that, and would also re-arm every
    legacy script that reaches the DSN through `settings`.
    """
    for name in ("DATABASE_URL", "TEST_DATABASE_URL"):
        assert not hasattr(settings, name), (
            f"settings.{name} is back. The archive DSN belongs in .env.migration, "
            "read explicitly by the migration/parity tooling — not in the "
            "settings object every module imports."
        )
