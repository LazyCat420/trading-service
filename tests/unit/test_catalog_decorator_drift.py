"""The published catalog and the Python decorator must declare the same parameters.

WHY THIS TEST EXISTS
--------------------
Every trading tool is declared TWICE: once by `@registry.register(parameters=...)`
next to the Python function, and once in the compiled catalog
(`lazy-agent-service/tool_schemas/<owner_app>/<domain>.json`, built into the flat
`tool_schemas.json` that every runtime loads).

`ToolRegistry._put_schema` resolves the collision one way only — **the catalog
wins, always** — and logs a WARNING naming the disagreement. Nothing read that
warning. Two tools had been drifting silently:

| tool | catalog said | the function accepts |
|---|---|---|
| `save_trading_chart` | `ticker, overlays, period` | + `analysis, strategy_name, confidence, reasoning` |
| `get_sec_filings` | `required: [ticker]` | `symbol` alias, `required: []` |

The consequence is not cosmetic. The model only ever sees the catalog, and the
SDK **drops every undeclared key before dispatch** — so the four rich
`save_trading_chart` fields could never be filled, and the trading client's
"AI Analysis Overlays" / "Show Reasoning" panels were structurally empty. On
`get_sec_filings` the 2026-07-29 alias fix (which existed precisely because 30%
of calls were rejected with `Malformed arguments: missing ['ticker']`) never
reached the catalog, so the rejection stayed live.

A warning nobody reads is not a guard. This turns it into a failing test.
"""

from __future__ import annotations

import json
import logging
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CATALOG = os.path.join(_REPO_ROOT, "tool_schemas.json")


def _contract(params: dict | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(sorted properties, sorted required) — the same shape `_put_schema` compares."""
    p = params or {}
    return (
        tuple(sorted((p.get("properties") or {}).keys())),
        tuple(sorted(p.get("required") or [])),
    )


def _reload_every_tool_module() -> list[str]:
    """Re-run every `@registry.register` decorator; return the modules reloaded.

    Reload, not import: `app.tools.registry` loads the catalog at import time
    and the tool modules may already have been imported by an earlier test. A
    reload re-runs each decorator, which calls `_put_schema(source="decorator")`
    against the catalog entry that is already in place — i.e. it replays the
    exact comparison that runs at boot, inside our log-capture window.

    Comparing `registry.schemas` to the catalog file instead would be a check
    that passes in both states: the catalog always wins, so the registry is a
    copy of the file and the two can never disagree.
    """
    import importlib
    import pkgutil

    import app.tools as tools_pkg

    reloaded = []
    for mod in pkgutil.iter_modules(tools_pkg.__path__):
        if mod.name == "registry":
            continue
        qualified = f"app.tools.{mod.name}"
        try:
            module = importlib.import_module(qualified)
            importlib.reload(module)
            reloaded.append(qualified)
        except Exception as e:  # noqa: BLE001 — a broken module is its own test's problem
            logging.getLogger(__name__).warning("skipped %s: %s", qualified, e)
    return reloaded


@pytest.mark.skipif(
    not os.path.exists(_CATALOG),
    reason=(
        "tool_schemas.json is gitignored, so it is absent from a fresh worktree. "
        "Symlink it: ln -s ../../tool_schemas.json tool_schemas.json"
    ),
)
def test_no_tool_drifts_between_catalog_and_decorator(caplog):
    """No trading-owned tool may declare different parameters in the two places.

    Detection is the registry's OWN warning, not a reimplementation of its
    comparison — a test that re-derives the rule cannot see the rule change.
    """
    import app.tools.registry  # noqa: F401  — ensures the catalog is loaded first

    with caplog.at_level(logging.WARNING, logger="lazycat.tool_registry"):
        reloaded = _reload_every_tool_module()

    assert len(reloaded) >= 10, (
        f"only {len(reloaded)} tool modules re-registered — nothing was compared, "
        "so this test proved nothing"
    )

    drifted = [
        r.getMessage()
        for r in caplog.records
        if "DIFFERENT" in r.getMessage()
    ]
    assert not drifted, (
        "The catalog and the decorator disagree. The catalog wins at runtime, "
        "so the model is shown one contract and the executor enforces another. "
        "Fix it in lazy-agent-service/tool_schemas/<owner_app>/*.json and "
        "re-run scripts/build_tool_schemas.py:\n  " + "\n  ".join(drifted)
    )


@pytest.mark.skipif(not os.path.exists(_CATALOG), reason="tool_schemas.json absent (gitignored)")
def test_registry_holds_at_least_the_known_catalog_size():
    """Vacuity guard: an empty registry would pass every assertion above."""
    from app.tools.registry import registry

    assert len(registry.schemas) >= 20, (
        f"only {len(registry.schemas)} schemas loaded — the drift check "
        "above proved nothing"
    )


@pytest.mark.skipif(not os.path.exists(_CATALOG), reason="tool_schemas.json absent (gitignored)")
def test_save_trading_chart_publishes_the_fields_the_client_renders():
    """The four narrative fields must be reachable by the model, not just by Python.

    `AgenticChart.jsx` renders `analysis`, `strategy_name`, `confidence` and
    `reasoning` from `latest_analysis`. If the catalog does not declare them the
    SDK strips them before dispatch and the panel renders empty forever.
    """
    catalog = {t["name"]: t for t in json.load(open(_CATALOG, encoding="utf-8"))}
    props = set((catalog["save_trading_chart"]["parameters"].get("properties") or {}))
    missing = {"analysis", "strategy_name", "confidence", "reasoning"} - props
    assert not missing, f"catalog drops the fields the client renders: {sorted(missing)}"


@pytest.mark.skipif(not os.path.exists(_CATALOG), reason="tool_schemas.json absent (gitignored)")
def test_get_sec_filings_accepts_the_symbol_alias():
    """The 2026-07-29 alias fix has to exist where the model can see it."""
    catalog = {t["name"]: t for t in json.load(open(_CATALOG, encoding="utf-8"))}
    params = catalog["get_sec_filings"]["parameters"]
    assert "symbol" in (params.get("properties") or {}), (
        "agents carrying the old EDGAR-style schema send `symbol`; undeclared "
        "keys are dropped before the required-check, which is what produced the "
        "30% `missing ['ticker']` rejection rate"
    )
    assert not params.get("required"), (
        "`ticker` must not be required — the executor resolves it from the "
        "alias or from the ticker under analysis"
    )
