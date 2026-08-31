"""No executable line may carry a database DSN with credentials in it.

`trading_bot_pass` has never been rotated and is documented in
treesearch-service/docs/CREDENTIAL_ROTATION.md as already published in four
public repositories. Rotating it is a cross-service operation — treesearch,
SmartGarden and the backfill jobs share the server — and is not this repo's to
do alone. What IS this repo's to do is stop adding copies.

Two files carried the production DSN as the DEFAULT of an `os.getenv`, so they
connected to the live archive on any box regardless of the environment:
`build_migration_ledger.py:152` and `pg_embeddings_to_mongo.py:33`. Both now
resolve through `quality_census.pg_url()`, which prefers `PG_ARCHIVE_URL` from
`.env.migration` and exits with the fix in the message when nothing is set.

Docstrings are exempt — several explain the defect using the string that caused
it — and so are the test files that PLANT a DSN to prove a scanner works.
Neither is an executable connection.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("app", "scripts", ".claude")
_DSN = re.compile(r"(?:postgres(?:ql)?(?:\+\w+)?|mongodb(?:\+srv)?)://[^\s\"']*:[^\s\"'@]+@")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() of every Constant that is a docstring."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _credentialed_literals() -> list[str]:
    found: list[str] = []
    for root in SCAN_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for f in sorted(base.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            docs = _docstring_nodes(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and id(node) not in docs
                        and _DSN.search(node.value)):
                    found.append(f"{f.relative_to(REPO)}:{node.lineno}  "
                                 f"{_DSN.sub('<scheme>://<user>:<secret>@', node.value)[:100]}")
    return sorted(found)


def test_no_executable_line_carries_a_credentialed_dsn():
    bad = _credentialed_literals()
    assert bad == [], (
        "a DSN with a password in it is in executable code — it connects "
        "regardless of the environment and it publishes the credential:\n  "
        + "\n  ".join(bad))


def test_the_scanner_finds_one_and_ignores_a_docstring(tmp_path):
    """Negative control, both directions."""
    f = tmp_path / "x.py"
    f.write_text(
        '"""A docstring mentioning postgresql://trader:pw@host:5433/db."""\n'
        'GOOD = "postgresql://host:5433/db"          # no credentials\n'
        'BAD = "postgresql://trader:pw@host:5433/db"\n'
    )
    tree = ast.parse(f.read_text())
    docs = _docstring_nodes(tree)
    hits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs and _DSN.search(n.value)]
    assert hits == ["postgresql://trader:pw@host:5433/db"], hits


def test_the_two_archive_tools_resolve_the_dsn_through_the_shared_helper():
    """The specific fix, pinned so it cannot be undone by a convenient default."""
    for rel in ("scripts/build_migration_ledger.py",
                "scripts/pg_embeddings_to_mongo.py"):
        src = (REPO / rel).read_text()
        assert "from scripts.quality_census import pg_url" in src, rel
        assert "pg_url()" in src, rel
