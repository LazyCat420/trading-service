"""Repo-wide guard: every price_history READ must pin one vendor.

`price_history` has primary key `(ticker, date, source)` — see
`app/db/schema_pg.sql`. One ticker-date can therefore carry several vendor
prints, and the vendors do NOT agree: measured 2026-07-29, 9,225 dual-source
ticker-dates across 38 tickers with a mean absolute close difference of
**20.05%** (yfinance publishes dividend/split-adjusted closes, polygon raw).

An unfiltered read is wrong in two directions at once:

  * two prints of the SAME date pair into a near-zero return and dilute
    variance (CRH annualized vol: 25.18% mixed vs 32.44% pinned), and any
    `LIMIT n` returns n ROWS spanning ~n/2 DATES
  * alternating conventions across dates manufacture jumps that never
    happened (DRIP: 133 daily moves over 15% mixed, 1 pinned)

WHY THIS TEST EXISTS RATHER THAN A NAMED-MODULE CHECK
-----------------------------------------------------
The previous guard (`test_forward_window_source.py`) named `outcome_tracker`
and `agent_scorecard` specifically. That is why the identical bug was still
live in `challenger.py` (a second, independent outcome tracker),
`technical_processor.py` (feeding every desk's RSI/ATR), `factors.py`,
`regime_hmm.py` and `quant_edge_verifier.py` (feeding `run_equation`) on
2026-07-30 — a new unfiltered read could not fail any test.

This guard scans EVERY module instead, so a new read has to opt out
explicitly, in writing, with a reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("app", "scripts")

# Reads that genuinely do not need a vendor pin. Every entry needs a reason;
# "it was already like that" is not one. Keyed by path suffix.
VENDOR_AGNOSTIC: dict[str, str] = {
    # Measures the vendor split ITSELF: it counts (ticker,date) days carried by
    # more than one source, so pinning a single vendor would make the number it
    # reports always zero. Reads nothing into a trading decision.
    "scripts/quality_census.py": "audit — counts multi-vendor coverage by design",
    # Writers: these POPULATE price_history, so `source` is a column they set,
    # not a filter they apply.
    "scripts/backfill_price_history.py": "writer — inserts rows, sets source",
    "scripts/populate_sp500.py": "writer — inserts rows, sets source",
    "app/collectors/yfinance_collector.py": "writer — inserts rows, sets source",
    "app/data/sp500_price_collector.py": "writer — inserts rows, sets source",
}

# Query shapes that are vendor-immune by construction. A COUNT over DISTINCT
# dates cannot be inflated by a second vendor print of the same date, and a
# cross-ticker market-calendar scan has no single "dominant" vendor to pick.
_VENDOR_IMMUNE = (
    re.compile(r"count\s*\(\s*distinct\s+date", re.I),
    re.compile(r"select\s+distinct\s+date", re.I),
    re.compile(r"max\s*\(\s*date\s*\)", re.I),
    re.compile(r"min\s*\(\s*date\s*\)", re.I),
)

# What counts as pinning a vendor.
_PINNED = (
    re.compile(r"dominant_source_sql", re.I),
    re.compile(r"\bsource\s*=", re.I),
    re.compile(r"\bsource\s+in\b", re.I),
    re.compile(r"group\s+by\s+source", re.I),
)

# ── the ratchet ──────────────────────────────────────────────────────
#
# Unpinned reads that existed when this guard was written, with the count of
# offending queries per file. They are NOT approved — they are a backlog, and
# the guard's job is to stop it growing while it is worked down.
#
# The count is the ratchet: a file may only ever get BETTER. Fixing a query
# means lowering its number; reaching 0 means deleting the entry. Adding a new
# unpinned read to any of these files fails the test, and so does adding one to
# a file not listed here at all.
#
# Deliberately not a blanket skip. Each of these is a real, unverified read
# against a table where `source` is part of the primary key; several are on
# live paths (`paper_trader`, `portfolio`, `scoring_engine`, `orchestrator`).
# They are listed rather than silently allowed so the debt is countable.
#
# The seven modules fixed on 2026-07-30 — returns, factors, regime_hmm,
# technical_baseline, technical_processor, challenger, quant_edge_verifier —
# are deliberately absent: they must never regress into this list.
#
# returns_engine.py (was 2) and sector_aggregator.py (was 1) left the list on
# 2026-08-18 with the Mongo port. Their reads are now pinned in pandas via
# keep_dominant_source() rather than in SQL — the debt was PAID, not moved out
# of the scanner's sight, which a port off SQL can otherwise do for free.
KNOWN_UNPINNED: dict[str, int] = {
    "app/autoresearch/auditors/data_audit.py": 1,
    "app/cognition/evaluation/oracle.py": 1,
    "app/processors/data_sanity.py": 2,
    "app/processors/quant_processor.py": 7,
    "app/services/boot_service.py": 2,
    "app/services/cycle_scheduler.py": 1,
    "app/tools/market_tools.py": 1,
    "app/trading/backtest_data.py": 2,
    "app/trading/paper_trader.py": 2,
    "app/v3/invariants.py": 1,
    "scripts/confidence_audit.py": 1,
    "scripts/cycle_healthcheck.py": 1,
    "scripts/factor_backtest.py": 1,
    "scripts/gate_ablation.py": 1,
    "scripts/mine_shkreli_doctrine.py": 1,
    "scripts/residual_alpha_report.py": 2,
    "scripts/score_tournament_ranker.py": 1,
    "scripts/simulate_freshness_thresholds.py": 3,
}

_FROM_PRICE_HISTORY = re.compile(r"from\s+price_history", re.I)
_IS_WRITE = re.compile(r"insert\s+into\s+price_history|update\s+price_history"
                       r"|delete\s+from\s+price_history", re.I)

# A read is a SELECT against the table. Requiring the verb keeps prose out:
# `technical_processor.py`'s own docstring says "compute indicators from
# price_history", and matching that instead of a query is precisely the
# "tests that match prose" failure this repo has already been bitten by.
_IS_SELECT = re.compile(r"\bselect\b", re.I)

# A multi-ticker read cannot use the SQL filter (there is no single dominant
# vendor across tickers), so it selects the `source` column and pins per-ticker
# in pandas via keep_dominant_source(). That IS pinned — just not in the SQL.
_SELECTS_SOURCE = re.compile(r"select[^;]*?\bsource\b[^;]*?\bfrom\s+price_history", re.I)


def _reads_price_history(text: str) -> bool:
    return bool(_FROM_PRICE_HISTORY.search(text) and _IS_SELECT.search(text))


def _sql_literals(path: Path) -> list[tuple[int, str]]:
    """Every string literal in `path`, f-strings reconstructed.

    An f-string carrying `{dominant_source_sql()}` is the pinned form, so the
    interpolated expression source has to be part of the text we match against
    or the guard would report a false positive on exactly the fixed code.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    try:
                        parts.append(ast.unparse(v.value))
                    except Exception:  # noqa: BLE001 - best effort
                        parts.append(" ")
            out.append((node.lineno, "".join(parts)))
    return out


