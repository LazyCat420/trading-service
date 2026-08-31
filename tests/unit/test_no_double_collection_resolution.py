"""Nothing may hand a RESOLVED collection name to the table-name API.

`app/db/mongo_store._coll` says it plainly: "Never take a collection name from
a caller. Mongo creates a collection on first write, so a name that bypasses
this function does not error; it silently starts a second, invisible
collection."

Every `mongo_store` / `mongo_query` helper takes a POSTGRES TABLE NAME and
resolves it exactly once. Passing `collection_for(table)` resolves it twice.
Today that is a no-op only because `renames_active()` is False and
`collection_for` is the identity — so the defect is invisible until the day the
renames are switched on, at which point reads go to a collection that does not
exist and writes create one that nothing else can see.

Found by the adversarial review of the Postgres-reader port at
`scripts/agent_latency_report.py:208`, where a test had been written to assert
the doubled name rather than the contract, pinning the bug in place.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("app", "scripts", "tests")
_MODULES = {"mongo_store", "mongo_query"}


def _resolver_call(node: ast.AST) -> bool:
    """Is this expression a call to collection_for / target_collection_for?"""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
    return name in ("collection_for", "target_collection_for")


def _offenders() -> list[str]:
    out: list[str] = []
    for root in SCAN_ROOTS:
        for f in sorted((REPO / root).rglob("*.py")):
            if "__pycache__" in f.parts or f.name == Path(__file__).name:
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in _MODULES
                        and node.args):
                    continue
                if _resolver_call(node.args[0]):
                    out.append(f"{f.relative_to(REPO)}:{node.lineno} "
                               f"{ast.unparse(node)[:120]}")
    return sorted(out)


def test_no_call_resolves_the_collection_name_twice():
    bad = _offenders()
    assert bad == [], (
        "these pass an already-resolved collection name to an API whose "
        "contract is a TABLE name, so it is resolved twice. Harmless only "
        "while renames are off; the day they are switched on, the read misses "
        "and the write creates an invisible second collection:\n  "
        + "\n  ".join(bad))


def test_the_scanner_finds_the_shape_it_exists_to_catch(tmp_path):
    """Negative control — and the positive form must NOT be flagged."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from app.db import mongo_store\n"
        "from app.db.collections import collection_for\n"
        "def f(p):\n"
        "    return mongo_store.aggregate(collection_for('v3_agent_telemetry'), p)\n"
    )
    tree = ast.parse(bad.read_text())
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id in _MODULES
            and n.args and _resolver_call(n.args[0])]
    assert len(hits) == 1

    good = tmp_path / "good.py"
    good.write_text(
        "from app.db import mongo_store\n"
        "def f(p):\n"
        "    return mongo_store.aggregate('v3_agent_telemetry', p)\n"
    )
    tree = ast.parse(good.read_text())
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id in _MODULES
            and n.args and _resolver_call(n.args[0])]
    assert hits == []


def test_renames_are_still_off_which_is_why_this_is_latent():
    """If this ever flips, every offender above becomes a live bug."""
    from app.db.collections import renames_active
    assert renames_active() is False, (
        "renames are ON — re-run test_no_call_resolves_the_collection_name_twice "
        "before anything else; a doubled resolution is no longer a no-op")
