"""Import-health regression tests for the autoresearch subsystem.

Bug history (2026-07-14): the whole app.pipeline.* tree and
app.services.vllm_client were deleted in earlier refactors, but autoresearch
kept importing them inside broad try/excepts. Result: the LLM reflection fell
back to canned text every cycle, llm_performance_score was pinned at exactly
50.0, and the triage audit silently no-op'd — all invisible in the report
status. These tests fail if a dead import creeps back in.
"""
import ast
import os

import pytest

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "app"))

DEAD_MODULES = ("app.pipeline", "app.services.vllm_client")


def _iter_py_files():
    for root, _dirs, files in os.walk(APP_DIR):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def test_no_dead_module_imports_anywhere():
    """No file under app/ may import a module tree that no longer exists."""
    offenders = []
    for path in _iter_py_files():
        with open(path, encoding="utf-8") as fh:
            try:
                tree = ast.parse(fh.read())
            except SyntaxError:
                continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == dead or name.startswith(dead + ".") for dead in DEAD_MODULES):
                    offenders.append(f"{os.path.relpath(path, APP_DIR)}: {name}")
    assert not offenders, f"Dead-module imports found: {offenders}"


def test_reflection_llm_import_resolves():
    """reflection.py's LLM import path must exist (the silent-fallback bug)."""
    from app.services.prism_agent_caller import llm, Priority  # noqa: F401


def _module_imports(module) -> set[str]:
    import inspect
    tree = ast.parse(inspect.getsource(module))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_llm_audit_has_no_dead_imports():
    """llm_audit's dead get_trends import forced the 0.5-default score."""
    from app.autoresearch.auditors import llm_audit
    for name in _module_imports(llm_audit):
        assert not name.startswith("app.pipeline"), name


def test_triage_audit_has_no_dead_imports():
    from app.autoresearch.auditors import triage_audit
    for name in _module_imports(triage_audit):
        assert not name.startswith("app.pipeline"), name
