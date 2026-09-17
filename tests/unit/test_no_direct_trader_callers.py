"""Static AST Dependency Check: Enforce No Direct Production Callers to paper_trader.buy/sell.

In accordance with Control-Plane Completion Plan Section 2:
"Restrict legacy buy() / sell() access to the facade's OBSERVE adapter.
Add a static dependency check or equivalent test that catches new direct production callers."
"""

import ast
import os
from pathlib import Path


def test_no_direct_production_callers_to_paper_trader_buy_or_sell():
    """Scan all Python files in app/ to verify that only app/trading/facade.py

    and internal paper_trader.py functions import or invoke buy() / sell().
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    app_dir = repo_root / "app"

    allowed_files = {
        (app_dir / "trading" / "paper_trader.py").resolve(),
        (app_dir / "trading" / "facade.py").resolve(),
    }

    violations = []

    for root, _, files in os.walk(app_dir):
        for file in files:
            if not file.endswith(".py"):
                continue
            full_path = (Path(root) / file).resolve()
            if full_path in allowed_files:
                continue

            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            try:
                tree = ast.parse(content, filename=str(full_path))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                # 1. Check direct imports: from app.trading.paper_trader import buy, sell
                if isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if "paper_trader" in mod:
                        for alias in node.names:
                            if alias.name in ("buy", "sell"):
                                violations.append(
                                    f"{full_path.relative_to(repo_root)}:{node.lineno} imports '{alias.name}' directly from {mod}"
                                )

                # 2. Check attribute calls: paper_trader.buy(...) or paper_trader.sell(...)
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        attr_name = node.func.attr
                        if attr_name in ("buy", "sell"):
                            # Check if the target is paper_trader
                            if isinstance(node.func.value, ast.Name) and "paper_trader" in node.func.value.id:
                                violations.append(
                                    f"{full_path.relative_to(repo_root)}:{node.lineno} calls '{node.func.value.id}.{attr_name}' directly"
                                )

    assert not violations, (
        "Direct callers to paper_trader.buy/sell detected! All production trading paths must route through TradeFacade.\n"
        + "\n".join(violations)
    )
