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

# Reads that genuinely do not need a vendor pin — with the MEASURED count of
# reads each one is allowed to have.
#
# ⚠ This was `dict[str, str]` and a bare `pytest.skip` until 2026-09-12, and
# that is strictly weaker than the ratchet it replaced. `KNOWN_UNPINNED_MONGO`
# asserted `len(bad) == budget`, so a NEW unpinned read in a listed file failed;
# a skip stops reading the file at all. Proven by sabotage: with the skip in
# place, deleting BOTH vendor pins from `technical_processor.py` — the module
# that serves `financial_technical_snapshot.close` to the agents — left the
# suite green at 1269 passed. An exemption must cost a number, or it is a
# blindfold. See [[an-allowlist-can-drift-both-ways-and-keep-its-count]].
#
# Entries are `(sql_reads, mongo_reads, reason)` and both counts are asserted
# EXACTLY, in both directions: a new unpinned read reds, and so does fixing one
# without lowering the number, which keeps the reason honest.
#
# Nine entries that stood here on 2026-09-12 are gone because the SCANNER was
# taught their shape instead — a `$group` on `$source` is the vendor census, a
# MAX/MIN `agg_row` is vendor-immune, and a writer sets `source` rather than
# filtering on it. A rule the scanner understands is worth more than a file it
# is told to ignore.
VENDOR_AGNOSTIC: dict[str, tuple[int, int, str]] = {
    # Measures the vendor split ITSELF: it counts (ticker,date) days carried by
    # more than one source, so pinning a single vendor would make the number it
    # reports always zero. Reads nothing into a trading decision.
    "scripts/quality_census.py": (1, 0, "audit — counts multi-vendor coverage by design"),
    # ---- reads that genuinely span vendors --------------------------------
    # Each reports a property OF the collection, not a price fed to a decision.
    # Pinning them would make the number they report wrong in the other
    # direction, which is why they are named here instead of budgeted.
    "app/processors/data_sanity.py": (0, 1, "counts BAD rows (close <= 0) across the whole collection — pinning would hide half the defects it exists to find"),
    "app/services/startup_tasks.py": (0, 1, "row count answering 'is there any price data at all' — a vendor cannot change whether the answer is zero"),
    "app/services/boot_service.py": (0, 1, "same count, scoped to today — 'did the collectors run', not 'what is the price'"),
    "app/quant/technical_baseline.py": (0, 1, "count_docs(ticker) answering 'does this ticker have any history' — a gate, not a price"),
    "app/v3/invariants.py": (0, 1, "count of rows newer than a cutoff — a freshness invariant, satisfied by ANY vendor"),
    "app/autoresearch/auditors/data_audit.py": (0, 3, "coverage audit — row counts, the date-gap scan and the newest-bar probe. A day carried by either vendor is not a gap, so pinning would INVENT gaps"),
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
    # cycle_healthcheck left this list on 2026-08-19 with its Mongo port: the
    # SQL freshness probe became a distinct-ticker count, which is
    # vendor-immune by construction rather than merely out of the SQL
    # scanner's sight (the Mongo scan checks the same file).
    # 0 as of 2026-08-30, all four below: the Mongo port took their SQL
    # literals with it and the reads now go through `app.quant.returns`'
    # `_one_vendor` / `keep_dominant_source`, which is the canonical pin. They
    # are NOT gone — the Mongo scan sees them, and the combined floor above
    # confirms it: 15 SQL + 61 Mongo before, 3 SQL + 73 Mongo after, both 76.
    # Three of the four (residual_alpha_report, regime_overlay_backtest,
    # vol_forecast_race) each replaced TWO direct reads with one call through a
    # shared helper that reads once, and the +1s elsewhere covered it.
    #
    # residual_alpha_report's port also closed a real hole rather than moving
    # it: the old `LIMIT sessions` returned n ROWS spanning about n/2 DATES on a
    # dual-vendor name, and 107 of the 255 tickers decided on since 2026-05-01
    # carry two vendors.
    "scripts/factor_backtest.py": 0,
    "scripts/gate_ablation.py": 0,
    # 0 as of 2026-08-30: the SQL `SELECT ticker FROM fundamentals UNION SELECT
    # ticker FROM price_history` became two distinct_values calls. A
    # distinct-ticker list is vendor-immune by construction — the same reason
    # cycle_healthcheck left this list — so the read moves to the Mongo scan
    # below and needs no budget there either.
    "scripts/mine_shkreli_doctrine.py": 0,
    "scripts/residual_alpha_report.py": 0,
    "scripts/simulate_freshness_thresholds.py": 0,
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


def _sql_literal_read_count() -> int:
    return sum(
        1
        for f in _python_files()
        for _, text in _sql_literals(f)
        if _reads_price_history(text)
    )


def test_the_scanner_actually_finds_queries():
    """A guard that silently matches nothing passes forever.

    If the AST walk or the regexes break, every other test in this file goes
    green while checking nothing. Pin a floor on what the scan must see.

    The floor is on the COMBINED population — SQL literals plus Mongo call
    sites — for the reason the note below spent four revisions arriving at:
    a PORT does not remove a read, it moves it from one scanner to the other.
    A floor on the SQL half alone therefore falls every time the migration
    makes progress, and it reached 15 with the last three SQL readers of
    price_history sitting inside one script that was about to be converted.
    Counting both halves at once means only a DELETION can lower it, which is
    the event a ratchet is supposed to notice.
    """
    total = _sql_literal_read_count()
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
    # Floor lowered 18 → 17 on 2026-08-29: confidence_audit.py ported to MongoDB.
    # 17 → 16 on 2026-08-30: mine_shkreli_doctrine.py ported.
    # 16 → 15 on 2026-08-30: score_tournament_ranker.py DELETED — the tournament
    # debate it ranked was removed in 1cf3c0b, so the reader outlived its
    # subsystem. Note the difference from the two above: a PORT moves the read
    # to the Mongo scan below, a DELETE removes it from both. This floor now
    # measures a shrinking population for two different reasons at once, which
    # is the last argument for the NOTE above: the SQL-side floor is TWO ports
    # from 0, and at 0 every SQL test in this file goes green while checking
    # nothing. The Mongo scan below is what still has teeth.
    # 15 -> a combined floor on 2026-08-30. The SQL half was exactly AT its
    # floor with three of its fifteen reads inside
    # scripts/simulate_freshness_thresholds.py, one of the eight scripts in the
    # current porting batch: the next legitimate port would have turned this
    # assertion red for doing the work correctly, and the obvious repair —
    # lowering the number again — is the fifth consecutive lowering of a
    # measure that only ever decays. Combined, the number moves only when a
    # read is deleted.
    combined = total + _mongo_call_site_count()
    assert combined >= COMBINED_READ_FLOOR, (
        f"the two scanners together see only {combined} price_history reads "
        f"({total} SQL literal, {combined - total} Mongo call sites), floor "
        f"{COMBINED_READ_FLOOR}. A port MOVES a read between the two counts "
        "and must not change this total; only a deletion may lower it, and "
        "then only with the reason written into COMBINED_READ_FLOOR. If "
        "nothing was deleted, a scanner is broken — not the codebase clean."
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
    bad = _unpinned_reads(path)
    if rel in VENDOR_AGNOSTIC:
        expected, _, reason = VENDOR_AGNOSTIC[rel]
        assert len(bad) == expected, (
            f"{rel} is allow-listed for EXACTLY {expected} vendor-agnostic SQL "
            f"read(s) ({reason}) but the scanner sees {len(bad)}:\n"
            + "\n".join(f"  line {n}: {q}" for n, q in bad)
            + "\n\nIf a read was added, pin it. If one was fixed or deleted, "
            "lower the number in VENDOR_AGNOSTIC in the same commit."
        )
        return

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

    `one_vendor(...)` counts too — and its private alias `_one_vendor(...)`,
    which the substring below matches as well. It is the canonical pin helper
    from `app.quant.returns`: it resolves the dominant vendor and merges
    `{"source": src}` into the filter, so a read wrapped in it is pinned even
    though the literal `source` no longer appears at the call site. Without
    this the scanner condemns correctly-pinned code, which is exactly the
    false positive that made `technical_processor.py` look like a regression.

    The public name was added 2026-09-12 when the 32 budgeted reads were
    closed; matching only the underscored one would have forced 18 call sites
    to reach into a private helper to satisfy their own guard.
    """
    if "'source'" in call_src or '"source"' in call_src:
        return True
    # `$group: {_id: "$source"}` IS the vendor census. Pinning a vendor in the
    # read that decides which vendor is dominant makes its answer always 1 —
    # the Mongo counterpart of `group by source` in `_PINNED`.
    if "'$source'" in call_src or '"$source"' in call_src:
        return True
    return "one_vendor(" in call_src


def _lines_inside_functions_that_pin(tree: ast.AST) -> frozenset[int]:
    """Line numbers covered by a function that calls `keep_dominant_source`.

    The pandas pin happens after the read, on the DataFrame the read produced,
    so it cannot be seen in the call's own arguments. The enclosing function is
    the smallest unit where "this read's rows reach the helper" is decidable
    without dataflow analysis, and it is strictly narrower than the module-wide
    text check it replaces — which exempted `load_close_returns` for eleven
    weeks because a sibling function mentioned the helper.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = "\n".join(ast.unparse(stmt) for stmt in node.body)
        if "keep_dominant_source" not in body:
            continue
        end = getattr(node, "end_lineno", None) or node.lineno
        lines.update(range(node.lineno, end + 1))
    return frozenset(lines)


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

    # A read that pins per-ticker in pandas is pinned, same rule the SQL scan
    # applies — the filter is just downstream of the read.
    #
    # ⚠ This was a MODULE-WIDE text check until 2026-09-12, and that is how
    # `returns.py:205` (`load_close_returns`, the GARCH series on the desk
    # prompt) sat unpinned and invisible: the module DEFINES
    # keep_dominant_source, so the scanner returned [] for the whole file. An
    # escape granted by a module's own vocabulary exempts the reads it was
    # never meant to cover. Scoped to the ENCLOSING FUNCTION, the exemption
    # covers exactly the reads whose frame actually reaches the helper.
    pinning_lines = _lines_inside_functions_that_pin(tree)

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
        if node.lineno in pinning_lines:
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
        # A pure MAX/MIN/COUNT DISTINCT over `date` is vendor-immune. Read the
        # agg SPEC (the third argument), not `src` — unparsing the whole call
        # puts `('price_history'` in range of the same regex, so every agg_row
        # failed the `all(...)` and the rule below had never once fired.
        if fn.attr == "agg_row" and len(node.args) > 2:
            spec = " ".join(ast.unparse(node.args[2]).split())
            ops = re.findall(r"\('(\w+)'", spec)
            if ops and all(op in _MONGO_IMMUNE_AGGS for op in ops):
                continue

        bad.append((node.lineno, src[:140]))
    return sorted(bad)


def _mongo_call_site_count() -> int:
    """EVERY mongo_query/mongo_store call naming the price_history collection.

    Pinned or not, read or write — this counts the population, not the debt.
    It is the other half of the non-vacuity floor: when a Postgres reader is
    ported, its SQL literals disappear from `_sql_literal_read_count()` and
    reappear here, so the sum is unchanged and the ratchet stays honest.
    """
    total = 0
    for f in _python_files():
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("mongo_query", "mongo_store")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "price_history"):
                total += 1
    return total


#: The two scanners' combined population, measured 2026-08-30: 15 SQL literals
#: + 61 Mongo call sites. LOWER IT ONLY FOR A DELETION, and say which read went
#: and why. Porting a reader from Postgres to Mongo must leave this unchanged —
#: if your port lowers it, you collapsed several statements into fewer calls,
#: which is fine, but write that down here rather than editing the number.
COMBINED_READ_FLOOR = 76


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
    # EMPTY, and it must stay empty — 2026-09-12.
    #
    # This dict held 32 unpinned reads across 23 files for three weeks while
    # this file PASSED, because a budget that equals the violation count is not
    # a guard, it is a record. Its own comment said "These are NOT approved.
    # Several are on live decision paths — paper_trader marks the book" and
    # that remained true every day the suite was green.
    #
    # All 32 are closed: 18 now pin a vendor through
    # `app.quant.returns.one_vendor` (single-ticker) or `keep_dominant_source`
    # (multi-ticker), and the rest moved to VENDOR_AGNOSTIC with a written
    # reason. A NEW entry here re-opens the hole; pin the read or justify it
    # above instead.
}


