"""No module may disable logging as a side effect of being imported.

`logging.disable(CRITICAL)` is PROCESS-WIDE and permanent. A module that calls
it at import scope silences every logger in the interpreter — the caller's
included — and nothing the caller does can put it back, because they never asked
for it and do not know it happened.

`scripts/gate_ablation.py` did exactly that, with a reasonable motive: a gate
replay is chatty and the report is the point. The cost showed up three files
away. pytest imports every module under `tests/unit/` at COLLECTION, so
importing `test_gate_ablation_reads_mongo.py` fired the disable, and three
`caplog` assertions in `test_doctrine_mining` failed with nothing connecting
them to it. Those three were written off as flaky across three consecutive full
runs — they passed alone, and they passed in every pair — because the trigger
was an import, not a test.

That file's own `_restore_logging` fixture put it back for its OWN tests. When
they were deselected, the import still fired and nothing undid it. A fixture
cannot repair a module-scope side effect it does not run for.

The silence is fine; the SCOPE was wrong. `gate_ablation` enters a
`_quiet_replay()` context inside `main()` now, and restores on the way out.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("app", "scripts")


def _module_scope_calls(tree: ast.Module, names: tuple[str, ...]) -> list[int]:
    """Line numbers of `names` calls reachable without entering a def/class."""
    found: list[int] = []

    def is_main_guard(node) -> bool:
        """`if __name__ == "__main__":` does not run on import.

        That is the whole property this test is about, so counting it would be
        counting the idiom rather than the defect — and a guard that flags
        correct code gets an allowlist bolted on and then ignored.
        """
        if not isinstance(node, ast.If):
            return False
        t = node.test
        return (isinstance(t, ast.Compare)
                and isinstance(t.left, ast.Name) and t.left.id == "__name__"
                and len(t.comparators) == 1
                and isinstance(t.comparators[0], ast.Constant)
                and t.comparators[0].value == "__main__")

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue                      # deferred until called
            if is_main_guard(node):
                continue                      # runs only as a script
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    src = ast.unparse(sub.func)
                    if src in names:
                        found.append(sub.lineno)
    walk(tree.body)
    return found


#: The one that is destructive rather than merely rude. `logging.disable`
#: silences EVERY logger below the given level, process-wide, and there is no
#: way for an importer to discover it happened. `basicConfig` by contrast is a
#: no-op when the root logger already has handlers, which under any test runner
#: it does — it is untidy at module scope, not dangerous, and flagging it here
#: would bury the one that matters under six that do not.
_GLOBAL_LOGGING_MUTATORS = (
    "logging.disable",
)


def _offenders() -> list[str]:
    out: list[str] = []
    for root in SCAN_ROOTS:
        for f in sorted((REPO / root).rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            for lineno in _module_scope_calls(tree, _GLOBAL_LOGGING_MUTATORS):
                out.append(f"{f.relative_to(REPO)}:{lineno}")
    return sorted(out)


def test_no_module_mutates_global_logging_at_import():
    bad = _offenders()
    assert bad == [], (
        "these change logging for the WHOLE PROCESS just by being imported, and "
        "the importer cannot undo what it did not ask for:\n  "
        + "\n  ".join(bad)
        + "\n\nMove it inside the function that wants it, and restore on the "
          "way out — see `_quiet_replay` in scripts/gate_ablation.py.")


def test_the_scanner_sees_module_scope_and_ignores_a_function_body(tmp_path):
    """Negative control, both directions. A call inside a def is deferred until
    someone calls it, which is the whole distinction this test draws."""
    bad = tmp_path / "bad.py"
    bad.write_text("import logging\nlogging.disable(logging.CRITICAL)\n")
    assert _module_scope_calls(ast.parse(bad.read_text()),
                               _GLOBAL_LOGGING_MUTATORS) == [2]

    good = tmp_path / "good.py"
    good.write_text("import logging\n"
                    "def main():\n"
                    "    logging.disable(logging.CRITICAL)\n")
    assert _module_scope_calls(ast.parse(good.read_text()),
                               _GLOBAL_LOGGING_MUTATORS) == []

    under_main = tmp_path / "under_main.py"
    under_main.write_text('import logging\n'
                          'if __name__ == "__main__":\n'
                          '    logging.disable(logging.CRITICAL)\n')
    assert _module_scope_calls(ast.parse(under_main.read_text()),
                               _GLOBAL_LOGGING_MUTATORS) == [], (
        "a __main__ guard does not run on import — flagging it would be "
        "flagging the idiom, not the defect")

    guarded = tmp_path / "guarded.py"
    guarded.write_text("import contextlib, logging\n"
                       "@contextlib.contextmanager\n"
                       "def quiet():\n"
                       "    logging.disable(logging.CRITICAL)\n"
                       "    try:\n        yield\n"
                       "    finally:\n        logging.disable(logging.NOTSET)\n")
    assert _module_scope_calls(ast.parse(guarded.read_text()),
                               _GLOBAL_LOGGING_MUTATORS) == []


def test_importing_gate_ablation_leaves_logging_alone():
    """The specific regression, driven rather than grepped."""
    import logging

    before = logging.root.manager.disable
    import scripts.gate_ablation  # noqa: F401
    assert logging.root.manager.disable == before