def _unpinned_reads(path: Path) -> list[tuple[int, str]]:
    """Reads of price_history in `path` that do not pin a vendor."""
    try:
        module_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    pins_in_pandas = "keep_dominant_source" in module_text

    bad = []
    for lineno, text in _sql_literals(path):
        if not _reads_price_history(text):
            continue
        if _IS_WRITE.search(text):
            continue
        if any(p.search(text) for p in _PINNED):
            continue
        if any(p.search(text) for p in _VENDOR_IMMUNE):
            continue
        if pins_in_pandas and _SELECTS_SOURCE.search(text):
            continue
        bad.append((lineno, " ".join(text.split())[:140]))
    return bad


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        files.extend(sorted((REPO / root).rglob("*.py")))
    return [f for f in files if "__pycache__" not in f.parts]


def test_the_scanner_actually_finds_queries():
    """A guard that silently matches nothing passes forever.

    If the AST walk or the regexes break, every other test in this file goes
    green while checking nothing. Pin a floor on what the scan must see.
    """
    total = sum(
        1
        for f in _python_files()
        for _, text in _sql_literals(f)
        if _reads_price_history(text)
    )
    # Floor lowered 25 → 18 on 2026-08-18: the Mongo port moved real reads
    # (returns_engine, sector_aggregator) out of SQL literals and into
    # mongo_query calls, so the SQL-literal count fell to 19 legitimately. The
    # negative control above still passes, which is what distinguishes "fewer
    # SQL reads exist" from "the walk stopped matching".
    #
    # NOTE for whoever finishes the migration: this floor measures SQL literals
    # only, so it decays toward 0 as tables cut over — and at 0 every other test
    # in this file goes green while checking nothing. The vendor rule is a
    # property of price_history, not of Postgres; the guard needs a Mongo-side
    # scan (a find_rows/join_rows on "price_history" that neither filters
    # `source` nor routes through keep_dominant_source) before the last SQL
    # reader leaves.
    assert total >= 18, (
        f"scanner found only {total} price_history reads — it is broken, "
        "not the codebase that is clean"
    )


def test_the_scanner_flags_a_known_bad_query():
    """Negative control: the guard must reject the shape it exists to catch."""
    bad = "SELECT close FROM price_history WHERE ticker = %s ORDER BY date DESC LIMIT 1"
    assert not any(p.search(bad) for p in _PINNED)
    assert not any(p.search(bad) for p in _VENDOR_IMMUNE)
    assert _reads_price_history(bad)

    good = bad.replace("WHERE ticker = %s", "WHERE ticker = %s AND source = (x)")
    assert any(p.search(good) for p in _PINNED)


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_every_price_history_read_pins_one_vendor(path: Path):
    rel = str(path.relative_to(REPO))
    if rel in VENDOR_AGNOSTIC:
        pytest.skip(f"allow-listed: {VENDOR_AGNOSTIC[rel]}")

    bad = _unpinned_reads(path)
    budget = KNOWN_UNPINNED.get(rel, 0)
    detail = "\n".join(f"  line {n}: {q}" for n, q in bad)

    if len(bad) > budget:
        pytest.fail(
            f"{rel} has {len(bad)} unpinned price_history read(s), budget {budget}:\n"
            f"{detail}\n\n"
            "Use app.quant.returns.dominant_source_sql() (single ticker) or "
            "keep_dominant_source() (multi-ticker), placing the filter INSIDE "
            "any subquery that carries a LIMIT. If the read is genuinely "
            "vendor-agnostic, add it to VENDOR_AGNOSTIC with a reason."
        )

    # The ratchet only works if it tightens. A file that got fixed but kept its
    # old budget would silently leave room for the bug to come back.
    assert len(bad) == budget, (
        f"{rel} now has {len(bad)} unpinned read(s) but KNOWN_UNPINNED still "
        f"budgets {budget}. Lower it to {len(bad)} "
        f"({'or delete the entry' if not bad else 'to lock the fix in'})."
    )
