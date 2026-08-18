"""The MCP namespace has one definition, and every site must use it.

The prefix `mcp__lazy-tool-service__` was hardcoded in five places — three
strippers (`tool_logging`, `tool_optimizer`, `tool_telemetry`) and two
constructors (`base_agent`, `prism_registration`) — with `tool_optimizer`
carrying a "Must stay in sync with tool_logging.py" comment instead of an
import. When the service was renamed to `lazy-agent-service`, that arrangement
had exactly one outcome available: the constructors keep minting a name the
strippers no longer recognise.

Neither half fails loudly. An unstripped name does not raise; it silently
fails to match a whitelist, which reads as "the agent called an off-whitelist
tool" — the same artifact that produced a false "zero whitelisted tools are
used by any agent" report on 2026-07-25. A constructed-but-unroutable name
surfaces as the model saying the tool is unavailable.

So the scan below is the real test: a sixth hardcoded copy fails here.
"""

import ast
import re
from pathlib import Path

import pytest

from app.services.mcp_prefix import (
    MCP_EMIT_PREFIX,
    MCP_PREFIXES,
    mcp_tool_name,
    strip_mcp_prefix,
)


@pytest.mark.unit
def test_both_service_names_strip_to_the_same_bare_name():
    """The rename is live in one prism scope and not the others, so BOTH
    spellings arrive today depending on which scope the call came through."""
    assert strip_mcp_prefix("mcp__lazy-tool-service__get_sec_filings") == "get_sec_filings"
    assert strip_mcp_prefix("mcp__lazy-agent-service__get_sec_filings") == "get_sec_filings"


@pytest.mark.unit
def test_bare_names_pass_through_untouched():
    """Callers hand this mixed lists; a bare name must not be mangled."""
    assert strip_mcp_prefix("get_sec_filings") == "get_sec_filings"
    assert strip_mcp_prefix("") == ""
    assert strip_mcp_prefix(None) == ""


@pytest.mark.unit
def test_catch_all_mcp_underscore_is_matched_last():
    """`mcp_` is a prefix of the long forms. If it were tried first it would
    turn `mcp__lazy-agent-service__x` into `_lazy-agent-service__x` — a name
    that matches no whitelist and no schema."""
    assert MCP_PREFIXES[-1] == "mcp_"
    assert strip_mcp_prefix("mcp__lazy-agent-service__x") == "x"


@pytest.mark.unit
def test_constructor_skips_already_namespaced_and_domain_selectors():
    """`prism_registration` feeds whitelists that legitimately contain both."""
    assert mcp_tool_name("get_sec_filings") == f"{MCP_EMIT_PREFIX}get_sec_filings"
    assert mcp_tool_name("mcp__lazy-agent-service__x") == "mcp__lazy-agent-service__x"
    assert mcp_tool_name("domain:market-data") == "domain:market-data"
    assert mcp_tool_name("") == ""


@pytest.mark.unit
def test_constructed_names_survive_a_round_trip():
    """Whatever we emit, our own strippers must canonicalise it back. This is
    what actually breaks when the two halves disagree."""
    for bare in ("get_sec_filings", "lazy_web_search", "news_search"):
        assert strip_mcp_prefix(mcp_tool_name(bare)) == bare


@pytest.mark.unit
def test_no_module_hardcodes_the_prefix_outside_mcp_prefix():
    """SCAN, not another per-module case — a sixth copy fails here.

    Parsed with `ast` rather than grepped, because prose is allowed to name the
    prefix and code is not. A line-based scan flagged the docstring in
    `tool_logging.py` that *explains* the stripping, which would have trained
    the next reader to ignore this test.
    """
    app_dir = Path(__file__).resolve().parents[2] / "app"
    allowed = {"app/services/mcp_prefix.py"}

    def docstring_nodes(tree: ast.AST) -> set[int]:
        """id() of every string Constant that is a docstring."""
        out = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = getattr(node, "body", None) or []
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    out.add(id(body[0].value))
        return out

    offenders: list[str] = []
    scanned = 0
    for path in sorted(app_dir.rglob("*.py")):
        rel = str(path.relative_to(app_dir.parent)).replace("\\", "/")
        if rel in allowed:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        scanned += 1
        docs = docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docs
                and re.search(r"mcp__lazy-(tool|agent)-service__", node.value)
            ):
                offenders.append(f"{rel}:{node.lineno}: {node.value[:70]!r}")

    assert scanned > 100, (
        f"only parsed {scanned} modules — the walk is not reaching app/, which "
        "would make this test vacuous rather than passing"
    )
    assert not offenders, (
        "these modules hardcode the MCP prefix in a string literal instead of "
        "importing from app.services.mcp_prefix, so a rename will half-land "
        "again:\n  " + "\n  ".join(offenders)
    )
