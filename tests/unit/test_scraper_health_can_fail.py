"""/health must be able to say "unhealthy". It is what Docker polls.

The old handler returned a hardcoded dict. Docker's healthcheck therefore
tested exactly one proposition — "uvicorn is accepting sockets" — and reported
`Up N days (healthy)` through every failure that actually matters here: a
Chromium that will not launch, the container at its pid ceiling, the failure
cache disabled, a collector ImportError-ing on every request.

A health endpoint that cannot fail is not a health endpoint, so these tests are
mostly about the negative case.
"""
from unittest.mock import MagicMock, patch

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from fastapi import FastAPI  # noqa: E402

from app.scraper.api.routes import health as health_route  # noqa: E402


def _client():
    app = FastAPI()
    app.include_router(health_route.router)
    return fastapi_testclient.TestClient(app, raise_server_exceptions=False)


def test_a_healthy_service_reports_200():
    fake = MagicMock(is_closed=False)
    with patch.object(type(health_route.session_manager), "client", property(lambda self: fake)):
        r = _client().get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"
    assert r.json()["checks"]["http_client"] is True


def test_a_closed_http_client_makes_the_container_unhealthy():
    """This is the whole point: something breaks, and Docker can see it."""
    fake = MagicMock(is_closed=True)
    with patch.object(type(health_route.session_manager), "client", property(lambda self: fake)):
        r = _client().get("/health")
    assert r.status_code == 503, "wget exits non-zero on 503 — that is what restarts the container"
    assert r.json()["status"] == "unhealthy"


def test_an_exploding_session_manager_is_unhealthy_not_a_500():
    """A health check that crashes is indistinguishable from one that hangs."""
    def boom(self):
        raise RuntimeError("client is gone")
    with patch.object(type(health_route.session_manager), "client", property(boom)):
        r = _client().get("/health")
    assert r.status_code == 503
    assert r.json()["checks"]["http_client"] is False


def test_a_degraded_failure_cache_is_reported_but_not_fatal():
    """Memory-only costs one wasted fetch per URL; it does not fail a scrape.
    Reporting it matters — nothing logged when it silently switched itself off —
    but restarting the container over it would be wrong."""
    fake = MagicMock(is_closed=False)
    store = MagicMock(enabled=False)
    with patch.object(type(health_route.session_manager), "client", property(lambda self: fake)), \
         patch.object(health_route.failure_cache, "store", store):
        r = _client().get("/health")
    assert r.status_code == 200, "degraded cache must not restart-loop the container"
    assert r.json()["checks"]["failure_cache"] is False


def test_health_reports_the_commit_it_was_built_from():
    """`app/` is a gitignored build artifact, so `git diff` in the deploy repo is
    trivially clean whatever the image holds. Without a stamp, "is the deployed
    scraper current?" has no answer — and it was 66 lines stale on 2026-09-06
    with no signal anywhere."""
    fake = MagicMock(is_closed=False)
    with patch.object(type(health_route.session_manager), "client", property(lambda self: fake)), \
         patch.object(health_route, "BUILD_SHA", "abc1234"), \
         patch.object(health_route, "BUILD_TIME", "2026-09-06T12:00:00Z"):
        body = _client().get("/health").json()
    assert body["build"]["sha"] == "abc1234"
    assert body["build"]["time"] == "2026-09-06T12:00:00Z"


def test_health_does_not_launch_a_browser():
    """It runs every 30s with a 5s timeout. A probe that is itself expensive
    becomes the outage — the deep checks belong on /health/engines."""
    import inspect
    src = inspect.getsource(health_route.health)
    for expensive in ("PlaywrightEngine", "Crawl4aiEngine", "health_check", "chromium"):
        assert expensive not in src, f"/health must not touch {expensive}"
