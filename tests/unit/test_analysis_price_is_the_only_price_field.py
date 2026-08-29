"""`price_at_analysis` is a migration leftover that nothing writes.

app/services/result_saver.py stores the analysis-time price as `analysis_price`
and app/services/freshness_gate.py reads it back under that name. A second
column, `price_at_analysis`, came across in the Postgres migration and has NEVER
had a writer.

Measured against the live store on 2026-08-28:

    analysis_price      exists on 5,277 docs, NON-NULL on 982
    price_at_analysis   exists on 5,102 docs, NON-NULL on 0

Both fields sit on the same document, so a reader that picked the wrong one got
`None` forever with nothing to indicate a mistake. Three readers had:
verdict_service (twice) and morning_briefing -- the last of which handed the
briefing prompt an empty string for every ticker, every day.

This reads the source, because the property is structural: a reader either
names the field with a writer or it does not.
"""
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[2] / "app"

# Modules allowed to mention the dead name at all (in prose explaining it).
_COMMENT_ONLY = {"morning_briefing.py"}


def _python_sources():
    for p in sorted(_APP.rglob("*.py")):
        if "scraper" in p.parts:      # build-copy for scraper-service
            continue
        yield p


def _code_lines(path: Path):
    """Lines with the comment stripped, so prose about the bug is allowed."""
    for i, raw in enumerate(path.read_text().splitlines(), 1):
        code = raw.split("#", 1)[0]
        if code.strip():
            yield i, code


def test_no_module_reads_the_field_with_no_writer():
    offenders = []
    for path in _python_sources():
        for lineno, code in _code_lines(path):
            if "price_at_analysis" in code:
                offenders.append(f"{path.relative_to(_APP.parent)}:{lineno}")
    assert not offenders, (
        "`price_at_analysis` is read again in " + ", ".join(offenders)
        + ". Nothing writes it -- it is non-null on zero documents. The stored "
        "field is `analysis_price`."
    )


def test_result_saver_still_writes_the_name_everyone_reads():
    """Vacuity guard: if the writer is renamed, the guard above must not simply
    go quiet -- this fails and points at the rename."""
    src = (_APP / "services" / "result_saver.py").read_text()
    assert '"analysis_price"' in src, (
        "result_saver no longer writes `analysis_price`; every reader fixed on "
        "2026-08-28 now names a field with no writer"
    )


def test_freshness_gate_reads_the_same_name():
    src = (_APP / "services" / "freshness_gate.py").read_text()
    assert "analysis_price" in src
