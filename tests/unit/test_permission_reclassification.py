"""
Test: Permission Reclassification — Verify DESTRUCTIVE → WRITE for paper trading.

Uses source-level AST inspection to avoid needing the full runtime import chain.
This verifies that the code DEFINES the correct permissions, regardless of
which modules are importable in the test environment.
"""

import ast
import pytest


def _get_permission_for_tool(filepath: str, tool_name: str) -> str | None:
    """Parse a Python file's AST and find the permission kwarg for a @registry.register() call."""
    import os
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Look for @registry.register(name="tool_name", ..., permission=...)
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "register":
                name_val = None
                perm_val = None
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        name_val = kw.value.value
                    if kw.arg == "permission" and isinstance(kw.value, ast.Attribute):
                        perm_val = kw.value.attr  # e.g. "WRITE" or "DESTRUCTIVE"
                if name_val == tool_name:
                    return perm_val

    return None


# ── Paper Trading Tools → WRITE ──────────────────────────────────────

TRADING_TOOLS_FILE = "app/tools/trading_tools.py"


def test_buy_stock_is_write():
    perm = _get_permission_for_tool(TRADING_TOOLS_FILE, "buy_stock")
    assert perm == "WRITE", f"buy_stock permission should be WRITE, got {perm}"


def test_sell_stock_is_write():
    perm = _get_permission_for_tool(TRADING_TOOLS_FILE, "sell_stock")
    assert perm == "WRITE", f"sell_stock permission should be WRITE, got {perm}"


def test_add_to_watchlist_is_write():
    perm = _get_permission_for_tool(TRADING_TOOLS_FILE, "add_to_watchlist")
    assert perm == "WRITE", f"add_to_watchlist permission should be WRITE, got {perm}"


def test_remove_from_watchlist_is_write():
    perm = _get_permission_for_tool(TRADING_TOOLS_FILE, "remove_from_watchlist")
    assert perm == "WRITE", f"remove_from_watchlist permission should be WRITE, got {perm}"


# ── Data Tools → Default (READ_ONLY or no explicit permission) ────────


def test_get_market_data_has_no_destructive():
    """get_market_data should not be DESTRUCTIVE."""
    perm = _get_permission_for_tool("app/tools/finance_tools.py", "get_market_data")
    assert perm != "DESTRUCTIVE", f"get_market_data should not be DESTRUCTIVE, got {perm}"
