"""A table alias that is a Postgres reserved word is a syntax error.

`scripts/decision_score_report.py shadow` shipped in aac14ec aliasing
`decision_outcomes` as `do` — a reserved word (the DO statement) — so the
subcommand raised `SyntaxError: syntax error at or near "do"` before reading a
row. It stayed dead from the moment it shipped until 2026-08-07, because the
only test on that feature covers `compute_decision_score` and never the
reporter. Nothing executes these query strings in the test run.

This scan is a PROXY for execution, not a substitute for it: it catches the one
failure class that turns an unexecuted query into a guaranteed error at the
first real call. A query can still be wrong in ways only running it reveals.

Deliberately NOT written as a `real_db` integration test: `real_db` skips
unless `TRADING_BOT_TEST_DB=1`, so in the default run it would pass without
checking anything — which is the same shape of hole that let the original bug
through ([[a-check-that-passes-for-both-states-is-not-a-check]]).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Reserved in Postgres and NOT usable as a bare table alias (no AS). Not the
# full list — the ones short and natural enough that a person reaches for them
# when abbreviating a table name.
RESERVED = {
    "do", "all", "and", "any", "as", "asc", "case", "cast", "check", "column",
    "constraint", "create", "default", "desc", "distinct", "else", "end",
    "except", "false", "for", "from", "grant", "group", "having", "in",
    "initially", "intersect", "into", "limit", "not", "null", "offset", "on",
    "only", "or", "order", "primary", "references", "select", "some", "table",
    "then", "to", "true", "union", "unique", "user", "using", "when", "where",
    "window", "with",
}

# `FROM tbl alias` / `JOIN tbl alias` — bare alias only. `AS` forms and
# parenthesised subqueries are skipped; this is about the abbreviation habit.
# The `(?<!DISTINCT )` guard is not decoration: `a.x IS NOT DISTINCT FROM b.x
# AND ...` parses as table `b.x` aliased `AND` without it, and `substring(col
# FROM 'pat')` is the same shape. Both are ordinary SQL, and flagging them is
# how a scan gets a reputation for crying wolf.
ALIAS_RE = re.compile(
    r"(?<!DISTINCT )\b(?:FROM|JOIN)\s+([a-zA-Z_][\w.]*)\s+([a-zA-Z_]\w*)\b(?!\s*\()",
    re.IGNORECASE,
)

# Words that follow a table name but are clauses, not aliases.
NOT_AN_ALIAS = {
    "on", "using", "where", "group", "order", "limit", "having", "join",
    "left", "right", "inner", "outer", "full", "cross", "set", "values",
    "returning", "union", "except", "intersect", "for", "offset", "window",
    "as", "natural", "lateral", "tablesample", "with",
    # Boolean/comparison connectives. Nobody aliases a table `and`; seeing one
    # of these means the regex ran past the end of the FROM clause.
    "and", "or", "is", "not", "then", "else", "end", "when", "case",
}

ROOT = Path(__file__).resolve().parents[2]


def _sql_sources() -> list[Path]:
    paths: list[Path] = []
    for base in ("app", "scripts"):
        d = ROOT / base
        if d.is_dir():
            paths.extend(p for p in d.rglob("*.py") if "__pycache__" not in p.parts)
    return paths


# ⚠️ ONLY STRING LITERALS THAT ARE ACTUALLY SQL.
# The first draft ran ALIAS_RE over raw file lines and reported 40+ offenders,
# every one of them English: a docstring saying "the artifacts the Board reads
# in ..." matched `FROM <word> in`. A probe shaped that way condemns working
# code and would have been switched off within a day. Strings are extracted
# with `ast` and kept only when they contain a real SELECT ... FROM.
#
# UPDATE ... FROM AND DELETE ... USING COUNT TOO, and the first version of this
# filter required SELECT — which made it fail open on
# `app/autoresearch/auditors/decision_audit.py`, an UPDATE ... FROM
# decision_outcomes do carrying the identical defect. The narrow filter would
# have shipped a green test over a second live instance of the very bug it was
# written for.
_LOOKS_LIKE_SQL = re.compile(
    r"\bSELECT\b.*?\bFROM\b|\bUPDATE\b.*?\bSET\b|\bDELETE\s+FROM\b",
    re.IGNORECASE | re.DOTALL,
)


def _sql_literals(path: Path):
    """Yield (line_no, sql_text) for every string constant that is SQL."""
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _LOOKS_LIKE_SQL.search(node.value):
                yield node.lineno, node.value


def test_no_reserved_word_table_aliases():
    offenders: list[str] = []
    for path in _sql_sources():
        for line_no, sql in _sql_literals(path):
            for _table, alias in ALIAS_RE.findall(sql):
                low = alias.lower()
                if low in NOT_AN_ALIAS or low not in RESERVED:
                    continue
                offenders.append(
                    f"{path.relative_to(ROOT)}:{line_no} aliased as "
                    f"`{alias}` — reserved in Postgres, the query cannot parse"
                )

    assert not offenders, (
        "reserved-word table aliases found; these queries raise a SyntaxError "
        "on their first real execution:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_catches_the_original_bug(tmp_path):
    """A guard that cannot fail is not a guard.

    Pins the exact shape of aac14ec's defect so a future rewrite of the regex
    cannot quietly stop matching it.
    """
    sample = tmp_path / "probe.py"
    sample.write_text(
        'q = """\n'
        "SELECT ds.band, do.pnl_pct\n"
        "  FROM decision_scores ds\n"
        "  LEFT JOIN decision_outcomes do\n"
        "         ON do.ticker = ds.ticker\n"
        '"""\n',
        encoding="utf-8",
    )
    hits = [
        alias
        for line in sample.read_text().splitlines()
        for _t, alias in ALIAS_RE.findall(line)
        if alias.lower() in RESERVED
    ]
    assert hits == ["do"], f"the scan no longer catches the original bug: {hits}"


def test_the_fixed_query_passes():
    """And the repaired form must NOT trip it, or the fix is unshippable."""
    fixed = (
        "  FROM decision_scores ds\n"
        "  LEFT JOIN decision_outcomes outcome\n"
        "         ON outcome.ticker = ds.ticker\n"
    )
    hits = [
        alias
        for line in fixed.splitlines()
        for _t, alias in ALIAS_RE.findall(line)
        if alias.lower() in RESERVED
    ]
    assert hits == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
