"""
Shared database migrations — run-once column additions.

Consolidates _ensure_summary_columns() that was duplicated in:
  - context_builder.py
  - context_builder.py
  - summarizer.py (superset with quality columns)

Usage:
    from app.utils.db_migrations import ensure_summary_columns
    ensure_summary_columns()  # safe to call multiple times
"""

import logging

from app.db.connection import get_db

logger = logging.getLogger(__name__)

_summary_columns_ensured = False


def ensure_summary_columns(db=None):
    """Add summary/quality columns if they don't exist (idempotent).

    Merges all migrations from context_builder + summarizer into one
    canonical superset. Safe to call multiple times — uses a module-level
    flag to skip after the first successful run.

    Args:
        db: Optional database connection. If None, calls get_db().
    """
    global _summary_columns_ensured
    if _summary_columns_ensured:
        return
    try:
        if db is None:
            with get_db() as new_db:
                ensure_summary_columns(new_db)
            return

        migrations = [
            # YouTube
            "ALTER TABLE youtube_transcripts ADD COLUMN IF NOT EXISTS summary VARCHAR",
            "ALTER TABLE youtube_transcripts ADD COLUMN IF NOT EXISTS tickers_mentioned VARCHAR",
            "ALTER TABLE youtube_transcripts ADD COLUMN IF NOT EXISTS summarized_at TIMESTAMP",
            "ALTER TABLE youtube_transcripts ADD COLUMN IF NOT EXISTS quality_status VARCHAR",
            "ALTER TABLE youtube_transcripts ADD COLUMN IF NOT EXISTS quality_reason VARCHAR",
            "ALTER TABLE youtube_transcripts ADD COLUMN IF NOT EXISTS quality_score INTEGER",
            # Reddit
            "ALTER TABLE reddit_posts ADD COLUMN IF NOT EXISTS summary VARCHAR",
            "ALTER TABLE reddit_posts ADD COLUMN IF NOT EXISTS summarized_at TIMESTAMP",
            "ALTER TABLE reddit_posts ADD COLUMN IF NOT EXISTS quality_status VARCHAR",
            "ALTER TABLE reddit_posts ADD COLUMN IF NOT EXISTS quality_reason VARCHAR",
            "ALTER TABLE reddit_posts ADD COLUMN IF NOT EXISTS quality_score INTEGER",
            "ALTER TABLE reddit_posts ADD COLUMN IF NOT EXISTS qualitative_draft JSONB",
            # News (base + quality columns from summarizer)
            "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS llm_summary VARCHAR",
            "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS summarized_at TIMESTAMP",
            "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS quality_status VARCHAR",
            "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS quality_reason VARCHAR",
            "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS quality_score INTEGER",
            "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS qualitative_draft JSONB",
            # AutoResearch V2
            """CREATE TABLE IF NOT EXISTS cycle_summaries (
                ticker VARCHAR,
                cycle_id VARCHAR,
                cycle_date TIMESTAMP,
                agent_name VARCHAR,
                action VARCHAR,
                confidence INTEGER,
                confidence_tier VARCHAR,
                rationale_summary VARCHAR,
                was_correct BOOLEAN,
                outcome_pnl DOUBLE PRECISION,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, cycle_id)
            )""",
            """CREATE TABLE IF NOT EXISTS debate_history (
                ticker VARCHAR,
                cycle_id VARCHAR,
                pro_argument VARCHAR,
                con_argument VARCHAR,
                winner VARCHAR,
                final_confidence INTEGER,
                UNIQUE (ticker, cycle_id)
            )""",
            """CREATE TABLE IF NOT EXISTS company_narratives (
                ticker VARCHAR PRIMARY KEY,
                story_summary TEXT NOT NULL,
                key_themes JSONB NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )""",
            # Source quality decay — tracks per-publisher discard rates.
            # Used by watchlist_health.update_source_quality_scores() to flag
            # publishers that systematically produce truncated/paywalled content.
            """CREATE TABLE IF NOT EXISTS source_quality_scores (
                publisher         TEXT PRIMARY KEY,
                total_articles    INTEGER DEFAULT 0,
                discarded_count   INTEGER DEFAULT 0,
                discard_pct       FLOAT DEFAULT 0.0,
                avg_content_len   INTEGER DEFAULT 0,
                is_banned         BOOLEAN DEFAULT FALSE,
                ban_reason        TEXT,
                last_updated      TIMESTAMPTZ DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS sub_task_queue (
                id SERIAL PRIMARY KEY,
                parent_agent VARCHAR,
                sub_agent VARCHAR,
                ticker VARCHAR,
                task_payload JSONB,
                status VARCHAR DEFAULT 'pending',
                result JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS critic_feedback (
                id SERIAL PRIMARY KEY,
                ticker VARCHAR,
                target_agent VARCHAR,
                score INTEGER,
                hallucinations JSONB,
                missing_risks JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )""",
        ]
        for sql in migrations:
            try:
                db.execute(sql)
            except Exception:
                pass  # Column already exists or other benign error
        _summary_columns_ensured = True
    except Exception as e:
        logger.warning("[PIPELINE] Could not ensure summary columns: %s", e)


_source_quality_table_ensured = False


def ensure_source_quality_table(db=None) -> None:
    """Ensure source_quality_scores table exists (idempotent).

    Called from watchlist_health.update_source_quality_scores() before
    the first upsert so the table is guaranteed to exist even on a fresh DB.
    """
    global _source_quality_table_ensured
    if _source_quality_table_ensured:
        return
    try:
        if db is None:
            with get_db() as new_db:
                ensure_source_quality_table(new_db)
            return
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS source_quality_scores (
                publisher         TEXT PRIMARY KEY,
                total_articles    INTEGER DEFAULT 0,
                discarded_count   INTEGER DEFAULT 0,
                discard_pct       FLOAT DEFAULT 0.0,
                avg_content_len   INTEGER DEFAULT 0,
                is_banned         BOOLEAN DEFAULT FALSE,
                ban_reason        TEXT,
                last_updated      TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        _source_quality_table_ensured = True
    except Exception as e:
        logger.warning("[PIPELINE] Could not ensure source_quality_scores table: %s", e)

