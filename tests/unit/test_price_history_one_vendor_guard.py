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
# The nine entries that stood here on 2026-08-18 — data_audit, oracle,
# data_sanity, quant_processor, boot_service, market_tools, backtest_data,
# paper_trader, invariants — all measured 0 after the Mongo port and were
# removed. That is NOT nine fixes: every one of those reads still exists, it
# just moved from a SQL literal into a `mongo_query`/`mongo_store` call this
# scanner could not see. The debt is now counted by KNOWN_UNPINNED_MONGO below,
# which is where those files reappear.
KNOWN_UNPINNED: dict[str, int] = {
    # 0 as of the Mongo conversion: the one unpinned read was the finviz
    # supplement's EXISTS price-freshness subquery, which now spells the
    # source filter out explicitly. Ratchet lowered, per this test's own
    # instruction — do not raise it again.
    "app/services/cycle_scheduler.py": 0,
    "scripts/confidence_audit.py": 1,
    # cycle_healthcheck left this list on 2026-08-19 with its Mongo port: the
    # SQL freshness probe became a distinct-ticker count, which is
    # vendor-immune by construction rather than merely out of the SQL
    # scanner's sight (the Mongo scan checks the same file).
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


# ── the Mongo-side scan ──────────────────────────────────────────────
#
# Added 2026-08-18, because the SQL scan above had begun to decay exactly the
# way its own NOTE warned: nine files dropped to a 0 SQL budget on the Mongo
# port without a single query being fixed. The reads moved into
# `mongo_query.*` / `mongo_store.*` calls, which no regex over SQL literals can
# see, so the guard reported the debt as PAID.
#
# The vendor rule is a property of `price_history` — one ticker-date carries
# several vendor prints and they disagree by 20% on average — not a property of
# Postgres. It therefore has to be enforced on whichever client reads the
# collection.

#: Helpers on the Mongo layer that WRITE. `source` is a field they set, not a
#: filter they apply, so they are not reads.
_MONGO_WRITES = frozenset({
    "insert_docs", "upsert_doc", "update_docs", "delete_docs",
    "find_one_and_update", "bulk_write",
})

#: Aggregations that cannot be inflated by a second vendor print of the same
#: date — the Mongo counterpart of `_VENDOR_IMMUNE`. A MAX(date) is the same
#: date whichever vendor printed it.
_MONGO_IMMUNE_AGGS = frozenset({"max", "min", "count_distinct"})


def _pins_source(call_src: str) -> bool:
    """Does this call name the `source` field anywhere in its arguments?

    Deliberately generous: a filter, a `$group` key, an `$in`, a projection
    that feeds `keep_dominant_source` — all count. The guard's job is to catch
    reads that never consider the vendor at all, which is the shape that
    actually shipped.

    `_one_vendor(...)` counts too. It is the canonical pin helper from
    `app.quant.returns` — it resolves the dominant vendor and merges
    `{"source": src}` into the filter — so a read wrapped in it is pinned even
    though the literal `source` no longer appears at the call site. Without
    this the scanner condemns correctly-pinned code, which is exactly the
    false positive that made `technical_processor.py` look like a regression.
    """
    if "'source'" in call_src or '"source"' in call_src:
        return True
    return "_one_vendor(" in call_src


def _unpinned_mongo_reads(path: Path) -> list[tuple[int, str]]:
    """Reads of the `price_history` COLLECTION that do not pin a vendor."""
    try:
        module_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(module_text)
    except (SyntaxError, UnicodeDecodeError):
        return []

    # A module that pins per-ticker in pandas is pinned, same rule the SQL
    # scan applies — the filter is just downstream of the read.
    if "keep_dominant_source" in module_text:
        return []

    bad: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            continue
        if not (isinstance(fn.value, ast.Name)
                and fn.value.id in ("mongo_query", "mongo_store")):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "price_history"):
            continue
        if fn.attr in _MONGO_WRITES:
            continue

        src = " ".join(ast.unparse(node).split())
        if _pins_source(src):
            continue
        # `distinct_values('price_history', 'ticker'|'date', ...)` is
        # vendor-immune by construction: a second vendor's print for the same
        # ticker-day adds a duplicate ROW, and a distinct set of tickers or
        # dates cannot be changed by a duplicate. Pinning a vendor there would
        # make a coverage/freshness count report the coverage of ONE vendor
        # while claiming to report the store's.
        if (fn.attr == "distinct_values" and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in ("ticker", "date")):
            continue
        # A pure MAX/MIN/COUNT DISTINCT over `date` is vendor-immune.
        if fn.attr == "agg_row" and all(
            op in _MONGO_IMMUNE_AGGS
            for op in re.findall(r"\('(\w+)'", src)
        ) and re.findall(r"\('(\w+)'", src):
            continue

        bad.append((node.lineno, src[:140]))
    return sorted(bad)


