"""
Symbol-level code evidence for the self-healing repair loop.

Replaces the two mechanisms the repair path used to get source in front of a
model, both of which lost information for structural reasons:

* ``target_map.resolve_target`` looked the target up in a hardcoded dict
  (``SCRAPER_MAP``/``PROMPT_MAP``/``OPTIMIZER_MAP``, with ``STRATEGY_MAP``
  literally empty). Anything unmapped simply failed. It then read the whole file
  and cut it at ``raw[:8000]`` — a byte offset with no relationship to where the
  relevant code is.
* ``debate._extract_relevant_context`` then cut again at 4000 chars, keeping
  only *top-level* defs whose name substring-matched a word from the issue text.

This module indexes symbols with the stdlib ``ast`` module and returns the
enclosing function/class by its real line range, with provenance.

WHY AST AND NOT A LANGUAGE SERVER
---------------------------------
Measured against pyright over this repo (625 files), an ast walk returned
byte-for-byte identical references for distinctive names:

    _extract_relevant_context   AST 2   LSP 2     (identical)
    validate_artifact           AST 15  LSP 15    (identical)
    PhaseOutcome                AST 61  LSP 61    (identical)

AST recall is always 100% — it returns a superset — but it matches on *name*
while a language server resolves *scope*, so precision collapses where a name is
reused:

    close                       AST 108   LSP 7     (6.5% precision)
    start                       AST 62    LSP 2     (3.2% precision)
    execute                     AST 1390  LSP 1     (0.1% precision)

So AST is exactly as good as a language server for the typical repair target and
catastrophically worse for a generic method name. Rather than pick one, callers
get an explicit ambiguity signal: see ``SymbolEvidence.is_ambiguous``. Impact
analysis on an ambiguous symbol must not be trusted — emitting 1390 "callers"
would blow the context budget and poison any test selection built on it.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Directories that are not the service's own source.
_SKIP_PARTS = frozenset({
    ".venv", "node_modules", ".git", "__pycache__", "build", "dist", ".mypy_cache",
})

# Above this many same-name references, treat the symbol as ambiguous even if
# only one definition was found — a name that common is being matched by
# coincidence, not by resolution.
AMBIGUITY_REF_THRESHOLD = 40

# Hard ceiling on an excerpt. A single enormous function should not be able to
# consume the whole prompt; it is truncated at a line boundary, and the fact is
# recorded on the evidence rather than left implicit.
MAX_EXCERPT_LINES = 260


@dataclass
class SymbolEvidence:
    """One symbol, its source, and what depends on it."""

    name: str
    relative_path: str
    lineno: int                     # 1-indexed, inclusive
    end_lineno: int                 # 1-indexed, inclusive
    kind: str                       # "function" | "async function" | "class"
    signature: str
    excerpt: str                    # line-numbered source of the symbol itself
    content_hash: str               # sha256 of the excerpt, for staleness checks
    repo_sha: str
    truncated: bool = False
    definition_count: int = 1       # how many symbols in the tree share this name
    references: list[tuple[str, int]] = field(default_factory=list)
    ambiguity_reason: str = ""

    @property
    def is_ambiguous(self) -> bool:
        """True when name-matching cannot be trusted to mean scope-resolution."""
        return bool(self.ambiguity_reason)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_ambiguous"] = self.is_ambiguous
        return d


def _iter_source_files(root: Path | None = None):
    root = root or PROJECT_ROOT
    for path in root.rglob("*.py"):
        if _SKIP_PARTS.intersection(path.parts):
            continue
        yield path


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def get_repo_sha() -> str:
    """Current HEAD, or ``"unknown"`` when git is unavailable (e.g. in-container).

    The trading-service image ships source WITHOUT .git, so this is expected to
    return "unknown" in production. Evidence still carries a content hash, which
    is what actually detects staleness.
    """
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=10,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception as e:  # noqa: BLE001 — provenance is advisory
        logger.debug("[CODE-EVIDENCE] repo sha unavailable: %s", e)
    return "unknown"


def _signature_of(node: ast.AST, source_lines: list[str]) -> str:
    """The def/class line(s), up to the colon."""
    start = node.lineno - 1
    end = min(getattr(node, "body", [node])[0].lineno - 1, len(source_lines))
    sig = " ".join(ln.strip() for ln in source_lines[start:end]).strip()
    return sig[:400]


def _kind_of(node: ast.AST) -> str:
    if isinstance(node, ast.AsyncFunctionDef):
        return "async function"
    if isinstance(node, ast.ClassDef):
        return "class"
    return "function"


def find_definitions(symbol: str, root: Path | None = None) -> list[tuple[Path, ast.AST]]:
    """Every definition of ``symbol``, at any nesting depth.

    ``ast.walk`` rather than a top-level scan: the old extractor only looked at
    module-level defs, so a method on a class was invisible to it.
    """
    found: list[tuple[Path, ast.AST]] = []
    for path in _iter_source_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and node.name == symbol:
                found.append((path, node))
    return found


def find_references(symbol: str, root: Path | None = None) -> list[tuple[str, int]]:
    """Every syntactic reference to ``symbol`` as an identifier.

    Ignores comments and docstrings, which is the one place a plain grep was
    measurably wrong (it reported a docstring mention of PhaseOutcome as a
    reference; pyright and this function both correctly exclude it).
    """
    hits: set[tuple[str, int]] = set()
    for path in _iter_source_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        rel = _rel(path)
        for node in ast.walk(tree):
            line = None
            if isinstance(node, ast.Name) and node.id == symbol:
                line = node.lineno
            elif isinstance(node, ast.Attribute) and node.attr == symbol:
                line = node.lineno
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and node.name == symbol:
                line = node.lineno
            elif isinstance(node, ast.alias) and symbol in (node.name, node.asname):
                line = getattr(node, "lineno", None)
            if line is not None:
                hits.add((rel, line))
    return sorted(hits)


def build_symbol_evidence(
    symbol: str,
    *,
    prefer_path: str | None = None,
    include_references: bool = True,
    root: Path | None = None,
) -> SymbolEvidence | None:
    """Bounded evidence for ``symbol``, or None if it is not defined anywhere.

    ``prefer_path`` disambiguates when several files define the same name — pass
    the file from the traceback.
    """
    definitions = find_definitions(symbol, root)
    if not definitions:
        logger.info("[CODE-EVIDENCE] no definition found for %r", symbol)
        return None

    chosen_path, chosen_node = definitions[0]
    if prefer_path:
        wanted = prefer_path.replace("\\", "/")
        for path, node in definitions:
            if _rel(path).endswith(wanted) or wanted.endswith(_rel(path)):
                chosen_path, chosen_node = path, node
                break

    source_lines = chosen_path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = chosen_node.lineno
    end = getattr(chosen_node, "end_lineno", None) or start

    truncated = False
    if (end - start + 1) > MAX_EXCERPT_LINES:
        end = start + MAX_EXCERPT_LINES - 1
        truncated = True

    width = len(str(end))
    excerpt = "\n".join(
        f"{n:>{width}}| {source_lines[n - 1]}"
        for n in range(start, min(end, len(source_lines)) + 1)
    )
    if truncated:
        excerpt += (
            f"\n{'.' * width}| ... [truncated at {MAX_EXCERPT_LINES} lines; "
            f"symbol continues to line {getattr(chosen_node, 'end_lineno', '?')}]"
        )

    references = find_references(symbol, root) if include_references else []

    # Ambiguity: either the name is defined more than once, or it is referenced
    # so widely that name-matching is meaningless. Both mean callers must not
    # treat `references` as a real dependency set.
    reason = ""
    if len(definitions) > 1:
        others = ", ".join(sorted({_rel(p) for p, _ in definitions})[:5])
        reason = (
            f"{len(definitions)} definitions share this name ({others}) — "
            f"references are name-matched, not scope-resolved"
        )
    elif len(references) > AMBIGUITY_REF_THRESHOLD:
        reason = (
            f"{len(references)} references exceed the {AMBIGUITY_REF_THRESHOLD} "
            f"ambiguity threshold — likely a common name matched by coincidence"
        )

    return SymbolEvidence(
        name=symbol,
        relative_path=_rel(chosen_path),
        lineno=start,
        end_lineno=end,
        kind=_kind_of(chosen_node),
        signature=_signature_of(chosen_node, source_lines),
        excerpt=excerpt,
        content_hash=hashlib.sha256(excerpt.encode()).hexdigest()[:16],
        repo_sha=get_repo_sha(),
        truncated=truncated,
        definition_count=len(definitions),
        references=references,
        ambiguity_reason=reason,
    )


def symbol_from_traceback(tb_text: str) -> tuple[str, str] | None:
    """Extract ``(symbol, relative_path)`` from the deepest frame of a traceback.

    The deepest frame is where the exception actually surfaced, so it is scanned
    first — the old mapper matched on filename alone and could not name a symbol
    at all.
    """
    import re

    frames = re.findall(r'File "([^"]+)", line (\d+), in (\S+)', tb_text)
    if not frames:
        return None
    path, _lineno, func = frames[-1]
    if func in ("<module>", "<lambda>", "<listcomp>", "<genexpr>"):
        for p, _l, f in reversed(frames[:-1]):
            if f not in ("<module>", "<lambda>", "<listcomp>", "<genexpr>"):
                path, func = p, f
                break
        else:
            return None
    rel = path.replace("\\", "/")
    for marker in ("/app/", "app/"):
        idx = rel.find(marker)
        if idx != -1:
            rel = rel[idx:].lstrip("/")
            break
    return func, rel


def build_evidence_for_traceback(
    tb_text: str,
    *,
    root: Path | None = None,
) -> SymbolEvidence | None:
    """Resolve a traceback straight to symbol-level evidence.

    Needs no entry in ``target_map``'s hardcoded dicts — that registry could only
    resolve names someone had already added by hand.
    """
    parsed = symbol_from_traceback(tb_text)
    if not parsed:
        return None
    symbol, rel_path = parsed
    return build_symbol_evidence(symbol, prefer_path=rel_path, root=root)


def render_evidence(evidence: SymbolEvidence, *, max_reference_lines: int = 20) -> str:
    """Render evidence as prompt text, with the ambiguity caveat made explicit."""
    lines = [
        f"## CODE EVIDENCE: {evidence.name}",
        f"- file: {evidence.relative_path}:{evidence.lineno}-{evidence.end_lineno}",
        f"- kind: {evidence.kind}",
        f"- signature: {evidence.signature}",
        f"- provenance: repo={evidence.repo_sha} hash={evidence.content_hash}",
    ]
    if evidence.truncated:
        lines.append(f"- NOTE: excerpt truncated at {MAX_EXCERPT_LINES} lines")

    if evidence.is_ambiguous:
        lines += [
            "",
            f"⚠️  AMBIGUOUS SYMBOL — {evidence.ambiguity_reason}.",
            "   Do not treat the reference list as a dependency set; verify each "
            "call site before assuming it reaches this definition.",
        ]

    lines += ["", "### Source", "```python", evidence.excerpt, "```"]

    if evidence.references and not evidence.is_ambiguous:
        shown = evidence.references[:max_reference_lines]
        lines += ["", f"### References ({len(evidence.references)} total)"]
        lines += [f"- {p}:{n}" for p, n in shown]
        if len(evidence.references) > len(shown):
            lines.append(f"- ... and {len(evidence.references) - len(shown)} more")

    return "\n".join(lines)
