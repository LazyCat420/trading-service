"""A mirror failure must be visible in the logs the soak actually reads.

Every per-table promotion ends in a soak that greps the container logs for
"PG fallback", "mirror failed" and "[PG GUARD]" and requires zero hits. That
proof is only worth something if a real failure would have produced a line.

Six Mongo mirror-failure sites logged at DEBUG while cycle_main.py configures
the root logger at INFO, so the lines were never emitted at all. A grep over 48
hours of live container logs returned zero -- not because nothing failed, but
because nothing could be printed. Two of the six were the money ledger.

This test walks the AST rather than grepping, so a reformatted call or a
multi-line string cannot slip past it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_APP = pathlib.Path(__file__).resolve().parents[2] / "app"

# Words that mark a log line as migration evidence. A call whose message
# mentions one of these is something a soak or an operator needs to see.
_EVIDENCE = ("mirror", "pg fallback", "pg guard", "dual-write", "dual write")

# Sites that talk about a *vendor* API fallback, not the Mongo mirror. These are
# genuinely routine and belong at DEBUG.
_ALLOWED_DEBUG_SUBSTRINGS = (
    "fmp_api_key",
    "polygon",
    "duckduckgo",
    "skipping",
)


def _message_of(call: ast.Call) -> str | None:
    """The literal format string of a logger call, if it has one."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.JoinedStr):  # f-string
        return "".join(
            v.value for v in first.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return None


def _logger_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        level = node.func.attr
        if level not in {"debug", "info", "warning", "error", "critical", "exception"}:
            continue
        target = node.func.value
        name = getattr(target, "id", None) or getattr(target, "attr", None)
        if name not in {"logger", "log", "logging", "_logger"}:
            continue
        yield level, node


def _offending_sites():
    out = []
    for path in sorted(_APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - caught by the compile sweep
            continue
        for level, call in _logger_calls(tree):
            if level not in {"debug", "info"}:
                continue
            msg = _message_of(call)
            if not msg:
                continue
            low = msg.lower()
            if not any(word in low for word in _EVIDENCE):
                continue
            if any(ok in low for ok in _ALLOWED_DEBUG_SUBSTRINGS):
                continue
            out.append(f"{path.relative_to(_APP.parent)}:{call.lineno}: "
                       f"logger.{level}({msg[:60]!r})")
    return out


def test_no_mirror_failure_is_logged_below_warning():
    sites = _offending_sites()
    assert not sites, (
        "these log a Mongo mirror/fallback event at a level the running "
        "process does not emit, so a soak grepping for it reads a false "
        "clean:\n  " + "\n  ".join(sites)
    )


def test_the_detector_can_actually_fire(tmp_path):
    """Negative control: the AST walk must catch a planted offender.

    Without this, a detector that silently matched nothing would pass the test
    above forever.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(e):\n"
        "    logger.debug('[X] Mongo mirror failed (non-fatal): %s', e)\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    hits = [
        _message_of(call)
        for level, call in _logger_calls(tree)
        if level == "debug" and (_message_of(call) or "")
    ]
    assert hits and any("mirror" in (h or "").lower() for h in hits)


@pytest.mark.parametrize(
    "path,needle",
    [
        ("app/trading/paper_trader.py", "Mongo mirror failed on BUY ledger write"),
        ("app/trading/paper_trader.py", "Mongo mirror failed on SELL ledger write"),
    ],
)
def test_the_money_mirror_logs_at_error(path, needle):
    """A dropped ledger mirror is not a warning-level event."""
    src = (_APP.parent / path).read_text(encoding="utf-8")
    assert needle in src
    line = next(ln for ln in src.splitlines() if needle in ln)
    assert "logger.error" in line, line.strip()