def test_the_mongo_scanner_actually_finds_reads():
    """The same floor the SQL scan carries, for the same reason.

    This is the check that would have caught the decay: when nine files went to
    a 0 SQL budget, the collection was still being read 36 times.
    """
    total = sum(len(_unpinned_mongo_reads(f)) for f in _python_files())
    found = _mongo_call_site_count()
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
    bad = _unpinned_mongo_reads(path)
    if rel in VENDOR_AGNOSTIC:
        _, expected, reason = VENDOR_AGNOSTIC[rel]
        assert len(bad) == expected, (
            f"{rel} is allow-listed for EXACTLY {expected} vendor-agnostic "
            f"Mongo read(s) ({reason}) but the scanner sees {len(bad)}:\n"
            + "\n".join(f"  line {n}: {q}" for n, q in bad)
            + "\n\nIf a read was added, pin it with one_vendor(). If one was "
            "fixed or deleted, lower the number in VENDOR_AGNOSTIC in the same "
            "commit."
        )
        return

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


def test_the_combined_floor_counts_both_halves_and_can_reach_zero(monkeypatch, tmp_path):
    """The floor's own negative control.

    A population floor is only worth having if it can fall — the previous one
    could not tell "the scanner broke" from "the migration progressed", which
    is why it had been lowered four times in twelve days. Pin both properties
    here: each half counts what it claims to, and an empty tree drives the
    combined number to 0 rather than quietly matching nothing.
    """
    sql_reader = tmp_path / "sql_side.py"
    sql_reader.write_text(
        "def f(db, t):\n"
        "    return db.execute(\n"
        "        'SELECT close FROM price_history WHERE ticker = %s', [t])\n"
    )
    mongo_reader = tmp_path / "mongo_side.py"
    mongo_reader.write_text(
        "from app.db import mongo_query\n"
        "def f(t):\n"
        "    return mongo_query.find_rows('price_history', {'ticker': t},\n"
        "                                 ['close'])\n"
    )

    monkeypatch.setattr(__name__ and globals()["__name__"] and
                        __import__("sys").modules[__name__], "_python_files",
                        lambda: [sql_reader, mongo_reader])
    assert _sql_literal_read_count() == 1
    assert _mongo_call_site_count() == 1

    monkeypatch.setattr(__import__("sys").modules[__name__],
                        "_python_files", lambda: [])
    assert _sql_literal_read_count() == 0
    assert _mongo_call_site_count() == 0
    assert 0 < COMBINED_READ_FLOOR, (
        "a floor of 0 would pass on an empty scan, which is the failure this "
        "whole test exists to make impossible"
    )
