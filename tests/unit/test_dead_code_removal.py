"""Collectors that never ran, and an import that never resolved.

The 2026-08-03 data-usage audit mapped every collector to its table and every
table to its readers. Nine collectors had no caller anywhere — not scheduled,
not in a cycle, not behind a tool — and four of them existed only to satisfy
their own unit test, which is the shape that makes dead code look alive.

The two tests that matter here are not the deletion list (a deleted file can
only be re-added deliberately) but:

  * test_no_module_imports_a_nonexistent_module — the bug class that hid the
    dead web_search scraper for months;
  * test_target_map_points_only_at_files_that_exist — a self-healing repair
    target aimed at a deleted file sends the fixer after code that cannot run.
"""

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

DELETED_COLLECTORS = [
    # (module, why)
    ("acled_collector", "GDELT-backed conflict events; global.conflict_events had 0 rows"),
    ("gdelt_collector", "war news feed; global.war_news_feed had 0 rows"),
    ("eia_collector", "energy series; global.energy_reports had 0 rows"),
    ("coingecko_collector", "crypto prices; this desk trades equities"),
    ("commodity_collector", "duplicate — market_regime_collector already fills "
                            "asset_prices.commodity, 1,185 rows current to 2026-08-03"),
    ("defillama_collector", "defi_tvl/defi_yield rows had zero readers"),
    ("worldbank_collector", "every macro reader filters source='fred'"),
    ("bls_collector", "same, and its UNEMPLOYMENT row collided with FRED's PK"),
    ("tiingo_collector", "a 4th price vendor behind yfinance/FMP/Polygon/Finviz"),
]


@pytest.mark.parametrize("module,why", DELETED_COLLECTORS)
def test_dead_collector_stays_deleted(module, why):
    assert not (REPO / "app" / "collectors" / f"{module}.py").exists(), (
        f"{module} was deleted 2026-08-03 ({why}). Re-adding it needs a caller "
        f"AND a reader, or it is dead again the day it lands."
    )


# Known-broken imports that fail LOUDLY (they raise to the caller rather than
# being swallowed), with no implementation in this service. Listed so the scan
# still catches NEW silent ones. Each needs a real subsystem, not an import fix.
ALLOWED_BROKEN_IMPORTS = {
    # /trust-scores raises HTTPException(500) to its caller — visible, not silent.
    "app.governance.trust_score_manager",
}


def test_no_module_imports_a_nonexistent_app_module():
    """A broken `from app.x import y` inside a try/except is invisible.

    app/services/web_search.py imported app.collectors.crawl4ai_config — a
    module that never existed in this repo — inside a blanket
    `except Exception`, which logged it as "Batch scrape failed" and moved on.
    Article enrichment had therefore NEVER run: every web-search result stayed
    a snippet, and the failure was indistinguishable from a flaky dependency.
    """
    broken: list[str] = []
    for path in (REPO / "app").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for mod in mods:
                if not mod.startswith("app.") or mod in ALLOWED_BROKEN_IMPORTS:
                    continue
                rel = mod.replace(".", "/")
                if ((REPO / f"{rel}.py").exists()
                        or (REPO / rel / "__init__.py").exists()):
                    continue
                broken.append(f"{path.relative_to(REPO)}:{node.lineno} -> {mod}")

    assert not broken, "imports that cannot resolve:\n  " + "\n  ".join(broken)


def test_target_map_points_only_at_files_that_exist():
    """The self-healer picks a repair target from these maps. An entry aimed
    at a deleted file sends it to patch code that never runs."""
    from app.cognition.evolution import target_map

    missing = {}
    for name in ("SCRAPER_MAP", "PROMPT_MAP", "STRATEGY_MAP", "OPTIMIZER_MAP"):
        for key, rel in getattr(target_map, name, {}).items():
            if not (REPO / rel).exists():
                missing[f"{name}[{key}]"] = rel
    assert not missing, f"target_map entries with no file: {missing}"


def test_web_search_uses_the_real_scraper_path():
    """Assert on the IMPORT, not on any mention of the name — the fix's own
    comment explains the old module, and a substring check matched that."""
    import inspect

    from app.services.web_search import WebSearchService

    src = inspect.getsource(WebSearchService._scrape_top_articles)
    assert "from app.collectors.crawl4ai_config import" not in src
    assert "from app.services.scraper_client import scraper_client" in src


def test_discovery_us_filter_is_wired_to_a_real_implementation():
    """It imported app.validation.ticker_validator (nonexistent) and fell back
    to `return True`, so the filter admitted every foreign ticker it existed
    to drop, silently."""
    import inspect

    from app.services import discovery_mode
    from app.utils.us_ticker_resolver import is_us_tradeable

    src = inspect.getsource(discovery_mode)
    assert "from app.utils.us_ticker_resolver import is_us_tradeable" in src
    assert "from app.validation.ticker_validator import" not in src
    # ...and the implementation it now reaches actually filters.
    assert is_us_tradeable("AAPL") is True
    assert is_us_tradeable("000660.KS") is False
    assert is_us_tradeable("6758.T") is False
