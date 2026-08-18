"""
Symbol-level code evidence for the repair loop.

The behaviours worth protecting here are the ones the old path got wrong:
resolving a target with no hand-written map entry, cutting source at a symbol
boundary instead of a byte offset, and admitting when name-matching cannot be
trusted.
"""
import textwrap

import pytest

from app.cognition.evolution.code_evidence import (
    AMBIGUITY_REF_THRESHOLD,
    MAX_EXCERPT_LINES,
    build_evidence_for_traceback,
    build_symbol_evidence,
    find_definitions,
    find_references,
    render_evidence,
    symbol_from_traceback,
)


@pytest.fixture
def tiny_repo(tmp_path):
    """A miniature tree so assertions do not drift with the real codebase."""
    pkg = tmp_path / "app" / "svc"
    pkg.mkdir(parents=True)
    (pkg / "core.py").write_text(textwrap.dedent('''
        """Module docstring mentioning persist_metrics as prose only."""

        class Widget:
            def persist_metrics(self, rows):
                """Docstring."""
                total = 0
                for r in rows:
                    total += r
                return total

        def helper():
            w = Widget()
            return w.persist_metrics([1, 2])
    ''').lstrip())
    (pkg / "other.py").write_text(textwrap.dedent('''
        from app.svc.core import Widget

        def caller():
            return Widget().persist_metrics([3])
    ''').lstrip())
    return tmp_path


def test_finds_a_method_not_just_top_level_defs(tiny_repo):
    """The old extractor only scanned module-level defs, so methods were invisible."""
    defs = find_definitions("persist_metrics", root=tiny_repo)
    assert len(defs) == 1
    assert defs[0][0].name == "core.py"


def test_references_exclude_prose(tiny_repo):
    """A docstring mention is not a reference — the one place plain grep was wrong."""
    refs = find_references("persist_metrics", root=tiny_repo)
    assert len(refs) == 3          # def + two call sites
    assert all(line != 1 for _, line in refs)   # line 1 is the module docstring


def test_excerpt_is_symbol_bounded_not_byte_bounded(tiny_repo):
    ev = build_symbol_evidence("persist_metrics", root=tiny_repo)
    assert ev is not None
    # Starts at the def and ends at the end of the method, not at a byte offset.
    assert "def persist_metrics" in ev.excerpt
    assert "return total" in ev.excerpt
    # It must NOT bleed into the following function.
    assert "def helper" not in ev.excerpt
    assert ev.end_lineno > ev.lineno
    assert ev.kind == "function"


def test_excerpt_is_line_numbered_with_provenance(tiny_repo):
    ev = build_symbol_evidence("persist_metrics", root=tiny_repo)
    assert "|" in ev.excerpt                     # line numbers present
    assert ev.content_hash and len(ev.content_hash) == 16
    assert ev.relative_path.endswith("core.py")


def test_unknown_symbol_returns_none(tiny_repo):
    assert build_symbol_evidence("no_such_symbol", root=tiny_repo) is None


def test_distinctive_symbol_is_not_flagged_ambiguous(tiny_repo):
    ev = build_symbol_evidence("persist_metrics", root=tiny_repo)
    assert ev.definition_count == 1
    assert not ev.is_ambiguous
    assert ev.ambiguity_reason == ""


def test_duplicate_definitions_flag_ambiguity(tmp_path):
    """Name-matching cannot resolve scope — say so rather than emit a superset."""
    pkg = tmp_path / "app"
    pkg.mkdir()
    for i, name in enumerate(("a.py", "b.py", "c.py")):
        (pkg / name).write_text(f"def execute():\n    return {i}\n")

    ev = build_symbol_evidence("execute", root=tmp_path)
    assert ev.definition_count == 3
    assert ev.is_ambiguous
    assert "3 definitions" in ev.ambiguity_reason