# Measured 2026-08-18 by this scanner. Same ratchet contract as KNOWN_UNPINNED:
# a file may only ever get BETTER, and reaching 0 means deleting the entry.
#
# These are NOT approved. Several are on live decision paths — `paper_trader`
# marks the book, `scoring_engine` and `orchestrator` price every desk,
# `portfolio` values the positions — and every one of them takes the newest row
# by date with no vendor filter, so which vendor answers depends on which one
# published last.
#
# Note especially `challenger.py`, `quant_edge_verifier.py`, `regime_hmm.py`,
# `technical_processor.py` and `technical_baseline.py`: those are five of the
# seven modules fixed on 2026-07-30 that the header above says "must never
# regress into this list". They regressed. The SQL fix was real; the Mongo port
# reintroduced the unpinned read underneath it.
KNOWN_UNPINNED_MONGO: dict[str, int] = {
    "app/autoresearch/auditors/data_audit.py": 3,
    "app/cognition/evaluation/oracle.py": 1,
    "app/cognition/evidence/packet_builder.py": 1,
    "app/collectors/data_rotator.py": 1,
    "app/processors/data_sanity.py": 1,
    "app/processors/market_regime.py": 3,
    "app/processors/quant_processor.py": 2,
    # Was budgeted 2 on the reading that its Mongo port had dropped the pin.
    # It had not: both indicator reads go through `_one_vendor(...)`, and the
    # scanner's text grep simply could not see through that helper. The one
    # remaining read is the dominant-vendor CENSUS itself (`$group` on
    # `$source`), which must stay vendor-agnostic to do its job.
    "app/processors/technical_processor.py": 1,
    "app/quant/regime_grading.py": 1,
    "app/quant/regime_hmm.py": 1,
    # 3 -> 2 on 2026-08-19: one of the three was a `distinct_values` over
    # tickers, which the scanner now recognises as vendor-immune by
    # construction (a duplicate vendor row cannot change a distinct set).
    "app/quant/technical_baseline.py": 2,
    "app/routers/market_router.py": 2,
    # 2 -> 1 on 2026-08-28: NOT a fix. BootService carried duplicate copies of
    # the FRED/market/SP500 startup tasks; they were consolidated into their
    # one owner, app/services/startup_tasks.py, and the SP500 seed's
    # price_history existence COUNT moved with the code. The entry below for
    # startup_tasks.py is that same read, not a new one -- the repo-wide total
    # is unchanged. (The read is `count` over all of price_history, asking only
    # "is there any price data at all"; like the `distinct_values` case noted
    # above it cannot be changed by a duplicate vendor row, but the scanner
    # reads text, not intent.)
    "app/services/boot_service.py": 1,
    "app/services/startup_tasks.py": 1,
    "app/tools/market_tools.py": 1,
    "app/trading/backtest_data.py": 1,
    "app/trading/paper_trader.py": 3,
    "app/trading/portfolio.py": 1,
    "app/trading/quant_edge_verifier.py": 1,
    "app/trading/scoring_engine.py": 2,
    "app/trading/watchlist.py": 1,
    "app/v3/challenger.py": 1,
    "app/v3/invariants.py": 1,
    "app/v3/orchestrator.py": 1,
}


def test_the_mongo_scanner_actually_finds_reads():
    """The same floor the SQL scan carries, for the same reason.

    This is the check that would have caught the decay: when nine files went to
    a 0 SQL budget, the collection was still being read 36 times.
    """
    total = sum(len(_unpinned_mongo_reads(f)) + 0 for f in _python_files())
    found = sum(
        1
        for f in _python_files()
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in ("mongo_query", "mongo_store")
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "price_history"
    )
    assert found >= 30, (
        f"scanner found only {found} price_history Mongo calls — it is broken, "
        "not the codebase that is clean"
    )
    assert total >= 1, "every read pins a vendor, which has never been true"


def test_the_mongo_scanner_flags_a_known_bad_call(tmp_path):
    """Negative control: the guard must reject the shape it exists to catch,
    and accept the pinned form of the SAME call."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from app.db import mongo_query\n"
        "def f(t):\n"
        "    return mongo_query.find_row('price_history', {'ticker': t},\n"
        "                                ['close'], sort=[('date', -1)])\n"
    )
    assert len(_unpinned_mongo_reads(bad)) == 1

    good = tmp_path / "good.py"
    good.write_text(
        "from app.db import mongo_query\n"
        "def f(t):\n"
        "    return mongo_query.find_row('price_history',\n"
        "                                {'ticker': t, 'source': 'yfinance'},\n"
        "                                ['close'], sort=[('date', -1)])\n"
    )
    assert _unpinned_mongo_reads(good) == []

    writer = tmp_path / "writer.py"
    writer.write_text(
        "from app.db import mongo_store\n"
        "def f(docs):\n"
        "    return mongo_store.insert_docs('price_history', docs)\n"
    )
    assert _unpinned_mongo_reads(writer) == []


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_every_mongo_price_history_read_pins_one_vendor(path: Path):
    rel = str(path.relative_to(REPO))
    if rel in VENDOR_AGNOSTIC:
        pytest.skip(f"allow-listed: {VENDOR_AGNOSTIC[rel]}")

    bad = _unpinned_mongo_reads(path)
    budget = KNOWN_UNPINNED_MONGO.get(rel, 0)
    detail = "\n".join(f"  line {n}: {q}" for n, q in bad)

    if len(bad) > budget:
        pytest.fail(
            f"{rel} has {len(bad)} unpinned price_history Mongo read(s), "
            f"budget {budget}:\n{detail}\n\n"
            "Add the dominant vendor to the filter, or select `source` and pin "
            "per-ticker with app.quant.returns.keep_dominant_source(). If the "
            "read is genuinely vendor-agnostic, add it to VENDOR_AGNOSTIC with "
            "a reason."
        )

    assert len(bad) == budget, (
        f"{rel} now has {len(bad)} unpinned Mongo read(s) but "
        f"KNOWN_UNPINNED_MONGO still budgets {budget}. Lower it to {len(bad)} "
        f"({'or delete the entry' if not bad else 'to lock the fix in'})."
    )
