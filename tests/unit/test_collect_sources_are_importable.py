"""Every source /collect advertises must actually load.

The route imports its collector INSIDE the handler
(``app/scraper/api/routes/collect.py``), so a broken collector module is not a
boot failure — it is a per-request ``ImportError`` that the route's blanket
handler answers as ``200 {"count": 0, "error": ...}``. Nothing distinguishes
that from "this source had nothing today".

So the advertised set and the loadable set must be proved equal. Two ways this
has actually gone wrong here:

  - ``engine="vision"`` stayed in the ScrapeRequest Literal after the engine was
    deleted, so the request validated and came back 200/success=false.
  - ``discourse``/``xenforo`` were advertised for 27 days while importing a
    module scraper-service does not ship.

Set equality in BOTH directions, never a count: an advertised source with no
handler and a handler for an unadvertised source are different bugs, and a
length check catches neither when they happen together.
"""
import inspect
import pathlib
import re
import subprocess

import pytest

from app.scraper.api import schemas
from app.scraper.api.routes import collect as collect_routes
from app.scraper.api.routes import scrape as scrape_routes

ADVERTISED_SOURCES = frozenset(schemas.CollectRequest.model_fields["source"].annotation.__args__)
ADVERTISED_ENGINES = frozenset(schemas.ScrapeRequest.model_fields["engine"].annotation.__args__)


def _dispatched_sources() -> set[str]:
    """The source strings ``collect()`` actually branches on."""
    src = inspect.getsource(collect_routes.collect)
    return {
        s for s in ADVERTISED_SOURCES
        if f'req.source == "{s}"' in src or f'req.source in ("{s}"' in src
        or f'"{s}", "rss"' in src or f'"news", "{s}"' in src
    }


def test_every_advertised_source_is_dispatched():
    missing = ADVERTISED_SOURCES - _dispatched_sources()
    assert not missing, (
        f"/collect advertises {sorted(missing)} in CollectRequest.source but "
        "collect() has no branch for them — the request validates and falls "
        "through to 'Unknown source'."
    )


# Third-party packages the scraper IMAGE installs but trading-service's own venv
# does not — this repo owns the source but never runs it. Parsed from the image's
# requirements so the list cannot drift into an excuse for a real missing dep.
def _image_requirements() -> pathlib.Path | None:
    """scraper-service/requirements.txt, found from a worktree or the primary.

    A git worktree lives in a scratchpad, not beside the sibling repos, so a
    plain ``parents[n] / "scraper-service"`` resolves to nothing there. The
    common git dir always points at the real checkout regardless.
    """
    candidates = []
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=pathlib.Path(__file__).parent, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if common:
            candidates.append(pathlib.Path(common).parent.parent)
    except Exception:  # noqa: BLE001 — git absent is not a test failure
        pass
    candidates.append(pathlib.Path(__file__).resolve().parents[2].parent)
    for root in candidates:
        req = root / "scraper-service" / "requirements.txt"
        if req.is_file():
            return req
    return None


def _image_packages() -> set[str]:
    req = _image_requirements()
    if req is None:
        pytest.fail(
            "cannot locate scraper-service/requirements.txt — without it this "
            "test cannot tell an image-only dependency from a missing one, and "
            "guessing either way defeats the guard."
        )
    names = set()
    for line in req.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[\[<>=!;]", line, 1)[0].strip().lower()
        if name:
            names.add(name.replace("-", "_"))
    return names


@pytest.mark.parametrize("source", sorted(ADVERTISED_SOURCES))
def test_every_advertised_source_imports_its_collector(source):
    """Import the module the handler would import.

    This is the check that catches a partial-copy ImportError before it reaches
    production as a silent count=0.

    A third-party package that only the IMAGE installs (twscrape, crawl4ai, ...)
    is a skip: trading-service owns this source but never executes it, so its
    venv legitimately lacks them. **An ``app.*`` import is never a skip** — that
    is precisely the failure this test exists to catch, and a blanket
    ``except ImportError: skip`` would fail open on it. Same for a package that
    is missing from the image's requirements too: that is a real defect.
    """
    helper = {
        "reddit": "_collect_reddit", "reddit-purge": "_collect_reddit_purge",
        "youtube": "_collect_youtube", "news": "_collect_news", "rss": "_collect_news",
        "kannapedia": "_collect_kannapedia", "leafly": "_collect_leafly",
        "duckduckgo": "_collect_duckduckgo", "twitter": "_collect_twitter",
        "stocktwits": "_collect_stocktwits", "finnews": "_collect_finnews",
    }[source]
    src = inspect.getsource(getattr(collect_routes, helper))

    imported = False
    for line in src.splitlines():
        line = line.strip()
        if not line.startswith("from app.scraper.collectors."):
            continue
        module, _, names = line[len("from "):].partition(" import ")
        try:
            mod = __import__(module, fromlist=["_"])
        except ModuleNotFoundError as exc:
            missing = (exc.name or "").split(".")[0]
            assert not (exc.name or "").startswith("app."), (
                f"{module} imports {exc.name}, which scraper-service does not "
                "ship — this is the partial-copy trap, not a local venv gap."
            )
            assert missing.replace("-", "_") in _image_packages(), (
                f"{module} needs third-party {missing!r}, which is in NEITHER "
                "this venv nor scraper-service/requirements.txt — it would "
                "ImportError in the deployed image too."
            )
            pytest.skip(f"{missing} is installed in the scraper image, not in this venv")
        for name in names.split(","):
            name = name.strip()
            assert hasattr(mod, name), f"{module} has no {name}"
        imported = True
    assert imported, f"{helper} imports no collector — check the helper map"


def test_every_advertised_engine_is_registered():
    """The 'vision' trap: a Literal that outlives its implementation."""
    assert ADVERTISED_ENGINES == set(scrape_routes.ENGINES), (
        "ScrapeRequest.engine and the ENGINES registry disagree: "
        f"advertised-only={sorted(ADVERTISED_ENGINES - set(scrape_routes.ENGINES))}, "
        f"registered-only={sorted(set(scrape_routes.ENGINES) - ADVERTISED_ENGINES)}"
    )
