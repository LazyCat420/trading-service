"""No test may patch a symbol its target module does not have.

WHY THIS EXISTS
---------------
`unittest.mock.patch` raises `AttributeError` when the attribute is absent. If
that happens inside a FIXTURE, pytest records the test as an ERROR, not a
FAILURE — and the summary line counts them separately:

    4982 passed, 46 skipped, 279 warnings, 16 errors

Sixteen tests never ran, and the headline everyone quotes says "0 failed". On
2026-08-18 that hid five files' worth of tests across two sweeps: a full-suite
report was read as "the whole suite is green" while `test_finnhub_collector`
(11 tests), `test_autoresearch_smoke` (4), `test_openinsider_collector` and
`test_tradingeconomics_collector` (5) had not executed at all.

The Postgres->Mongo conversion produces this constantly, because every module
that stops importing `get_db` invalidates the fixtures that patch it — and the
resulting silence looks exactly like success.

WHAT IT CHECKS
--------------
Every string literal `patch("app.x.y.NAME")` / `patch.object(mod, "NAME")` in
tests/ is resolved against the real module. A target the module neither
imports nor defines is reported. This is a static read, so it costs nothing
and cannot itself error at setup.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
REPO = TESTS.parent

# Found by AST, not by regex. A docstring that QUOTES `patch("...get_db")`
# while explaining why that target was replaced is prose, not a call — the
# regex version flagged this file's own explanations, and a checker whose
# findings are mostly its own comments gets skimmed, and then a real finding
# goes past. An `ast.Call` cannot be a docstring.


def _patch_targets(tree: ast.AST):
    """Yield (dotted_module, attribute, lineno) for every patch("a.b.C")."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name not in ("patch", "patch.object"):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        target = first.value
        if not target.startswith(("app.", "cycle_main.")):
            continue
        dotted, _, attr = target.rpartition(".")
        if dotted and attr:
            yield dotted, attr, node.lineno


def _module_provides(dotted: str, name: str) -> bool | None:
    """True/False if the module exists and (does not) provide `name`.

    None when the dotted path is not a module in this repo — a patch target
    like "app.services.foo.CONSTANT.attr" or a third-party path is not this
    test's business.
    """
    path = REPO / (dotted.replace(".", "/") + ".py")
    if not path.exists():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any((a.asname or a.name) == name for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any((a.asname or a.name.split(".")[0]) == name for a in node.names):
                return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id == name:
                return True
        elif isinstance(node, ast.arg) and node.arg == name:
            return True
    return False


def _scan() -> tuple[list[str], int]:
    offenders: list[str] = []
    checked = 0
    for path in sorted(TESTS.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for dotted, name, line in _patch_targets(tree):
            provides = _module_provides(dotted, name)
            if provides is None:
                continue
            checked += 1
            if not provides:
                offenders.append(
                    f"{path.relative_to(REPO)}:{line} patches {dotted}.{name}, "
                    f"which {dotted} neither imports nor defines"
                )
    return offenders, checked


def test_no_test_patches_a_symbol_that_does_not_exist():
    offenders, _ = _scan()
    assert not offenders, (
        "these patch targets do not exist, so the patch raises AttributeError "
        "at setup and the test ERRORS rather than fails — invisible in a "
        '"0 failed" summary:\n  ' + "\n  ".join(offenders)
    )


def test_the_scan_actually_resolved_some_targets():
    """Negative control.

    If the regex stops matching, or every dotted path stops resolving to a
    file, the test above passes over an empty set. Require that it resolved a
    substantial number of real targets, so "no offenders" means "checked and
    clean" rather than "looked at nothing".
    """
    _, checked = _scan()
    assert checked > 40, (
        f"only {checked} patch targets resolved; the matcher has probably "
        "broken and the check above is now vacuous"
    )
