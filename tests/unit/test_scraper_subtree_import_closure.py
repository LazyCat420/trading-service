"""The scraper subtree must import nothing scraper-service does not ship.

This is the 13-day vision-OCR outage, as a test — and its second occurrence,
which the first version of this guard could not see.

``scraper-service/deploy.sh`` PRE_BUILD stages exactly three things:

    trading-service/app/scraper/.            -> app/scraper/
    trading-service/app/utils/text_utils.py  -> app/utils/text_utils.py
    lazycat-sdk/lazycat                      -> lazycat/

Nothing else under ``app.`` exists in that image. An import of anything else
raises ``ImportError`` **only in the deployed container**, and because the
routes import their collectors *inside the handler*, it surfaces as
``HTTP 200 {"count": 0, "error": "No module named ..."}`` while ``/health``
stays green. That is indistinguishable from "this source had nothing today".

**Why this is an allowlist and not a list of banned packages.** The original
guard matched ``app\\.(services|db|v3|agents|collectors)``. One day after it
was written, ``f4e74a42`` added ``from app.utils.async_utils import ...`` to
two collectors — landing in the one namespace the denylist did not name. It
went unnoticed for 27 days. A denylist only ever covers the failures already
suffered; the staged set is small, closed, and known, so assert against that.
"""
import ast
import pathlib

import pytest

# Exactly what deploy.sh PRE_BUILD copies. Keep in sync with that file — if a
# module is added there, add it here and the test starts allowing it.
STAGED_MODULES = frozenset({"app.utils.text_utils"})
STAGED_PACKAGES = ("app.scraper",)

_SCRAPER_ROOT = pathlib.Path(__file__).resolve().parents[2] / "app" / "scraper"

# A floor, not an exact count: an empty or truncated scan must be a failure,
# not a vacuous pass. rglob on a path that does not exist yields nothing and
# raises nothing, so without this the guard silently stops guarding the day
# someone renames the directory.
_MIN_FILES_SCANNED = 25


def _is_staged(module: str) -> bool:
    """Is ``module`` present in the scraper-service image?"""
    if not module.startswith("app."):
        return True  # third-party and stdlib are requirements.txt's problem
    if module in STAGED_MODULES:
        return True
    return any(
        module == pkg or module.startswith(pkg + ".") for pkg in STAGED_PACKAGES
    )


def _imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    """Every module an ``import``/``from`` statement names, with its line.

    Walks the whole tree rather than the top level, so a function-local import
    — the form the routes actually use, and the form a top-of-file regex
    cannot see — is caught too.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # `import app.utils.async_utils` / `... as x`
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) stay inside the subtree by definition.
            if node.level:
                continue
            mod = node.module or ""
            if mod == "app":
                # `from app import services` — the name is the submodule.
                found.extend((f"app.{a.name}", node.lineno) for a in node.names)
            else:
                found.append((mod, node.lineno))
    return found


def test_the_scraper_subtree_imports_only_what_the_image_ships():
    assert _SCRAPER_ROOT.is_dir(), (
        f"{_SCRAPER_ROOT} does not exist — this guard cannot scan a tree that "
        "is not there, and an empty scan passes vacuously. If app/scraper "
        "moved, update _SCRAPER_ROOT."
    )

    files = sorted(_SCRAPER_ROOT.rglob("*.py"))
    assert len(files) >= _MIN_FILES_SCANNED, (
        f"scanned only {len(files)} files under {_SCRAPER_ROOT}; expected at "
        f"least {_MIN_FILES_SCANNED}. A scan this small means the walk broke, "
        "not that the subtree shrank."
    )

    offenders = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, lineno in _imported_modules(tree):
            if not _is_staged(module):
                rel = path.relative_to(_SCRAPER_ROOT.parent.parent)
                offenders.append(f"{rel}:{lineno}: {module}")

    assert not offenders, (
        "app/scraper may import only what scraper-service's deploy.sh stages "
        f"({', '.join(sorted(STAGED_MODULES))} + app.scraper.*). These would "
        "raise ImportError in the deployed image, and the route's blanket "
        "handler would answer 200 with count=0:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "source,expected_offender",
    [
        ("from app.services.prism_agent_caller import x", "app.services.prism_agent_caller"),
        ("from app.utils.async_utils import run_in_executor_with_context", "app.utils.async_utils"),
        ("import app.utils.async_utils as au", "app.utils.async_utils"),
        ("from app import services", "app.services"),
        ("from app.db import pool", "app.db"),
        ("def f():\n    from app.v3.rules import y", "app.v3.rules"),
        ("if True:\n    import app.config", "app.config"),
    ],
)
def test_the_guard_catches_every_import_form(source, expected_offender):
    """Sabotage the guard: each of these is a real way to reintroduce the
    outage, and every one must be caught.

    The bottom four are the forms the previous regex missed — an aliased
    ``import``, ``from app import <submodule>``, and any import indented
    inside a function or block.
    """
    modules = [m for m, _ in _imported_modules(ast.parse(source))]
    assert expected_offender in modules
    assert not _is_staged(expected_offender)


@pytest.mark.parametrize(
    "source",
    [
        "from app.utils.text_utils import _extract_seeking_alpha_ssr",
        "from app.scraper.core.rate_limiter import rate_limiter",
        "import app.scraper.engines.http_engine",
        "from lazycat.ratelimit import KeyedRateLimiter",
        "import httpx",
        "from . import sibling",
    ],
)
def test_the_guard_allows_what_the_image_actually_ships(source):
    """The other direction — without this, a guard that failed everything
    would also pass the test above."""
    for module, _ in _imported_modules(ast.parse(source)):
        assert _is_staged(module), module