def test_high_reference_count_flags_ambiguity(tmp_path):
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "d.py").write_text("def run():\n    return 1\n")
    body = "\n".join(f"x{i} = run()" for i in range(AMBIGUITY_REF_THRESHOLD + 5))
    (pkg / "e.py").write_text(f"from app.d import run\n{body}\n")

    ev = build_symbol_evidence("run", root=tmp_path)
    assert ev.definition_count == 1          # only one def...
    assert ev.is_ambiguous                   # ...but too many refs to trust
    assert "threshold" in ev.ambiguity_reason


def test_ambiguous_evidence_withholds_the_reference_list(tmp_path):
    """Rendering must not hand a model 1000 name-matched 'callers' as fact."""
    pkg = tmp_path / "app"
    pkg.mkdir()
    for i, name in enumerate(("a.py", "b.py")):
        (pkg / name).write_text(f"def execute():\n    return {i}\n")

    ev = build_symbol_evidence("execute", root=tmp_path)
    rendered = render_evidence(ev)
    assert "AMBIGUOUS SYMBOL" in rendered
    assert "### References" not in rendered


def test_unambiguous_evidence_includes_references(tiny_repo):
    ev = build_symbol_evidence("persist_metrics", root=tiny_repo)
    rendered = render_evidence(ev)
    assert "AMBIGUOUS SYMBOL" not in rendered
    assert "### References" in rendered
    assert "### Source" in rendered


def test_enormous_symbol_is_truncated_at_a_line_boundary(tmp_path):
    pkg = tmp_path / "app"
    pkg.mkdir()
    body = "\n".join(f"    x{i} = {i}" for i in range(MAX_EXCERPT_LINES + 50))
    (pkg / "big.py").write_text(f"def huge():\n{body}\n")

    ev = build_symbol_evidence("huge", root=tmp_path)
    assert ev.truncated
    assert "truncated at" in ev.excerpt
    assert len(ev.excerpt.splitlines()) <= MAX_EXCERPT_LINES + 2


# ── traceback resolution ──────────────────────────────────────────────────────

TRACEBACK = '''Traceback (most recent call last):
  File "/app/cycle_main.py", line 40, in run_single_cycle
    await PipelineService.start_cycle()
  File "/app/app/services/pipeline_service.py", line 300, in _persist_summary
    rows = build(summary)
  File "/app/app/svc/core.py", line 4, in persist_metrics
    total += r
TypeError: unsupported operand type(s) for +=: 'int' and 'str'
'''


def test_traceback_resolves_to_the_deepest_frame():
    """The deepest frame is where the exception surfaced."""
    parsed = symbol_from_traceback(TRACEBACK)
    assert parsed is not None
    symbol, rel = parsed
    assert symbol == "persist_metrics"
    assert rel.endswith("app/svc/core.py")


def test_traceback_skips_synthetic_frames():
    tb = '''Traceback (most recent call last):
  File "/app/app/v3/orchestrator.py", line 10, in real_function
    boom()
  File "/app/app/v3/orchestrator.py", line 20, in <listcomp>
    [x for x in y]
'''
    symbol, _ = symbol_from_traceback(tb)
    assert symbol == "real_function"


def test_no_frames_returns_none():
    assert symbol_from_traceback("ValueError: nope") is None


def test_traceback_to_evidence_needs_no_target_map_entry(tiny_repo):
    """The whole point: resolution without a hand-written registry entry."""
    ev = build_evidence_for_traceback(TRACEBACK, root=tiny_repo)
    assert ev is not None
    assert ev.name == "persist_metrics"
    assert ev.relative_path.endswith("core.py")
    assert "total += r" in ev.excerpt


def test_evidence_is_smaller_than_the_old_truncation_budget(tiny_repo):
    """The old path emitted up to 8000 + 4000 chars of byte-sliced file."""
    ev = build_symbol_evidence("persist_metrics", root=tiny_repo)
    assert len(render_evidence(ev)) < 8000
