"""The service's front door: authentication, and refusing an unsafe URL.

These drive the REAL ASGI app that scraper-service ships (``scraper_main``),
not a stand-in — the whole point is what an unauthenticated caller on the LAN
gets back, and a mock of the app cannot answer that.
"""
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

def _scraper_service_dir() -> Path | None:
    """Locate the scraper-service checkout from a worktree or the primary.

    scraper_main lives in the sibling deploy repo and imports app.scraper.* from
    THIS tree — exactly the arrangement deploy.sh builds. A git worktree sits in
    a scratchpad rather than beside its siblings, so a plain relative path finds
    nothing there; the common git dir points at the real checkout either way.
    SCRAPER_SERVICE_DIR overrides, so a scraper-service worktree can be tested
    before it lands.
    """
    override = os.getenv("SCRAPER_SERVICE_DIR")
    if override:
        return Path(override)
    candidates = []
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=Path(__file__).parent, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if common:
            candidates.append(Path(common).parent.parent / "scraper-service")
    except Exception:  # noqa: BLE001
        pass
    candidates.append(Path(__file__).resolve().parents[2].parent / "scraper-service")
    for c in candidates:
        if (c / "scraper_main.py").is_file():
            return c
    return None


_SCRAPER_SERVICE = _scraper_service_dir()


def _load_app(monkeypatch, api_key: str | None):
    if _SCRAPER_SERVICE is None or not (_SCRAPER_SERVICE / "scraper_main.py").is_file():
        pytest.skip("scraper-service checkout not found (set SCRAPER_SERVICE_DIR)")
    if api_key is None:
        monkeypatch.delenv("SCRAPER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SCRAPER_API_KEY", api_key)
    monkeypatch.syspath_prepend(str(_SCRAPER_SERVICE))
    sys.modules.pop("scraper_main", None)
    return importlib.import_module("scraper_main").app


def test_a_request_without_the_key_is_refused(monkeypatch):
    app = _load_app(monkeypatch, "s3cret")
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/scrape", json={"url": "https://example.com", "engine": "http"})
    assert r.status_code == 401, "the LAN must not be able to drive this API"


def test_a_request_with_the_wrong_key_is_refused(monkeypatch):
    app = _load_app(monkeypatch, "s3cret")
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/scrape", json={"url": "https://example.com"},
                   headers={"X-Scraper-Key": "guess"})
    assert r.status_code == 401


def test_health_is_never_gated(monkeypatch):
    """Docker's healthcheck is a plain `wget` with no headers. If auth could
    gate /health, enabling the key would mark the container unhealthy and
    restart-loop it."""
    app = _load_app(monkeypatch, "s3cret")
    with fastapi_testclient.TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_an_unset_key_leaves_the_service_open(monkeypatch):
    """The rollout depends on this: deploy the service first (no key anywhere,
    behaves exactly as before), then set the key on both sides. A static
    `AUTH_ENABLED` pin could not be armed from the environment this way, and the
    live cycle would eat a 401 mid-sweep."""
    app = _load_app(monkeypatch, None)
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/scrape", json={"url": "file:///etc/hostname", "engine": "http"})
    assert r.status_code != 401, "no key configured must mean no auth"


def test_the_key_holder_still_cannot_read_the_container(monkeypatch):
    """Auth and the URL guard are independent. Holding the key does not buy a
    file:// read — the guard runs after authentication, not instead of it."""
    app = _load_app(monkeypatch, "s3cret")
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/scrape", json={"url": "file:///proc/self/environ", "engine": "playwright"},
                   headers={"X-Scraper-Key": "s3cret"})
    assert r.status_code == 200          # answered, not crashed
    body = r.json()
    assert body["success"] is False
    assert "scheme" in (body["error"] or "").lower()
    assert not body.get("content"), "no container content may come back"


def test_an_authorised_caller_cannot_reach_the_internal_network(monkeypatch):
    app = _load_app(monkeypatch, "s3cret")
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/scrape", json={"url": "http://10.0.0.16:7777/agent", "engine": "http"},
                   headers={"X-Scraper-Key": "s3cret"})
    body = r.json()
    assert body["success"] is False
    assert "private" in (body["error"] or "")


def test_a_batch_larger_than_the_cap_is_rejected_by_validation(monkeypatch):
    """51 jobs x 20 concurrency is the pid-exhaustion shape that took the NAS
    down twice, and it was a legal request."""
    app = _load_app(monkeypatch, "s3cret")
    jobs = [{"url": f"https://example.com/{i}", "engine": "playwright"} for i in range(51)]
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/scrape/batch", json={"jobs": jobs, "max_concurrency": 20},
                   headers={"X-Scraper-Key": "s3cret"})
    assert r.status_code == 422, "an unbounded batch must not validate"


def test_a_batch_within_the_cap_still_validates(monkeypatch):
    app = _load_app(monkeypatch, "s3cret")
    jobs = [{"url": "file:///etc/hostname", "engine": "http"}]   # guarded, but valid shape
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/scrape/batch", json={"jobs": jobs, "max_concurrency": 1},
                   headers={"X-Scraper-Key": "s3cret"})
    assert r.status_code == 200
    assert r.json()["results"][0]["success"] is False   # refused by the URL guard
