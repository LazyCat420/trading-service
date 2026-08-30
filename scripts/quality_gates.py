#!/usr/bin/env python3
"""Quality gates — the ONE definition of what counts as bad data.

Both `quality_census.py` (read-only, measures) and `purge_bad_data.py`
(destructive, deletes) import their predicates from here. Neither restates a
predicate in its own words: a check that copies the logic it is checking cannot
see that logic drift.

Two kinds of gate:

  RowGate    a WHERE predicate over a table that SURVIVES the purge. The rows
             matching the predicate are deleted; the table stays.
  TableGate  the whole table is archived to a dump file and then dropped.

Every gate carries `reason` (why this data is bad, in words a human can argue
with) and `evidence` (the measurement that justified it, taken 2026-08-17 on
trading_bot @ 10.0.0.16:5433). If you cannot write the evidence line, the gate
does not belong here.

WHAT IS DELIBERATELY *NOT* GATED
--------------------------------
Scraped market data is expensive to re-collect and is kept even when partial:
`price_history` (15.7M rows, 0 null/zero closes), `technicals` (1.37M derived
rows — kept, not recomputed, per user decision), `fundamentals` rows with SOME
datapoints, `sec_13f_holdings`, `congress_trades` (0 null tickers/amounts),
`macro_indicators` (0 null values), `embeddings` (0 null vectors).

`price_history` has 34,093 (ticker,date) pairs carried by more than one source.
Those are NOT duplicates: the table's natural key is (ticker,date,source) and it
has ZERO duplicates on that key (measured). yfinance and polygon legitimately
both cover a day. Nothing is deleted; readers need a source-priority rule and
Mongo needs a unique index on the real key. See the improvement checklist.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Tables owned by OTHER projects that share this Postgres database.
# treesearch-service (src/models/orm.py, src/db.py) reads and writes these.
# They are excluded from every gate, census line, and migration step — even the
# empty ones. This is an explicit allowlist, never a name pattern: a pattern
# would silently adopt the next table someone adds.
# ---------------------------------------------------------------------------
FOREIGN_OWNERS = {
    "treesearch-service": {
        # Derived from treesearch-service/src/models/orm.py __tablename__.
        # Kept in sorted order so a diff reads as one line per table.
        "breeders",
        "canonical_strains",
        "chemical_profiles",
        "genetic_relationships",
        "genomic_samples",
        "glass_artists",
        "glass_collaborations",
        "glass_favorites",
        "glass_ratings",
        "glass_submissions",
        "observation_images",
        "observations",
        "source_genomics_records",
        "strain_aliases",
    },
}

# Tables a foreign project used to declare and no longer does. They may still
# exist in the database, so they stay excluded from trading's gates — but they
# are listed APART from the live set so the drift test can tell "treesearch
# retired a table" from "someone quietly added a trading table to the
# allowlist". Every entry needs a reason.
FOREIGN_RETIRED = {
    "treesearch-service": {
        # 0 rows; treesearch-service/docs/data_cleanup.sql already DROPs it.
        "source_strain_records",
    },
}

FOREIGN_TABLES = frozenset(
    t
    for mapping in (FOREIGN_OWNERS, FOREIGN_RETIRED)
    for tables in mapping.values()
    for t in tables
)


def owner_of(table: str) -> str | None:
    """Which foreign project owns `table`, or None if it is trading-owned."""
    for mapping in (FOREIGN_OWNERS, FOREIGN_RETIRED):
        for owner, tables in mapping.items():
            if table in tables:
                return owner
    return None


@dataclass(frozen=True)
class RowGate:
    """Rows matching `predicate` are bad and get deleted. The table survives.

    `mongo` is the SAME condition as a Mongo query document. Both stores need
    it: the live mirror had already copied these rows into Mongo before the
    purge ran, so deleting them from Postgres alone left 183,712 bad documents
    behind — the opposite of migrating only good data.

    The two forms are written out separately rather than machine-translated
    because several predicates have no exact translation (a `::text` cast, a
    column renamed by the backfill mapper). They are held honest by the check
    that matters: after both purges the Postgres row count and the Mongo
    document count for the table must be equal.
    """

    table: str
    name: str
    reason: str
    evidence: str
    predicate: str          # a SQL boolean expression over `table`
    mongo: dict | None = None   # the same condition as a Mongo query document

    @property
    def kind(self) -> str:
        return "rows"


@dataclass(frozen=True)
class TableGate:
    """The whole table is archived then dropped."""

    table: str
    name: str
    reason: str
    evidence: str
    group: str
    # Populated at runtime for groups discovered from the live DB / ledger.
    dynamic: bool = field(default=False)

    @property
    def kind(self) -> str:
        return "table"


def _null_or_blank(field: str) -> dict:
    """Mongo form of SQL `field IS NULL OR btrim(field) = ''`.

    Deliberately NOT a regex. The first version used `$regex: r"^\\s*$"`, which
    survived one round of string escaping too many and reached Mongo as a
    pattern matching a literal backslash — so it found 0 of the 36 empty-body
    documents and the collection reported clean. `$trim` says what the SQL says,
    with nothing to escape.
    """
    return {"$expr": {"$eq": [
        {"$trim": {"input": {"$ifNull": [f"${field}", ""]}}}, ""
    ]}}


# ---------------------------------------------------------------------------
# ROW GATES — bad rows inside tables we keep
# ---------------------------------------------------------------------------
ROW_GATES: list[RowGate] = [
    RowGate(
        table="pipeline_events",
        name="empty_payload",
        reason=(
            "The event was logged with no payload at all. `news_scraped` and the "
            "`consensus` start/finish markers write `{}` 100% of the time, and "
            "`collecting/summarize` 67.6% of the time — the step fired but "
            "recorded nothing about what it did, so the row cannot answer any "
            "question later."
        ),
        evidence="183,492 of 372,727 rows (49.2%) have data_json = '{}' (2026-08-17)",
        predicate="data_json::text = '{}'",
        # The backfill mapper renames data_json -> data, so the Mongo field is
        # NOT the Postgres column name. Keying on data_json here would have
        # matched nothing and reported a clean collection.
        mongo={"data": {}},
    ),
    RowGate(
        table="data_archive",
        name="past_purge_deadline",
        reason=(
            "The row is past the purge date the archiver itself stamped on it. "
            "The retention job that was supposed to delete these never ran."
        ),
        evidence="58,779 of 72,099 rows (81.5%) have purge_after < now() (2026-08-17)",
        predicate="purge_after IS NOT NULL AND purge_after < now()",
        mongo={"purge_after": {"$ne": None, "$lt": "NOW"}},
    ),
    RowGate(
        table="news_articles",
        name="system_rejected",
        reason=(
            "The quality pipeline already judged these articles junk. We are "
            "honouring its own verdict rather than inventing a second one."
        ),
        evidence="249 rows with quality_status IN ('discarded','noise') (2026-08-17)",
        predicate="quality_status IN ('discarded', 'noise')",
        mongo={"quality_status": {"$in": ["discarded", "noise"]}},
    ),
    RowGate(
        table="news_articles",
        name="no_usable_text",
        reason=(
            "Neither the scraped summary nor the LLM summary holds enough text to "
            "analyse — the fetch returned essentially nothing."
        ),
        evidence="11 rows under 100 chars in BOTH summary and llm_summary (2026-08-17)",
        predicate=(
            "length(coalesce(btrim(summary), '')) < 100 "
            "AND length(coalesce(btrim(llm_summary), '')) < 100"
        ),
        mongo={"$expr": {"$and": [
            {"$lt": [{"$strLenCP": {"$trim": {"input": {"$ifNull": ["$summary", ""]}}}}, 100]},
            {"$lt": [{"$strLenCP": {"$trim": {"input": {"$ifNull": ["$llm_summary", ""]}}}}, 100]},
        ]}},
    ),
    RowGate(
        table="youtube_transcripts",
        name="system_rejected",
        reason="The quality pipeline already marked these transcripts discarded.",
        evidence="546 rows with quality_status = 'discarded' (2026-08-17)",
        predicate="quality_status = 'discarded'",
        mongo={"quality_status": "discarded"},
    ),
    RowGate(
        table="youtube_transcripts",
        name="no_transcript",
        reason=(
            "The video row exists but the transcript body is empty — the scrape "
            "produced a shell with no content to analyse."
        ),
        evidence="0 rows at census time; gate kept so the defect cannot creep back",
        predicate="raw_transcript IS NULL OR btrim(raw_transcript) = ''",
        mongo=_null_or_blank("raw_transcript"),
    ),
    RowGate(
        table="social_posts",
        name="empty_content",
        reason="The post body is empty — nothing was actually captured.",
        evidence="2 of 6,247 rows have empty content (2026-08-17)",
        predicate="content IS NULL OR btrim(content) = ''",
        mongo=_null_or_blank("content"),
    ),
    RowGate(
        table="reddit_posts",
        name="empty_body",
        reason=(
            "The post body is empty — nothing was actually captured. Note the "
            "column is `body`, not `content`: reddit_posts and social_posts do "
            "not share a schema, and the first draft of this gate named the "
            "wrong column and silently measured nothing."
        ),
        evidence="counted at census",
        predicate="body IS NULL OR btrim(body) = ''",
        mongo=_null_or_blank("body"),
    ),
    RowGate(
        table="reddit_posts",
        name="system_rejected",
        reason="The quality pipeline already marked these posts discarded.",
        evidence="counted at census",
        predicate="quality_status IN ('discarded', 'noise')",
        mongo={"quality_status": {"$in": ["discarded", "noise"]}},
    ),
    RowGate(
        table="fundamentals",
        name="all_key_datapoints_null",
        reason=(
            "Every headline datapoint is missing — the provider returned an empty "
            "record. Rows with SOME datapoints are deliberately KEPT: a partial "
            "fundamentals snapshot is still worth having and re-fetching it is "
            "the waste the user asked us to avoid."
        ),
        evidence=(
            "of 11,641 rows, 1,855 lack market_cap, 2,305 lack both P/E fields, "
            "2,809 lack revenue; the all-null intersection is counted at census"
        ),
        predicate=(
            "market_cap IS NULL AND pe_ratio IS NULL "
            "AND forward_pe IS NULL AND revenue IS NULL"
        ),
        mongo={"market_cap": None, "pe_ratio": None,
               "forward_pe": None, "revenue": None},
    ),
]


# ---------------------------------------------------------------------------
# TABLE GATES — whole tables archived then dropped
# ---------------------------------------------------------------------------

# Backup copies someone made by hand. Dead in both repos.
BACKUP_TABLES = [
    "sec_13f_holdings_backup_20260804",
    "fundamentals_backup_20260727",
    "decision_outcomes_backup_20260727",
    "decision_outcomes_backup_20260728",
]

# Referenced by NOTHING anywhere under ~/github/projects/sun (verified by grep
# across *.py, *.ts, *.js, excluding node_modules/.venv and the migration
# bookkeeping files that merely name every table).
DEAD_TABLES = [
    "cognition_episodic_memories",
    "cognition_memory_envelopes",
    "cognition_semantic_memories",
    "cognition_procedural_memories",
    "cognition_reflections",
    "cognition_reflective_memories",
    "context_telemetry",
]

STATIC_TABLE_GATES: list[TableGate] = [
    *[
        TableGate(
            table=t,
            name="hand_made_backup",
            reason=(
                "A hand-made backup copy of another table, superseded by the "
                "verified pg_dump this purge takes first."
            ),
            evidence="name matches a dated backup pattern; zero code references in either repo",
            group="backup",
        )
        for t in BACKUP_TABLES
    ],
    *[
        TableGate(
            table=t,
            name="unreferenced",
            reason="No code anywhere in the sun workspace reads or writes this table.",
            evidence="grep over all *.py/*.ts/*.js under ~/github/projects/sun found 0 references",
            group="dead",
        )
        for t in DEAD_TABLES
    ],
]

# Groups resolved against the live DB / ledger at run time, so the census and
# the purge always agree with today's reality rather than a pinned list:
#   empty       — 0 rows, trading-owned, not foreign
#   archive-only — disposition 'archive-only' in app/db/migration_ledger.json
DYNAMIC_GROUPS = ("empty", "archive_only")

GROUP_REASONS = {
    "empty": (
        "The table holds zero rows. There is no data to migrate, so it is "
        "archived (schema only) and dropped rather than left in an ambiguous "
        "pg state.",
        "0 rows in the live database at census time",
    ),
    "archive_only": (
        "Classified `archive-only` in the migration ledger: no live product "
        "dependency, retained only as history. Archived to a dump file with a "
        "checksum and restore command, then dropped.",
        "disposition == 'archive-only' in app/db/migration_ledger.json",
    ),
}


def row_gates_for(table: str) -> list[RowGate]:
    return [g for g in ROW_GATES if g.table == table]


def gated_tables() -> set[str]:
    """Tables named by at least one row gate."""
    return {g.table for g in ROW_GATES}


def assert_no_foreign(tables) -> None:
    """Fail closed if any gate ever names a table another project owns."""
    bad = {t: owner_of(t) for t in tables if t in FOREIGN_TABLES}
    if bad:
        raise SystemExit(
            "REFUSING TO PROCEED — these tables belong to another project: "
            + ", ".join(f"{t} (owned by {o})" for t, o in sorted(bad.items()))
        )


# A gate must never target a foreign table. Checked at import, not at delete
# time, so a bad edit cannot reach the database at all.
assert_no_foreign([g.table for g in ROW_GATES] + [g.table for g in STATIC_TABLE_GATES])
