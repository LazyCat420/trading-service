"""
Database Migrations
Handles structural updates to the schema for existing PostgreSQL databases.
Runs lightweight auto-migrations to ensure compatibility with newly added columns.
"""


def _safe_add_column(conn, table: str, column: str, dtype: str):
    """Add a column if it doesn't exist.

    PostgreSQL supports ADD COLUMN IF NOT EXISTS natively (since v9.6).
    We use a cursor from the raw connection (not PooledCursor) since
    this is called during pool init.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {dtype}"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def run_migrations(conn):
    """Auto-migrations for existing databases to match the current schema_pg.sql."""
    # ── congress_trades.bioguide_id ──
    # FIRST, deliberately. `schema_pg.sql` declares this column in the
    # `congress_trades` CREATE TABLE and then indexes it at line 1062, but
    # `CREATE TABLE IF NOT EXISTS` is a no-op against a table that predates the
    # column — so on any database created before it was added, that index
    # statement fails. Until 2026-08-10 `_init_schema` ran the whole file as one
    # batch, so that single failure discarded every statement after it. The
    # isolated test database sat at 161 tables against production's 214 for
    # exactly this reason, which is why `TRADING_BOT_TEST_DB` could never be
    # turned on. Adding the column here is what lets the index succeed.
    _safe_add_column(conn, "congress_trades", "bioguide_id", "TEXT")

    # ── Layout Presets (cross-browser sync)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS layout_presets (
                    name        TEXT PRIMARY KEY,
                    layout_data JSONB NOT NULL,
                    is_active   BOOLEAN DEFAULT FALSE,
                    updated_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Runtime parameters (agent-tunable via the Parameter Governor) ──
    # Append-only history; resolution = latest active non-expired row per key.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS runtime_parameters (
                    id          BIGSERIAL PRIMARY KEY,
                    param_key   TEXT NOT NULL,
                    value       DOUBLE PRECISION NOT NULL,
                    set_by      TEXT,
                    reason      TEXT,
                    status      TEXT DEFAULT 'active',
                    expires_at  TIMESTAMPTZ,
                    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_runtime_parameters_key_time
                ON runtime_parameters (param_key, created_at DESC)
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Agent-owned exits: provenance + targets on positions ──
    _safe_add_column(conn, "positions", "take_profit_pct", "DOUBLE PRECISION")
    _safe_add_column(conn, "positions", "stop_source", "TEXT")
    _safe_add_column(conn, "positions", "exit_style", "TEXT")

    # ── Cycle schedules: one-shot at an exact datetime (schedule_type='once')
    _safe_add_column(conn, "cycle_schedules", "run_at", "TIMESTAMPTZ")

    # ── Failure taxonomy: separate harness defects from bad market calls so the
    #    self-healing watchdog never tries to "repair" a losing trade.
    #    The index must be created HERE, after the column exists — putting it in
    #    schema_pg.sql aborts schema init on any pre-existing database.
    _safe_add_column(conn, "failure_buckets", "error_class", "TEXT")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_failure_buckets_error_class "
                "ON failure_buckets(error_class)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Youtube
    _safe_add_column(conn, "youtube_transcripts", "thumbnail_url", "TEXT")
    _safe_add_column(conn, "youtube_transcripts", "summary", "TEXT")
    _safe_add_column(conn, "youtube_transcripts", "tickers_mentioned", "TEXT")
    _safe_add_column(conn, "youtube_transcripts", "summarized_at", "TIMESTAMPTZ")

    # ── Timestamps & Quality
    _safe_add_column(conn, "news_articles", "collected_at", "TIMESTAMPTZ")
    _safe_add_column(conn, "news_articles", "quality_status", "TEXT")
    _safe_add_column(conn, "news_articles", "quality_reason", "TEXT")
    _safe_add_column(conn, "news_articles", "quality_score", "INTEGER")
    _safe_add_column(conn, "news_articles", "is_cluster_winner", "BOOLEAN")
    # 'detected' = ticker found in title/summary; 'query_fallback' = row
    # inherited the QUERIED ticker because extraction found none (Finnhub/
    # yfinance per-ticker feeds). Wake triggers must not act on fallback rows
    # — one generic roundup stored under 5 tickers woke a trade-enabled
    # cycle on 2026-08-02. NULL = legacy row (pre-2026-08-03, unknown).
    _safe_add_column(conn, "news_articles", "ticker_attribution", "TEXT")
    # Grounded fact extraction (langextract-style; app/services/news_extraction.py):
    # facts with source-aligned quotes, cached per article.
    _safe_add_column(conn, "news_articles", "grounded_facts", "JSONB")
    _safe_add_column(conn, "news_articles", "facts_extracted_at", "TIMESTAMPTZ")
    _safe_add_column(conn, "reddit_posts", "collected_at", "TIMESTAMPTZ")
    _safe_add_column(conn, "reddit_posts", "quality_status", "TEXT")
    _safe_add_column(conn, "reddit_posts", "quality_reason", "TEXT")
    _safe_add_column(conn, "youtube_transcripts", "collected_at", "TIMESTAMPTZ")
    _safe_add_column(conn, "youtube_transcripts", "quality_status", "TEXT")
    _safe_add_column(conn, "youtube_transcripts", "quality_reason", "TEXT")

    # ── URL Dedup constraint removed intentionally ──
    # The UNIQUE(url) constraint was removed because multiple tickers can share the same article URL,
    # and the primary key id (hash of title+ticker) handles deduplication correctly.

    # ── Source Trust (Reputation system updates)
    _safe_add_column(conn, "source_trust", "win_rate", "DOUBLE PRECISION DEFAULT 0.0")
    _safe_add_column(conn, "source_trust", "quality_wins", "INTEGER DEFAULT 0")
    _safe_add_column(conn, "source_trust", "flag_rate", "DOUBLE PRECISION DEFAULT 0.0")

    # ── Quant-grade commodity correlation columns
    _safe_add_column(
        conn, "stock_commodity_correlations", "cointegration_pvalue", "DOUBLE PRECISION"
    )
    _safe_add_column(conn, "stock_commodity_correlations", "cointegrated", "BOOLEAN")
    _safe_add_column(conn, "stock_commodity_correlations", "lead_lag_days", "INTEGER")
    _safe_add_column(
        conn, "stock_commodity_correlations", "lead_lag_correlation", "DOUBLE PRECISION"
    )
    _safe_add_column(
        conn, "stock_commodity_correlations", "vol_adj_correlation", "DOUBLE PRECISION"
    )
    _safe_add_column(
        conn,
        "stock_commodity_correlations",
        "correlation_stability",
        "DOUBLE PRECISION",
    )
    _safe_add_column(
        conn, "stock_commodity_correlations", "distance_correlation", "DOUBLE PRECISION"
    )
    _safe_add_column(conn, "stock_commodity_correlations", "quant_score", "INTEGER")
    _safe_add_column(conn, "stock_commodity_correlations", "method_details", "TEXT")

    # ── 13F Hedge Fund Tracker
    _safe_add_column(conn, "sec_13f_filers", "latest_quarter", "TEXT")
    _safe_add_column(conn, "sec_13f_filers", "next_expected_filing", "DATE")
    _safe_add_column(conn, "sec_13f_holdings", "name_of_issuer", "TEXT")
    _safe_add_column(conn, "sec_13f_holdings", "cusip", "TEXT")
    _safe_add_column(conn, "sec_13f_holdings", "share_type", "TEXT")
    _safe_add_column(conn, "sec_13f_holdings", "pct_change", "DOUBLE PRECISION")
    _safe_add_column(conn, "sec_13f_holdings", "is_new_position", "BOOLEAN")
    _safe_add_column(conn, "sec_13f_holdings", "is_exit", "BOOLEAN")
    _safe_add_column(conn, "sec_13f_holdings", "filing_date", "DATE")
    _safe_add_column(conn, "sec_13f_holdings", "collected_at", "TIMESTAMPTZ")
    # Provenance: 'edgar' (real 13F filings) vs 'yfinance' (pseudo-CIK holder rows).
    # Fund aggregates must filter on this — the two sources are not comparable.
    _safe_add_column(
        conn, "sec_13f_holdings", "source", "TEXT DEFAULT 'edgar'"
    )

    # ── Scheduler: policy-driven constraints and max_tickers
    _safe_add_column(conn, "cycle_schedules", "max_tickers", "INTEGER")
    _safe_add_column(conn, "cycle_schedules", "schedule_scope", "TEXT")
    _safe_add_column(conn, "cycle_schedules", "review_intent", "TEXT")
    _safe_add_column(conn, "cycle_schedules", "urgency", "TEXT")
    _safe_add_column(conn, "cycle_schedules", "earliest_window", "TEXT")
    _safe_add_column(conn, "cycle_schedules", "expiry_at", "TIMESTAMP")
    _safe_add_column(conn, "cycle_schedules", "reason_codes", "TEXT")
    _safe_add_column(conn, "cycle_schedules", "confidence", "INTEGER")
    _safe_add_column(conn, "cycle_schedules", "anti_overtrading_justification", "TEXT")

    # ── Pipeline version routing / benchmarking
    _safe_add_column(conn, "cycle_benchmarks", "requested_version", "TEXT")
    _safe_add_column(conn, "cycle_benchmarks", "effective_version", "TEXT")
    _safe_add_column(conn, "cycle_benchmarks", "benchmark_group", "TEXT")
    _safe_add_column(conn, "cycle_benchmarks", "execution_mode", "TEXT")
    _safe_add_column(conn, "cycle_benchmarks", "v2_stage", "INTEGER")
    _safe_add_column(conn, "pipeline_state", "requested_pipeline_version", "TEXT")
    _safe_add_column(conn, "pipeline_state", "effective_pipeline_version", "TEXT")
    _safe_add_column(conn, "pipeline_state", "benchmark_group", "TEXT")
    _safe_add_column(conn, "pipeline_state", "execution_mode", "TEXT")
    _safe_add_column(conn, "pipeline_state", "v2_stage", "INTEGER")
    _safe_add_column(conn, "pipeline_state", "max_tickers", "INTEGER")
    _safe_add_column(conn, "pipeline_state", "discovered_tickers", "INTEGER")
    _safe_add_column(conn, "pipeline_state", "dynamic_selection_mode", "BOOLEAN DEFAULT FALSE")
    _safe_add_column(conn, "pipeline_state", "agent_locale", "TEXT DEFAULT 'default'")

    # ── Pipeline state staleness detection
    _safe_add_column(conn, "pipeline_state", "updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

    # ── Strategy Evaluations: persist scope cycle_id
    _safe_add_column(conn, "strategy_evaluations", "cycle_id", "TEXT")

    # ── Ontology Graph: source_cycle_id tracking
    _safe_add_column(conn, "ontology_nodes", "source_cycle_id", "TEXT")
    _safe_add_column(conn, "ontology_edges", "source_cycle_id", "TEXT")

    # ── JIT Scraper / Re-analysis tracking
    _safe_add_column(conn, "news_articles", "analysis_count", "INTEGER DEFAULT 0")
    _safe_add_column(conn, "news_articles", "max_analyses", "INTEGER DEFAULT 5")
    _safe_add_column(conn, "reddit_posts", "analysis_count", "INTEGER DEFAULT 0")
    _safe_add_column(conn, "reddit_posts", "max_analyses", "INTEGER DEFAULT 5")
    _safe_add_column(conn, "youtube_transcripts", "analysis_count", "INTEGER DEFAULT 0")
    _safe_add_column(conn, "youtube_transcripts", "max_analyses", "INTEGER DEFAULT 5")

    # ── Analysis Results: Thesis storage ──
    _safe_add_column(conn, "analysis_results", "thesis_verdict", "TEXT")
    _safe_add_column(conn, "analysis_results", "thesis_confidence", "INTEGER")
    _safe_add_column(conn, "analysis_results", "thesis_summary", "TEXT")
    _safe_add_column(conn, "analysis_results", "thesis_updated_at", "TIMESTAMP")
    _safe_add_column(conn, "analysis_results", "thesis_unchanged", "BOOLEAN")

    # ── Attention Tracker (Smart Ticker Triage) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_attention (
                    ticker              TEXT PRIMARY KEY,
                    last_collected_at   TIMESTAMPTZ,
                    last_analyzed_at    TIMESTAMPTZ,
                    last_traded_at      TIMESTAMPTZ,
                    consecutive_skips   INTEGER DEFAULT 0,
                    consecutive_holds   INTEGER DEFAULT 0,
                    days_since_deep     INTEGER DEFAULT 0,
                    neglect_flagged     BOOLEAN DEFAULT FALSE,
                    neglect_reason      TEXT,
                    data_hash           TEXT,
                    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    updated_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Cycle Directives (Autoresearch Self-Improvement) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_directives (
                    id               TEXT PRIMARY KEY,
                    cycle_id         TEXT NOT NULL,
                    directive_type   TEXT NOT NULL,
                    directive_text   TEXT NOT NULL,
                    target_ticker    TEXT,
                    severity         TEXT DEFAULT 'info',
                    status           TEXT DEFAULT 'active',
                    created_at       TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    resolved_at      TIMESTAMPTZ,
                    expires_after    INTEGER DEFAULT 5
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_directives_status "
                "ON cycle_directives(status)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_directives_cycle "
                "ON cycle_directives(cycle_id)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Maintenance agent retry tracking ──
    _safe_add_column(
        conn, "pending_evolution_fixes", "attempt_count", "INTEGER DEFAULT 0"
    )

    # ── Rollback safety columns on pending_evolution_fixes ──
    _safe_add_column(conn, "pending_evolution_fixes", "backup_path", "TEXT")
    _safe_add_column(conn, "pending_evolution_fixes", "probation_until", "TIMESTAMPTZ")

    # ── Evolution Dead Ends (prevent repeating failed approaches) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS evolution_dead_ends (
                    id              TEXT PRIMARY KEY,
                    fix_id          TEXT NOT NULL,
                    target_type     TEXT NOT NULL,
                    target_name     TEXT NOT NULL,
                    approach_hash   TEXT NOT NULL,
                    failure_reason  TEXT NOT NULL,
                    metrics_before  JSONB,
                    metrics_after   JSONB,
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_dead_ends_target "
                "ON evolution_dead_ends(target_type, target_name)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── CORAL repair loop: queue (written in-container) + attempts (host) ──
    # Split because the image ships without .git/git/pytest, so the container
    # can only observe a failure — grading happens on a host checkout.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS evolution_repair_queue (
                    id              TEXT PRIMARY KEY,
                    cycle_id        TEXT,
                    error_message   TEXT NOT NULL,
                    traceback_text  TEXT,
                    target_path     TEXT,
                    target_symbol   TEXT,
                    status          TEXT DEFAULT 'queued',
                    attempts        INTEGER DEFAULT 0,
                    last_error      TEXT,
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    claimed_at      TIMESTAMPTZ,
                    finished_at     TIMESTAMPTZ
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_repair_queue_status "
                "ON evolution_repair_queue(status, created_at)"
            )
            # A pytest node id that already fails because of this bug. Preferred
            # over a generated reproduction test when one exists.
            cur.execute(
                "ALTER TABLE evolution_repair_queue "
                "ADD COLUMN IF NOT EXISTS repro_test TEXT"
            )
            # One open job per symbol: the watchdog fires hourly and would
            # otherwise queue the same traceback until the queue is all one bug.
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_repair_queue_open_target "
                "ON evolution_repair_queue(target_path, target_symbol) "
                "WHERE status IN ('queued', 'running')"
            )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS evolution_attempts (
                    id              TEXT PRIMARY KEY,
                    job_id          TEXT NOT NULL,
                    target_path     TEXT NOT NULL,
                    target_symbol   TEXT,
                    island          TEXT,
                    model           TEXT,
                    diff            TEXT,
                    rationale       TEXT,
                    score           DOUBLE PRECISION NOT NULL,
                    bundle          JSONB,
                    commit_hash     TEXT,
                    branch          TEXT,
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_attempts_target "
                "ON evolution_attempts(target_path, score DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_attempts_job "
                "ON evolution_attempts(job_id, created_at)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Pending Approvals ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    id            TEXT PRIMARY KEY,
                    agent_name    TEXT,
                    command       TEXT,
                    reason        TEXT,
                    status        TEXT DEFAULT 'pending',
                    stdout        TEXT,
                    stderr        TEXT,
                    created_at    TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    resolved_at   TIMESTAMPTZ
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Subsystem Benchmarks (per-cycle per-subsystem metrics) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subsystem_benchmarks (
                    id              TEXT PRIMARY KEY,
                    cycle_id        TEXT NOT NULL,
                    subsystem       TEXT NOT NULL,
                    metrics         JSONB NOT NULL,
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sub_bench_cycle "
                "ON subsystem_benchmarks(cycle_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sub_bench_subsystem "
                "ON subsystem_benchmarks(subsystem)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Finviz-parity fundamentals extension (2026-07-27) ──
    # ~36 new columns (dividends, ownership %, analyst targets/recs, earnings
    # date, EPS/sales growth windows, margins, float/shares, short ratio).
    # Guarded by the presence of `recom_score`: if it already exists this whole
    # block (including the NON-idempotent debt_to_equity normalization) is
    # skipped. The normalization converts yfinance/finnhub percent-style D/E
    # (AAPL 79.5) to ratios (0.795) so the column has ONE unit; running it
    # twice would corrupt the data, hence the sentinel.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'fundamentals' AND column_name = 'recom_score'"
            )
            if cur.fetchone() is None:
                for col in [
                    "dividend_yield", "dividend_ttm", "payout_ratio",
                    "price_to_cash", "price_to_fcf", "ev_to_sales",
                    "lt_debt_to_equity", "quick_ratio", "eps_ttm", "eps_next_q",
                    "eps_growth_this_y", "eps_growth_next_y",
                    "eps_growth_next_5y", "eps_growth_past_5y",
                    "sales_growth_past_5y", "eps_growth_qoq",
                    "sales_growth_qoq", "eps_surprise", "sales_surprise",
                    "insider_own_pct", "insider_trans_pct", "inst_own_pct",
                    "inst_trans_pct", "roic", "gross_margin", "oper_margin",
                    "shares_outstanding", "shares_float", "short_ratio",
                    "short_interest", "recom_score", "target_price",
                ]:
                    cur.execute(
                        f"ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION"
                    )
                cur.execute("ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS earnings_date DATE")
                cur.execute("ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS ipo_date DATE")
                cur.execute("ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS optionable BOOLEAN")
                cur.execute("ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS shortable BOOLEAN")
                cur.execute(
                    "UPDATE fundamentals SET debt_to_equity = debt_to_equity/100.0 "
                    "WHERE source IN ('yfinance', 'finnhub') AND debt_to_equity IS NOT NULL"
                )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── ETF metadata table (2026-07-27, screener ETF tab) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS etf_metadata (
                    ticker            TEXT PRIMARY KEY,
                    category          TEXT,
                    fund_family       TEXT,
                    total_assets      DOUBLE PRECISION,
                    expense_ratio_pct DOUBLE PRECISION,
                    dividend_yield    DOUBLE PRECISION,
                    ret_3y            DOUBLE PRECISION,
                    ret_5y            DOUBLE PRECISION,
                    nav_price         DOUBLE PRECISION,
                    beta_3y           DOUBLE PRECISION,
                    collected_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── One-time fix: ETH price collision + CAGR garbage position ──
    # The ticker "ETH" was misclassified as crypto, causing snapshots to use
    # the Ethereum crypto price (~$1,800) instead of the ETF price (~$23).
    # This inflated portfolio_snapshots to $324K.  CAGR had 479M shares at
    # $0.00001 due to a bad price pull.
    # This migration is idempotent: it only deletes if the bad data exists.
    _fix_eth_cagr_data(conn)

    # ── Deterministic baseline score, shadow-only (2026-08-05) ──
    _create_decision_scores(conn)

    # ── Persistent Research Firm: Ticker Dossiers & Queues ──
    _create_persistent_research_tables(conn)

    # ── Autovacuum thresholds for the big tables ──
    _tune_autovacuum_for_large_tables(conn)


# Per-table autovacuum overrides. Scale factor 0 + a fixed row threshold, which
# is the standard treatment for tables far larger than the defaults assume.
#
# Threshold sizing: analyze often enough that the planner's row estimate never
# drifts far, vacuum at roughly twice that. Both are absolute, so they do NOT
# recede as the table grows — which is the entire bug being fixed.
_AUTOVACUUM_TUNING = {
    # 15.2M rows, append-mostly. The one that had never been touched.
    "price_history": {"analyze": 50_000, "vacuum": 100_000},
    # 1.3M rows but high churn — rows are rewritten on every refresh.
    "technicals": {"analyze": 25_000, "vacuum": 50_000},
    "pipeline_events": {"analyze": 10_000, "vacuum": 20_000},
    "sec_13f_holdings": {"analyze": 5_000, "vacuum": 10_000},
}


def _tune_autovacuum_for_large_tables(conn):
    """Give the large tables absolute autovacuum thresholds instead of ratios.

    Postgres' defaults are proportional: autoanalyze at 10% of the table
    changed, autovacuum at 20% dead. On a big table the trigger point recedes
    exactly as fast as the table grows, so it is never reached.

    Measured on trading_bot 2026-07-31 (PG16, autovacuum on, track_counts on,
    every table at reloptions=NULL):

        price_history   15,163,653 live | 26,885 dead
                        needs 3,032,781 dead to vacuum, 1,516,415 mods to analyze
                        last_autovacuum = NULL, last_autoanalyze = NULL — never, ever

    So this is not a broken autovacuum daemon; it fires fine on the smaller
    tables (technicals 07-29, ontology_nodes 07-29, sec_13f_holdings 07-26).
    It is a threshold that a 15M-row table cannot reach. The visible symptom is
    the planner working from a stale reltuples — it estimated 30k rows for
    price_history against an actual 15.2M — and a manual ANALYZE only fixes
    that until it drifts back, which is why this belongs in the schema and not
    in someone's shell history.

    ALTER TABLE ... SET is idempotent metadata-only DDL: no table rewrite, no
    lock beyond a brief ACCESS EXCLUSIVE on the catalog row, safe to re-run at
    every boot. A missing table is skipped rather than raised, so this stays
    boot-safe on a fresh or partial database.
    """
    import logging
    logger = logging.getLogger(__name__)

    for table, cfg in _AUTOVACUUM_TUNING.items():
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                if cur.fetchone()[0] is None:
                    continue
                # Identifiers cannot be bound as parameters, and these come from
                # the module-level dict above — never from input.
                cur.execute(
                    f"ALTER TABLE public.{table} SET ("
                    "  autovacuum_analyze_scale_factor = 0,"
                    f" autovacuum_analyze_threshold = {int(cfg['analyze'])},"
                    "  autovacuum_vacuum_scale_factor = 0,"
                    f" autovacuum_vacuum_threshold = {int(cfg['vacuum'])})"
                )
            conn.commit()
        except Exception as e:
            logger.warning("[migrations] autovacuum tuning skipped for %s: %s", table, e)
            try:
                conn.rollback()
            except Exception:
                pass


def _fix_eth_cagr_data(conn):
    """One-time fix for corrupted portfolio data."""
    import logging

    logger = logging.getLogger(__name__)
    BOT_ID = "lazy-trader-v4"

    try:
        with conn.cursor() as cur:
            # 1. Delete inflated snapshots (ETH price collision caused $324K)
            cur.execute("DELETE FROM portfolio_snapshots WHERE total_value > 200000")
            deleted_snaps = cur.rowcount
            if deleted_snaps > 0:
                logger.info(
                    "[MIGRATION] Deleted %d corrupted snapshot(s) with inflated values",
                    deleted_snaps,
                )

            # 2. Delete CAGR garbage position (479M shares @ $0.00001)
            cur.execute(
                "SELECT qty, avg_entry_price FROM positions "
                "WHERE bot_id = %s AND ticker = 'CAGR'",
                (BOT_ID,),
            )
            cagr = cur.fetchone()
            if cagr:
                cost = float(cagr[0]) * float(cagr[1])
                cur.execute(
                    "DELETE FROM positions WHERE bot_id = %s AND ticker = 'CAGR'",
                    (BOT_ID,),
                )
                cur.execute(
                    "UPDATE bots SET cash_balance = cash_balance + %s WHERE bot_id = %s",
                    (cost, BOT_ID),
                )
                cur.execute(
                    "DELETE FROM position_lots WHERE bot_id = %s AND ticker = 'CAGR'",
                    (BOT_ID,),
                )
                cur.execute(
                    "DELETE FROM orders WHERE bot_id = %s AND ticker = 'CAGR'",
                    (BOT_ID,),
                )
                cur.execute(
                    "DELETE FROM trade_fills WHERE bot_id = %s AND ticker = 'CAGR'",
                    (BOT_ID,),
                )
                logger.info(
                    "[MIGRATION] Deleted CAGR garbage position (%.0f shares), "
                    "refunded $%.2f to cash",
                    cagr[0],
                    cost,
                )

            conn.commit()
    except Exception as e:
        logger.warning("[MIGRATION] ETH/CAGR data fix failed (non-fatal): %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Per-Box Telemetry columns on llm_audit_logs ──
    _safe_add_column(conn, "llm_audit_logs", "endpoint_name", "TEXT")
    _safe_add_column(conn, "llm_audit_logs", "prompt_tokens", "INTEGER DEFAULT 0")
    _safe_add_column(conn, "llm_audit_logs", "completion_tokens", "INTEGER DEFAULT 0")
    _safe_add_column(conn, "llm_audit_logs", "queue_wait_ms", "INTEGER DEFAULT 0")
    _safe_add_column(conn, "llm_audit_logs", "tokens_per_second", "REAL")

    # ── Triage tier on analysis_results ──
    _safe_add_column(conn, "analysis_results", "triage_tier", "TEXT DEFAULT 'standard'")



    # ── Evolution lessons archive (memory consolidation) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS evolution_lessons_archive (
                    id          TEXT PRIMARY KEY,
                    session_id  TEXT,
                    round       INTEGER DEFAULT 0,
                    score       REAL,
                    status      TEXT,
                    lesson_text TEXT,
                    timestamp   TEXT,
                    archived_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Fix discovered_tickers PK: (ticker) → (ticker, source) ──
    # The ON CONFLICT (ticker, source) clause in reddit_collector requires a
    # composite unique constraint that the old schema (PK on ticker only) doesn't have.
    try:
        with conn.cursor() as cur:
            # Check if 'source' is already part of the PK by seeing if the
            # unique index on (ticker, source) already exists
            cur.execute("""
                SELECT 1 FROM pg_indexes
                WHERE tablename = 'discovered_tickers'
                AND indexdef LIKE '%%ticker, source%%'
            """)
            if not cur.fetchone():
                cur.execute("""
                    ALTER TABLE discovered_tickers
                    DROP CONSTRAINT IF EXISTS
                    discovered_tickers_pkey
                """)
                cur.execute("""
                    ALTER TABLE discovered_tickers
                    ADD CONSTRAINT discovered_tickers_pkey
                    PRIMARY KEY (ticker, source)
                """)
                conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Hallucination Log (post-LLM verification audit trail) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS hallucination_log (
                    id VARCHAR PRIMARY KEY,
                    ticker VARCHAR,
                    cycle_id VARCHAR,
                    hallucination_count INTEGER,
                    total_claims INTEGER,
                    hallucination_rate FLOAT,
                    rejected BOOLEAN,
                    details_json VARCHAR,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass



    # ── Market Snapshots (Anti-Hallucination V2) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    ticker              TEXT,
                    fetched_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    data_source         TEXT,
                    candles_used        INTEGER,
                    price               DOUBLE PRECISION,
                    open                DOUBLE PRECISION,
                    high                DOUBLE PRECISION,
                    low                 DOUBLE PRECISION,
                    volume              BIGINT,
                    vwap                DOUBLE PRECISION,
                    rsi_14              DOUBLE PRECISION,
                    macd                DOUBLE PRECISION,
                    macd_signal         DOUBLE PRECISION,
                    macd_hist           DOUBLE PRECISION,
                    bb_upper            DOUBLE PRECISION,
                    bb_lower            DOUBLE PRECISION,
                    bb_pct              DOUBLE PRECISION,
                    sma_20              DOUBLE PRECISION,
                    sma_50              DOUBLE PRECISION,
                    sma_200             DOUBLE PRECISION,
                    atr_14              DOUBLE PRECISION,
                    adx_14              DOUBLE PRECISION,
                    stoch_k             DOUBLE PRECISION,
                    stoch_d             DOUBLE PRECISION,
                    returns_1d          DOUBLE PRECISION,
                    returns_5d          DOUBLE PRECISION,
                    returns_20d         DOUBLE PRECISION,
                    volatility_20d      DOUBLE PRECISION,
                    sharpe_20d          DOUBLE PRECISION,
                    max_drawdown_20d    DOUBLE PRECISION,
                    beta_20d            DOUBLE PRECISION,
                    pe_ratio            DOUBLE PRECISION,
                    forward_pe          DOUBLE PRECISION,
                    eps                 DOUBLE PRECISION,
                    market_cap          DOUBLE PRECISION,
                    revenue_growth      DOUBLE PRECISION,
                    profit_margin       DOUBLE PRECISION,
                    debt_to_equity      DOUBLE PRECISION,
                    PRIMARY KEY (ticker, fetched_at)
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Debate History Unique Constraint and Columns ──
    _safe_add_column(conn, "debate_history", "pro_argument", "TEXT")
    _safe_add_column(conn, "debate_history", "con_argument", "TEXT")
    _safe_add_column(conn, "debate_history", "persona_outcomes", "JSONB")
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM debate_history a USING (
                  SELECT MIN(ctid) as ctid, ticker, cycle_id
                  FROM debate_history
                  GROUP BY ticker, cycle_id HAVING COUNT(*) > 1
                ) b
                WHERE a.ticker = b.ticker AND a.cycle_id = b.cycle_id AND a.ctid <> b.ctid
            """)
            cur.execute("""
                ALTER TABLE debate_history 
                ADD CONSTRAINT debate_history_ticker_cycle_id_key UNIQUE (ticker, cycle_id)
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Bot Profiles: starting_cash + description columns ──
    _safe_add_column(conn, "bots", "starting_cash", "DOUBLE PRECISION DEFAULT 100000.0")
    _safe_add_column(conn, "bots", "description", "TEXT DEFAULT ''")

    # Backfill starting_cash for existing bots that have NULL
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bots SET starting_cash = cash_balance "
                "WHERE starting_cash IS NULL OR starting_cash = 0"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Trading Constitution (self-improving agentic rules) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trading_constitution (
                    id              TEXT PRIMARY KEY,
                    rule_category   TEXT NOT NULL,
                    rule_text       TEXT NOT NULL,
                    rule_params     JSONB DEFAULT '{}',
                    version         INTEGER DEFAULT 1,
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    amended_at      TIMESTAMPTZ,
                    amendment_reason TEXT,
                    performance_data JSONB DEFAULT '{}',
                    is_active       BOOLEAN DEFAULT TRUE
                )
            """)
            # Seed v1 rules if table is empty
            cur.execute("SELECT COUNT(*) FROM trading_constitution")
            count = cur.fetchone()[0]
            if count == 0:
                import json as _json

                seed_rules = [
                    (
                        "position_limit_v1",
                        "position_limits",
                        "Maximum 8 concurrent open positions",
                        _json.dumps({"max_positions": 8}),
                    ),
                    (
                        "sector_concentration_v1",
                        "sector",
                        ("No more than 30% of positions in a single sector"),
                        _json.dumps({"max_sector_pct": 30}),
                    ),
                    (
                        "sell_rsi_v1",
                        "sell_triggers",
                        ("SELL if RSI > 70 (overbought condition)"),
                        _json.dumps({"rsi_threshold": 70}),
                    ),
                    (
                        "sell_pe_v1",
                        "sell_triggers",
                        ("SELL if P/E exceeds 1.5x sector average"),
                        _json.dumps({"pe_multiplier": 1.5}),
                    ),
                    (
                        "sell_holding_v1",
                        "sell_triggers",
                        ("Review positions held >14 days without thesis confirmation"),
                        _json.dumps(
                            {
                                "max_holding_days": 14,
                            }
                        ),
                    ),
                    (
                        "sizing_v1",
                        "sizing",
                        ("Position size 2-10% of cash based on confidence level"),
                        _json.dumps(
                            {
                                "min_pct": 2,
                                "max_pct": 10,
                                "min_confidence": 70,
                            }
                        ),
                    ),
                    (
                        "buy_rsi_v1",
                        "buy_requirements",
                        "BUY only if RSI < 65 (not overbought)",
                        _json.dumps({"rsi_max": 65}),
                    ),
                ]
                for rule_id, cat, text, params in seed_rules:
                    cur.execute(
                        "INSERT INTO trading_constitution "
                        "(id, rule_category, rule_text, "
                        "rule_params) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (id) DO NOTHING",
                        (rule_id, cat, text, params),
                    )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    # ── Price Triggers: expanded columns for order trigger system ──
    _safe_add_column(conn, "price_triggers", "action", "TEXT DEFAULT 'SELL'")
    _safe_add_column(conn, "price_triggers", "qty_pct", "DOUBLE PRECISION DEFAULT 1.0")
    _safe_add_column(conn, "price_triggers", "trailing_pct", "DOUBLE PRECISION")
    _safe_add_column(conn, "price_triggers", "highest_price", "DOUBLE PRECISION")
    _safe_add_column(conn, "price_triggers", "reason", "TEXT")
    _safe_add_column(conn, "price_triggers", "triggered_at", "TIMESTAMPTZ")
    _safe_add_column(conn, "price_triggers", "created_by", "TEXT DEFAULT 'bot'")
    _safe_add_column(conn, "price_triggers", "trigger_price", "DOUBLE PRECISION")
    _safe_add_column(conn, "price_triggers", "dynamic_trigger_type", "TEXT")
    _safe_add_column(conn, "price_triggers", "dynamic_trigger_value", "DOUBLE PRECISION")

    # ── Autoresearch Experiences (Reflector Loop) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_experiences (
                    id              TEXT PRIMARY KEY,
                    agent_name      TEXT NOT NULL,
                    task_context    TEXT NOT NULL,
                    lesson_learned  TEXT NOT NULL,
                    success_score   DOUBLE PRECISION DEFAULT 1.0,
                    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    last_applied    TIMESTAMP WITH TIME ZONE
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_experiences_name "
                "ON agent_experiences(agent_name)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Tool-Use Improvement Framework ──
    _safe_add_column(conn, "agent_traces", "task_type", "TEXT")
    _safe_add_column(conn, "agent_traces", "endpoint_name", "TEXT")
    _safe_add_column(conn, "agent_traces", "model_name", "TEXT")
    _safe_add_column(conn, "tool_playbook", "task_type", "TEXT")
    _safe_add_column(conn, "tool_playbook", "recommended_tool_sequence", "TEXT")
    _safe_add_column(conn, "tool_playbook", "stop_conditions", "TEXT")
    _safe_add_column(conn, "tool_playbook", "bad_patterns_to_avoid", "TEXT")
    _safe_add_column(conn, "tool_playbook", "tool_name", "TEXT")
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tool_playbook_task ON tool_playbook(task_type)")
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── tool_playbook: collapse the unbounded duplicate pile ──
    # update_tool_playbook() inserted a fresh uuid4 PK per (agent, tool) on
    # EVERY run under `ON CONFLICT DO NOTHING` — a conflict that can never fire
    # against a random PK. The table reached 4,948 rows (+831/day) by
    # 2026-08-05, and base_agent injected every matching row into the prompt:
    # 1,387 identical lines, a 131k-char junior-analyst prompt, and an instant
    # "0 output tokens of a 0 token window" from prism. Every discovery cycle
    # died at the first agent.
    #
    # The natural key is (agent_role, task_type, market_context, tool_name).
    # tool_name has to be its OWN column: recommended_tool_sequence embeds the
    # live stats ("avg score: 94.2 over 104 uses"), so keying on that text
    # would still mint a new row every time a score moved.
    try:
        with conn.cursor() as cur:
            # Backfill tool_name from the legacy free-text sequence.
            cur.execute("""
                UPDATE tool_playbook
                   SET tool_name = substring(recommended_tool_sequence
                                             from 'Primary tool: ([^ ]+)')
                 WHERE tool_name IS NULL
                   AND recommended_tool_sequence LIKE 'Primary tool: %'
            """)
            # Keep the newest row per natural key, drop the rest.
            cur.execute("""
                DELETE FROM tool_playbook a
                 USING tool_playbook b
                 WHERE a.tool_name IS NOT NULL
                   AND a.tool_name        IS NOT DISTINCT FROM b.tool_name
                   AND a.agent_role       IS NOT DISTINCT FROM b.agent_role
                   AND a.task_type        IS NOT DISTINCT FROM b.task_type
                   AND a.market_context   IS NOT DISTINCT FROM b.market_context
                   AND (a.created_at, a.id) < (b.created_at, b.id)
            """)
            deleted = cur.rowcount
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_playbook_natural_key
                    ON tool_playbook (agent_role, task_type, market_context, tool_name)
                 WHERE tool_name IS NOT NULL
            """)
            conn.commit()
            if deleted:
                print(f"[migrations] tool_playbook: removed {deleted} duplicate rows")
    except Exception as e:
        print(f"[migrations] tool_playbook dedupe skipped: {e}")
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Cycle Checkpoints: retroactive UNIQUE constraint ──
    # checkpoints.py has UNIQUE in its CREATE TABLE, but databases created
    # before that change never get the constraint. ON CONFLICT upserts break.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_checkpoint'
                OR conname = 'cycle_checkpoints_cycle_id_step_name_ticker_key'
            """)
            if not cur.fetchone():
                # Remove duplicates first (keep the newest)
                cur.execute("""
                    DELETE FROM cycle_checkpoints a USING (
                        SELECT MAX(ctid) as ctid, cycle_id, step_name, ticker
                        FROM cycle_checkpoints
                        GROUP BY cycle_id, step_name, ticker HAVING COUNT(*) > 1
                    ) b
                    WHERE a.cycle_id = b.cycle_id
                    AND a.step_name = b.step_name
                    AND a.ticker = b.ticker
                    AND a.ctid <> b.ctid
                """)
                cur.execute("""
                    ALTER TABLE cycle_checkpoints
                    ADD CONSTRAINT uq_checkpoint
                    UNIQUE (cycle_id, step_name, ticker)
                """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Ticker Validation ──
    _safe_add_column(conn, "discovered_tickers", "validation_status", "TEXT DEFAULT 'pending'")
    _safe_add_column(conn, "discovered_tickers", "rate_limited_count", "INTEGER DEFAULT 0")

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_quarantine (
                    ticker          TEXT PRIMARY KEY,
                    reason          TEXT NOT NULL,
                    details         TEXT,
                    quarantined_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Thesis tracking columns on analysis_results ──
    _safe_add_column(conn, "analysis_results", "thesis_verdict", "VARCHAR(10)")
    _safe_add_column(conn, "analysis_results", "thesis_confidence", "INTEGER")
    _safe_add_column(conn, "analysis_results", "thesis_summary", "TEXT")
    _safe_add_column(conn, "analysis_results", "thesis_updated_at", "TIMESTAMPTZ")
    _safe_add_column(
        conn, "analysis_results", "thesis_unchanged", "BOOLEAN DEFAULT FALSE"
    )

    # ── Heartbeat tracking on ticker_attention ──
    _safe_add_column(conn, "ticker_attention", "last_full_review_at", "TIMESTAMPTZ")

    # ── Watermarks table for delta collection ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_collection_watermarks (
                    ticker          VARCHAR(10) NOT NULL,
                    source          VARCHAR(50) NOT NULL,
                    last_collected  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (ticker, source)
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── System Commands Queue (Frontend -> Backend Bridge) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS system_commands (
                    id              TEXT PRIMARY KEY,
                    command_type    TEXT NOT NULL,
                    payload         JSONB DEFAULT '{}',
                    status          TEXT DEFAULT 'pending',
                    result          JSONB,
                    error_message   TEXT,
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    started_at      TIMESTAMPTZ,
                    completed_at    TIMESTAMPTZ
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_system_commands_status "
                "ON system_commands(status)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Telemetry Logging column migrations ──
    _safe_add_column(conn, "tool_usage_stats", "service_source", "TEXT DEFAULT 'trading-service'")
    _safe_add_column(conn, "agent_traces", "service_source", "TEXT DEFAULT 'trading-service'")

    # ── Tool Reputation Index ──
    # Supports efficient queries for tool reliability stats (success rates by time window)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_usage_stats_name_time "
                "ON tool_usage_stats(tool_name, called_at DESC)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Agent Tool Optimization (Highlight & Prune) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_tool_optimization (
                    agent_name       TEXT NOT NULL,
                    tool_name        TEXT NOT NULL,
                    unused_count     INTEGER DEFAULT 0,
                    status           TEXT DEFAULT 'active',
                    updated_at       TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (agent_name, tool_name)
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Graph Node Events (Real-time Brain Graph WebSocket Bridge) ──
    # Trading-service writes events here when creating ontology nodes.
    # Trading-client polls this table and broadcasts to connected WebSocket clients.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS graph_node_events (
                    id              SERIAL PRIMARY KEY,
                    event_type      TEXT NOT NULL,
                    node_id         TEXT,
                    node_type       TEXT,
                    label           TEXT,
                    source_id       TEXT,
                    target_id       TEXT,
                    relation        TEXT,
                    weight          DOUBLE PRECISION DEFAULT 0.5,
                    metadata_json   TEXT,
                    ticker          TEXT,
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    consumed        BOOLEAN DEFAULT FALSE
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_events_consumed "
                "ON graph_node_events(consumed, created_at)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Fix A.4: UNIQUE(bot_id, ticker) on positions table ──
    # Prevents concurrent BUYs from creating duplicate position rows for the same ticker.
    # The paper_trader code already handles "existing position" logic, but this constraint
    # provides a DB-level safety net against race conditions.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM pg_indexes
                WHERE tablename = 'positions'
                AND indexdef LIKE '%%bot_id%%ticker%%'
                AND indexdef LIKE '%%UNIQUE%%'
            """)
            if not cur.fetchone():
                # Deduplicate first: keep the row with the highest qty for each bot+ticker pair
                cur.execute("""
                    DELETE FROM positions a USING (
                        SELECT MIN(ctid) as ctid, bot_id, ticker
                        FROM positions
                        GROUP BY bot_id, ticker HAVING COUNT(*) > 1
                    ) b
                    WHERE a.bot_id = b.bot_id AND a.ticker = b.ticker AND a.ctid <> b.ctid
                """)
                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_bot_ticker
                    ON positions(bot_id, ticker)
                """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Fix A.6: Performance indexes on agent_traces ──
    # This high-volume table (one row per tool call) has no indexes beyond PK.
    # Dashboard queries filtering by run_id or agent_name do full table scans.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_traces_run_id "
                "ON agent_traces(run_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_traces_agent_created "
                "ON agent_traces(agent_name, created_at)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Prompt Templates (Sector-Adaptive Prompt Storage) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS prompt_templates (
                    id              TEXT PRIMARY KEY,
                    sector          TEXT NOT NULL,
                    action_type     TEXT NOT NULL DEFAULT 'BUY',
                    system_prompt   TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'candidate',
                    total_trades    INTEGER DEFAULT 0,
                    wins            INTEGER DEFAULT 0,
                    losses          INTEGER DEFAULT 0,
                    win_rate        DOUBLE PRECISION DEFAULT 0.0,
                    avg_pnl_pct     DOUBLE PRECISION DEFAULT 0.0,
                    parent_id       TEXT,
                    generation      INTEGER DEFAULT 0,
                    created_by      TEXT DEFAULT 'seed',
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    promoted_at     TIMESTAMPTZ,
                    benched_at      TIMESTAMPTZ
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_prompt_templates_sector_status "
                "ON prompt_templates(sector, status)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_prompt_templates_win_rate "
                "ON prompt_templates(win_rate DESC)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Fix A.7: Add PRIMARY KEY (ticker, cycle_id) to cycle_summaries table ──
    _safe_add_column(conn, "cycle_summaries", "created_at", "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP")
    try:
        with conn.cursor() as cur:
            # Check if primary key constraint already exists
            cur.execute("""
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'cycle_summaries'::regclass
                AND contype = 'p'
            """)
            if not cur.fetchone():
                # Deduplicate first: keep the row with the highest ctid
                cur.execute("""
                    DELETE FROM cycle_summaries a USING (
                        SELECT MIN(ctid) as ctid, ticker, cycle_id
                        FROM cycle_summaries
                        GROUP BY ticker, cycle_id HAVING COUNT(*) > 1
                    ) b
                    WHERE a.ticker = b.ticker AND a.cycle_id = b.cycle_id AND a.ctid <> b.ctid
                """)
                cur.execute("""
                    ALTER TABLE cycle_summaries
                    ADD CONSTRAINT cycle_summaries_pkey
                    PRIMARY KEY (ticker, cycle_id)
                """)
            conn.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("[MIGRATION] cycle_summaries primary key addition failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Smart Ticker Lifecycle: price tracking + decision history ──
    _safe_add_column(conn, "analysis_results", "price_at_analysis", "DOUBLE PRECISION")
    _safe_add_column(conn, "ticker_attention", "price_at_analysis", "DOUBLE PRECISION")
    _safe_add_column(conn, "ticker_attention", "recent_decisions", "JSONB DEFAULT '[]'")

    # ── Watchlist Curation Log (LLM Curator audit trail) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watchlist_curation_log (
                    id              TEXT PRIMARY KEY,
                    ticker          TEXT NOT NULL,
                    cycle_id        TEXT,
                    trigger_reason  TEXT NOT NULL,
                    decision        TEXT NOT NULL,
                    rationale       TEXT,
                    suggested_tier  TEXT,
                    recent_decisions JSONB,
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_curation_log_ticker "
                "ON watchlist_curation_log(ticker)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_curation_log_created "
                "ON watchlist_curation_log(created_at DESC)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Ticker Reports (Per-Ticker Cycle Audit Reports) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_reports (
                    id              TEXT PRIMARY KEY,
                    cycle_id        TEXT NOT NULL,
                    ticker          TEXT NOT NULL,
                    action          TEXT,
                    confidence      INTEGER,
                    report_markdown TEXT,
                    result_summary  JSONB,
                    is_summary      BOOLEAN DEFAULT FALSE,
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(cycle_id, ticker)
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticker_reports_cycle "
                "ON ticker_reports(cycle_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticker_reports_ticker "
                "ON ticker_reports(ticker)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticker_reports_created "
                "ON ticker_reports(created_at DESC)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Ticker User Notes (per-ticker collaboration comments) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_user_notes (
                    ticker      TEXT PRIMARY KEY,
                    note        TEXT NOT NULL,
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── User Intents (chat-to-trading intent extraction) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_intents (
                    id              TEXT PRIMARY KEY,
                    ticker          TEXT,
                    intent_type     TEXT NOT NULL,
                    raw_message     TEXT,
                    extracted_insight TEXT,
                    source          TEXT DEFAULT 'chat',
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_intents_ticker "
                "ON user_intents(ticker)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_intents_created "
                "ON user_intents(created_at DESC)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Task progress columns on system_commands ──
    _safe_add_column(conn, "system_commands", "progress", "INTEGER DEFAULT 0")
    _safe_add_column(conn, "system_commands", "progress_message", "TEXT")

    # ── Simulated Debate Transcripts (MiroFish simulation parity) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS simulation_transcripts (
                    id                  SERIAL PRIMARY KEY,
                    ticker              TEXT NOT NULL,
                    cycle_id            TEXT,
                    transcript_json     TEXT,
                    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sim_transcripts_ticker "
                "ON simulation_transcripts(ticker)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sim_transcripts_cycle "
                "ON simulation_transcripts(cycle_id)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Episodic Observations (Developer 2 post-cycle learner) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS episodic_observations (
                    id                      TEXT PRIMARY KEY,
                    created_at              TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    cycle_id                TEXT,
                    ticker                  TEXT,
                    sector                  TEXT,
                    source_type             TEXT,
                    observation_text        TEXT,
                    rationale_excerpt       TEXT,
                    confidence_at_creation  DOUBLE PRECISION,
                    outcome_label           TEXT,
                    outcome_score           DOUBLE PRECISION,
                    promoted_to_memory      BOOLEAN DEFAULT FALSE
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Debate Tool Cache (Centralized Tool Results) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS debate_tool_cache (
                    id              SERIAL PRIMARY KEY,
                    cycle_id        TEXT NOT NULL,
                    ticker          TEXT NOT NULL,
                    tool_name       TEXT NOT NULL,
                    cache_key       TEXT NOT NULL,
                    tool_output     TEXT NOT NULL,
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(cycle_id, ticker, cache_key)
                )
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Universal Data Sources + Deduplication migrations ──
    _safe_add_column(conn, "news_articles", "content_hash", "TEXT")
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_news_content_hash ON news_articles(content_hash)")
            # url_fanout_exceeded() counts rows per URL on every news insert.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_news_articles_url ON news_articles(url)")
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Unified Social Posts Table (Twitter, StockTwits) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS social_posts (
                    id                  TEXT PRIMARY KEY,     -- sha256(platform + post_id + ticker)
                    platform            TEXT NOT NULL,        -- 'twitter' | 'stocktwits' | 'threads'
                    platform_post_id    TEXT NOT NULL,        -- Original post ID on the platform
                    ticker              TEXT,
                    author_username     TEXT,
                    author_display_name TEXT,
                    author_followers    INT,
                    content             TEXT,
                    like_count          INT DEFAULT 0,
                    repost_count        INT DEFAULT 0,       -- retweet/repost
                    reply_count         INT DEFAULT 0,
                    view_count          INT DEFAULT 0,
                    cashtags            TEXT,                 -- JSON array of $TAGS
                    hashtags            TEXT,                 -- JSON array of #TAGS
                    sentiment_score     DOUBLE PRECISION,     -- nullable, filled by LLM later
                    quality_score       INT,
                    quality_status      TEXT,
                    is_repost           BOOLEAN DEFAULT FALSE,
                    posted_at           TIMESTAMP,
                    collected_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    content_hash        TEXT                  -- sha256 of normalized content for fuzzy dedup
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_social_platform ON social_posts(platform)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_social_ticker ON social_posts(ticker)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_social_posted ON social_posts(posted_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_social_content_hash ON social_posts(content_hash)")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_social_platform_post ON social_posts(platform, platform_post_id, ticker)")
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Insider Trades Table (OpenInsider) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS insider_trades (
                    id              TEXT PRIMARY KEY,   -- sha256(ticker + insider_name + trade_date)
                    ticker          TEXT NOT NULL,
                    insider_name    TEXT,
                    insider_title   TEXT,
                    trade_type      TEXT,              -- 'P' (purchase) | 'S' (sale) etc.
                    price           DOUBLE PRECISION,
                    qty             INT,
                    value           DOUBLE PRECISION,
                    shares_owned    INT,
                    delta_pct       DOUBLE PRECISION,  -- % change in ownership
                    trade_date      DATE,
                    filing_date     DATE,
                    source          TEXT DEFAULT 'openinsider',
                    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_insider_ticker ON insider_trades(ticker)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_insider_trade_date ON insider_trades(trade_date)")
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Economic Calendar Table (TradingEconomics) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS economic_calendar (
                    id              TEXT PRIMARY KEY,
                    event_name      TEXT,
                    country         TEXT,
                    event_date      TIMESTAMP,
                    actual          DOUBLE PRECISION,
                    forecast        DOUBLE PRECISION,
                    previous        DOUBLE PRECISION,
                    importance      TEXT,   -- 'high' | 'medium' | 'low'
                    source          TEXT DEFAULT 'tradingeconomics',
                    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_econ_cal_date ON economic_calendar(event_date)")
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


    # --- Auto-synced missing tables from schema_pg.sql ---
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    ticker      TEXT,
                    date        DATE,
                    open        DOUBLE PRECISION,
                    high        DOUBLE PRECISION,
                    low         DOUBLE PRECISION,
                    close       DOUBLE PRECISION,
                    volume      BIGINT,
                    source      TEXT,
                    PRIMARY KEY (ticker, date, source)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fundamentals (
                    ticker              TEXT,
                    snapshot_date       DATE,
                    source              TEXT DEFAULT 'yfinance',
                    market_cap          DOUBLE PRECISION,
                    pe_ratio            DOUBLE PRECISION,
                    forward_pe          DOUBLE PRECISION,
                    peg_ratio           DOUBLE PRECISION,
                    price_to_book       DOUBLE PRECISION,
                    price_to_sales      DOUBLE PRECISION,
                    ev_to_ebitda        DOUBLE PRECISION,
                    profit_margin       DOUBLE PRECISION,
                    roe                 DOUBLE PRECISION,
                    roa                 DOUBLE PRECISION,
                    revenue             DOUBLE PRECISION,
                    revenue_growth      DOUBLE PRECISION,
                    net_income          DOUBLE PRECISION,
                    debt_to_equity      DOUBLE PRECISION,
                    current_ratio       DOUBLE PRECISION,
                    beta                DOUBLE PRECISION,
                    week_52_high        DOUBLE PRECISION,
                    week_52_low         DOUBLE PRECISION,
                    short_float_pct     DOUBLE PRECISION,
                    PRIMARY KEY (ticker, snapshot_date)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS technicals (
                    ticker          TEXT,
                    date            DATE,
                    rsi_14          DOUBLE PRECISION,
                    macd            DOUBLE PRECISION,
                    macd_signal     DOUBLE PRECISION,
                    macd_hist       DOUBLE PRECISION,
                    sma_20          DOUBLE PRECISION,
                    sma_50          DOUBLE PRECISION,
                    sma_200         DOUBLE PRECISION,
                    ema_12          DOUBLE PRECISION,
                    ema_26          DOUBLE PRECISION,
                    bb_upper        DOUBLE PRECISION,
                    bb_mid          DOUBLE PRECISION,
                    bb_lower        DOUBLE PRECISION,
                    atr_14          DOUBLE PRECISION,
                    adx_14          DOUBLE PRECISION,
                    stoch_k         DOUBLE PRECISION,
                    stoch_d         DOUBLE PRECISION,
                    obv             DOUBLE PRECISION,
                    vwap            DOUBLE PRECISION,
                    support         DOUBLE PRECISION,
                    resistance      DOUBLE PRECISION,
                    PRIMARY KEY (ticker, date)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS financial_history (
                    ticker              TEXT,
                    period_type         TEXT,
                    period_end          DATE,
                    revenue             DOUBLE PRECISION,
                    gross_profit        DOUBLE PRECISION,
                    operating_income    DOUBLE PRECISION,
                    net_income          DOUBLE PRECISION,
                    eps                 DOUBLE PRECISION,
                    free_cash_flow      DOUBLE PRECISION,
                    PRIMARY KEY (ticker, period_type, period_end)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS balance_sheet (
                    ticker              TEXT,
                    period_end          DATE,
                    total_assets        DOUBLE PRECISION,
                    total_liabilities   DOUBLE PRECISION,
                    total_equity        DOUBLE PRECISION,
                    cash                DOUBLE PRECISION,
                    total_debt          DOUBLE PRECISION,
                    working_capital     DOUBLE PRECISION,
                    PRIMARY KEY (ticker, period_end)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS asset_prices (
                    symbol      TEXT,
                    asset_class TEXT,
                    exchange    TEXT DEFAULT '',
                    date        DATE,
                    open        DOUBLE PRECISION,
                    high        DOUBLE PRECISION,
                    low         DOUBLE PRECISION,
                    close       DOUBLE PRECISION,
                    volume      DOUBLE PRECISION,
                    currency    TEXT DEFAULT 'USD',
                    source      TEXT DEFAULT 'openbb',
                    PRIMARY KEY (symbol, asset_class, date)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS macro_indicators (
                    indicator   TEXT,
                    date        DATE,
                    value       DOUBLE PRECISION,
                    country     TEXT DEFAULT 'US',
                    source      TEXT DEFAULT 'openbb',
                    PRIMARY KEY (indicator, date, country)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS news_articles (
                    id              TEXT PRIMARY KEY,
                    ticker          TEXT,
                    title           TEXT,
                    publisher       TEXT,
                    url             TEXT,
                    published_at    TIMESTAMP,
                    summary         TEXT,
                    llm_summary     TEXT,
                    source          TEXT,
                    summarized_at   TIMESTAMP,
                    quality_status  TEXT,
                    quality_reason  TEXT,
                    quality_score   INTEGER,
                    screenshot      TEXT,
                    cluster_id      TEXT,
                    is_cluster_winner BOOLEAN,
                    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    content_hash    TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scraper_scripts (
                    domain          TEXT PRIMARY KEY,
                    script          TEXT,
                    script_type     TEXT,
                    success_count   INTEGER DEFAULT 0,
                    fail_count      INTEGER DEFAULT 0,
                    status          TEXT DEFAULT 'active',
                    last_success    TIMESTAMP,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reddit_posts (
                    id                  TEXT PRIMARY KEY,
                    ticker              TEXT,
                    subreddit           TEXT,
                    title               TEXT,
                    body                TEXT,
                    score               INTEGER,
                    upvote_ratio        DOUBLE PRECISION,
                    comment_count       INTEGER,
                    flair               TEXT,
                    sentiment_score     DOUBLE PRECISION,
                    award_count         INTEGER,
                    comment_velocity    DOUBLE PRECISION,
                    summary             TEXT,
                    created_utc         TIMESTAMP,
                    summarized_at       TIMESTAMP,
                    quality_status      TEXT,
                    quality_reason      TEXT,
                    quality_score       INTEGER,
                    collected_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS youtube_transcripts (
                    video_id        TEXT PRIMARY KEY,
                    ticker          TEXT,
                    title           TEXT,
                    channel         TEXT,
                    raw_transcript  TEXT,
                    summary         TEXT,
                    tickers_mentioned TEXT,
                    thumbnail_url   TEXT,
                    published_at    TIMESTAMP,
                    duration_secs   INTEGER,
                    summarized_at   TIMESTAMP,
                    quality_status  TEXT,
                    quality_reason  TEXT,
                    quality_score   INTEGER,
                    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS data_archive (
                    id              SERIAL PRIMARY KEY,
                    source_table    TEXT,
                    source_id       TEXT,
                    ticker          TEXT,
                    title           TEXT,
                    content         TEXT,
                    original_date   TIMESTAMP,
                    purge_after     TIMESTAMP,
                    archived_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_table, source_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sec_13f_holdings (
                    cik            TEXT NOT NULL,
                    ticker         TEXT NOT NULL,
                    name_of_issuer TEXT,
                    cusip          TEXT,
                    value_usd      DOUBLE PRECISION,
                    shares         BIGINT,
                    share_type     TEXT,
                    pct_change     DOUBLE PRECISION,
                    is_new_position BOOLEAN,
                    is_exit        BOOLEAN,
                    filing_quarter TEXT NOT NULL,
                    filing_date    DATE,
                    collected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (cik, ticker, filing_quarter)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS congress_trades (
                    id                  TEXT PRIMARY KEY,
                    politician          TEXT,
                    party               TEXT,
                    chamber             TEXT,
                    state               TEXT,
                    ticker              TEXT,
                    transaction_type    TEXT,
                    amount_range        TEXT,
                    trade_date          DATE,
                    disclosure_date     DATE,
                    days_to_disclose    INTEGER
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fund_alerts (
                    id              TEXT PRIMARY KEY,
                    created_at      TIMESTAMP,
                    alert_type      TEXT,
                    ticker          TEXT,
                    entity_name     TEXT,
                    detail          TEXT,
                    severity        TEXT,
                    llm_summary     TEXT,
                    is_read         BOOLEAN DEFAULT FALSE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    id              TEXT PRIMARY KEY,
                    source_table    TEXT,
                    source_id       TEXT,
                    ticker          TEXT,
                    content_preview TEXT,
                    embedding       vector(384),
                    created_at      TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_data (
                    id              TEXT PRIMARY KEY,
                    filename        TEXT,
                    file_type       TEXT,
                    raw_content     TEXT,
                    processed_at    TIMESTAMP,
                    tags            TEXT,
                    embedding       vector(384)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_schedules (
                    id              TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    schedule_type   TEXT NOT NULL,         -- 'cron' | 'interval'
                    cron_expression TEXT,
                    interval_hours  DOUBLE PRECISION,
                    collect         BOOLEAN DEFAULT TRUE,
                    "analyze"       BOOLEAN DEFAULT TRUE,
                    trade           BOOLEAN,                  -- NULL = use armed state
                    tickers         TEXT,                  -- JSONB array
                    max_tickers     INTEGER,                  -- NULL = use .env default
                    discovered_tickers INTEGER DEFAULT 0,
                    market_hours_only BOOLEAN DEFAULT FALSE,
                    is_active       BOOLEAN DEFAULT TRUE,
                    last_run_at     TIMESTAMP,
                    next_run_at     TIMESTAMP,
                    run_count       INTEGER DEFAULT 0,
                    last_status     TEXT,
                    last_error      TEXT,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    ticker        TEXT PRIMARY KEY,
                    status        TEXT DEFAULT 'active',   -- active | paused | removed | banned
                    status_reason TEXT,
                    banned_at     TIMESTAMP,
                    added_at      TIMESTAMP,
                    source        TEXT,
                    notes         TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id              TEXT PRIMARY KEY,
                    bot_id          TEXT,
                    ticker          TEXT,
                    qty             DOUBLE PRECISION,
                    avg_entry_price DOUBLE PRECISION,
                    stop_loss_pct   DOUBLE PRECISION DEFAULT 0.08,
                    opened_at       TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id              TEXT PRIMARY KEY,
                    bot_id          TEXT,
                    ticker          TEXT,
                    side            TEXT,
                    qty             DOUBLE PRECISION,
                    price           DOUBLE PRECISION,
                    signal          TEXT,
                    created_at      TIMESTAMP,
                    filled_at       TIMESTAMP,
                    realized_pnl    DOUBLE PRECISION
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_fills (
                    fill_id         TEXT PRIMARY KEY,
                    order_id        TEXT NOT NULL,
                    bot_id          TEXT NOT NULL,
                    ticker          TEXT NOT NULL,
                    side            TEXT NOT NULL,
                    fill_qty        DOUBLE PRECISION NOT NULL,
                    fill_price      DOUBLE PRECISION NOT NULL,
                    fill_value      DOUBLE PRECISION NOT NULL,
                    fees            DOUBLE PRECISION DEFAULT 0.0,
                    filled_at       TIMESTAMP NOT NULL,
                    cycle_id        TEXT,
                    source          TEXT DEFAULT 'pipeline'
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS position_lots (
                    lot_id          TEXT PRIMARY KEY,
                    bot_id          TEXT NOT NULL,
                    ticker          TEXT NOT NULL,
                    fill_id         TEXT NOT NULL,
                    opened_at       TIMESTAMP NOT NULL,
                    original_qty    DOUBLE PRECISION NOT NULL,
                    remaining_qty   DOUBLE PRECISION NOT NULL,
                    entry_price     DOUBLE PRECISION NOT NULL,
                    status          TEXT DEFAULT 'open',
                    cycle_id        TEXT,
                    is_legacy       BOOLEAN DEFAULT FALSE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lot_closures (
                    closure_id      TEXT PRIMARY KEY,
                    bot_id          TEXT NOT NULL,
                    ticker          TEXT NOT NULL,
                    sell_fill_id    TEXT NOT NULL,
                    lot_id          TEXT NOT NULL,
                    closed_qty      DOUBLE PRECISION NOT NULL,
                    entry_price     DOUBLE PRECISION NOT NULL,
                    exit_price      DOUBLE PRECISION NOT NULL,
                    realized_pnl    DOUBLE PRECISION NOT NULL,
                    closed_at       TIMESTAMP NOT NULL,
                    holding_days    INTEGER
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id              TEXT PRIMARY KEY,
                    bot_id          TEXT,
                    snapshot_ts     TIMESTAMP,
                    cash_balance    DOUBLE PRECISION,
                    total_value     DOUBLE PRECISION,
                    realized_pnl    DOUBLE PRECISION,
                    unrealized_pnl  DOUBLE PRECISION
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_triggers (
                    id              TEXT PRIMARY KEY,
                    bot_id          TEXT,
                    ticker          TEXT,
                    trigger_type    TEXT,
                    price           DOUBLE PRECISION,
                    active          BOOLEAN DEFAULT TRUE,
                    created_at      TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS decision_outcomes (
                    id            TEXT PRIMARY KEY,
                    cycle_id      TEXT,
                    ticker        TEXT,
                    action        TEXT,
                    confidence    INTEGER,
                    entry_price   DOUBLE PRECISION,
                    exit_price    DOUBLE PRECISION,
                    pnl_pct       DOUBLE PRECISION,
                    outcome       TEXT,
                    lesson_stored TEXT,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at   TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bots (
                    bot_id          TEXT PRIMARY KEY,
                    display_name    TEXT,
                    model_name      TEXT,
                    status          TEXT DEFAULT 'idle',
                    cash_balance    DOUBLE PRECISION DEFAULT 100000.0,
                    starting_cash   DOUBLE PRECISION DEFAULT 100000.0,
                    total_pnl       DOUBLE PRECISION DEFAULT 0.0,
                    win_rate        DOUBLE PRECISION DEFAULT 0.0,
                    total_trades    INTEGER DEFAULT 0,
                    is_active       BOOLEAN DEFAULT TRUE,
                    created_at      TIMESTAMP,
                    last_run_at     TIMESTAMP,
                    description     TEXT DEFAULT ''
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS context_blobs (
                    context_hash    TEXT PRIMARY KEY,
                    content         TEXT,
                    byte_size       INTEGER,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS llm_audit_logs (
                    id                  TEXT PRIMARY KEY,
                    cycle_id            TEXT,
                    bot_id              TEXT,
                    ticker              TEXT,
                    agent_step          TEXT,
                    model               TEXT,
                    system_prompt_hash  TEXT,
                    context_hash        TEXT,
                    raw_response        TEXT,
                    tokens_used         INTEGER,
                    execution_ms        INTEGER,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    endpoint_name       TEXT,
                    prompt_tokens       INTEGER,
                    completion_tokens   INTEGER,
                    queue_wait_ms       INTEGER,
                    tokens_per_second   DOUBLE PRECISION
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS analysis_results (
                    id          TEXT PRIMARY KEY,
                    cycle_id    TEXT,
                    bot_id      TEXT,
                    ticker      TEXT,
                    agent_name  TEXT,
                    result_json TEXT,
                    confidence  INTEGER,
                    created_at  TIMESTAMP,
                    triage_tier TEXT,
                    thesis_verdict TEXT,
                    thesis_confidence INTEGER,
                    thesis_summary TEXT,
                    thesis_updated_at TIMESTAMP,
                    thesis_unchanged BOOLEAN
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS decision_evaluations (
                    decision_id        TEXT PRIMARY KEY,  -- fk to llm_audit_logs.id
                    cycle_id           TEXT,
                    ticker             TEXT,
                    timestamp          TIMESTAMP,
                    difficulty         TEXT,
                    evidence_gathering TEXT,
                    policy_understanding BOOLEAN,
                    first_principles_reasoning TEXT,
                    prompt_snapshot_link TEXT,
                    raw_output_link    TEXT,
                    red_cards          TEXT,              -- JSONB array of red cards
                    judge_a_score      DOUBLE PRECISION,
                    judge_b_score      DOUBLE PRECISION,
                    discrepancy_trigger BOOLEAN DEFAULT FALSE,
                    final_quality_score DOUBLE PRECISION
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS discovered_tickers (
                    ticker          TEXT,
                    source          TEXT,
                    context         TEXT,
                    score           DOUBLE PRECISION,
                    discovered_at   TIMESTAMP,
                    PRIMARY KEY (ticker, source)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scheduler_history (
                    id          TEXT PRIMARY KEY,
                    job_name    TEXT,
                    started_at  TIMESTAMP,
                    finished_at TIMESTAMP,
                    status      TEXT,
                    notes       TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS flash_briefings (
                    id              SERIAL PRIMARY KEY,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    report_content  TEXT,
                    source_urls     TEXT[],
                    article_count   INTEGER
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS morning_briefings (
                    id              SERIAL PRIMARY KEY,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    report_content  TEXT,
                    tickers_evaluated TEXT[]
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS data_source_status (
                    source          TEXT,
                    ticker          TEXT,
                    last_success    TIMESTAMP,
                    last_failure    TIMESTAMP,
                    rows_fetched    INTEGER DEFAULT 0,
                    error_msg       TEXT,
                    PRIMARY KEY (source, ticker)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS autoresearch_reports (
                    id                      TEXT PRIMARY KEY,
                    cycle_id                TEXT,
                    status                  TEXT,
                    phase                   TEXT,
                    error                   TEXT,
                    data_gaps               TEXT,
                    decision_issues         TEXT,
                    llm_issues              TEXT,
                    data_quality_score      DOUBLE PRECISION,
                    decision_quality_score  DOUBLE PRECISION,
                    llm_performance_score   DOUBLE PRECISION,
                    performance_metrics     TEXT,
                    reflection              TEXT,
                    recovery_stats          TEXT,
                    overall_score           DOUBLE PRECISION,
                    created_at              TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS autoresearch_cycle_summaries (
                    id              TEXT PRIMARY KEY,
                    cycle_id        TEXT UNIQUE,
                    total_tickers   INTEGER,
                    buy_count       INTEGER,
                    sell_count      INTEGER,
                    hold_count      INTEGER,
                    avg_confidence  DOUBLE PRECISION,
                    top_ticker      TEXT,
                    top_confidence  INTEGER,
                    lesson_summary  TEXT,
                    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS execution_errors (
                    id              TEXT PRIMARY KEY,
                    cycle_id        TEXT,
                    phase           TEXT,
                    ticker          TEXT,
                    error_type      TEXT,
                    error_message   TEXT,
                    stack_trace     TEXT,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_audit_log (
                    id TEXT PRIMARY KEY,
                    cycle_id TEXT,
                    timestamp TIMESTAMPTZ,
                    audit_type TEXT,
                    event_type TEXT,
                    phase TEXT,
                    ticker TEXT,
                    severity TEXT,
                    message TEXT,
                    data JSONB
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rag_ab_results (
                    id              TEXT PRIMARY KEY,
                    ticker          TEXT,
                    query           TEXT,
                    strategy        TEXT,
                    chunks_returned INTEGER,
                    top_score       DOUBLE PRECISION,
                    avg_score       DOUBLE PRECISION,
                    retrieval_ms    INTEGER,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_feedback (
                    id              TEXT PRIMARY KEY,
                    ticker          TEXT,
                    feedback_type   TEXT,   -- note | constraint | thesis | signal | ban_reason | flag_reason
                    content         TEXT,
                    sentiment       TEXT,   -- bullish | bearish | neutral (nullable)
                    confidence      INTEGER,   -- 0-100 (nullable)
                    constraint_type TEXT,   -- no_sell | max_position | min_confidence (nullable)
                    constraint_val  TEXT,   -- constraint value (nullable)
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at      TIMESTAMP,
                    is_active       BOOLEAN DEFAULT TRUE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_bans (
                    ticker        TEXT PRIMARY KEY,
                    reason        TEXT,
                    ban_type      TEXT DEFAULT 'manual',   -- manual | auto | pattern
                    pattern_tags  TEXT,                    -- JSONB: ["sub_penny", "no_volume"]
                    market_cap    DOUBLE PRECISION,
                    price_at_ban  DOUBLE PRECISION,
                    volume_at_ban BIGINT,
                    banned_by     TEXT DEFAULT 'user',
                    banned_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS data_flags (
                    id           TEXT PRIMARY KEY,
                    source_table TEXT,    -- news_articles | reddit_posts | youtube_transcripts
                    source_id    TEXT,
                    ticker       TEXT,
                    flag_type    TEXT,    -- spam | clickbait | irrelevant | outdated | fake
                    reason       TEXT,
                    flagged_by   TEXT DEFAULT 'user',
                    flagged_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    auto_action  TEXT     -- excluded | deleted | source_warned
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS source_trust (
                    source_type  TEXT,    -- publisher | subreddit | youtube_channel
                    source_name  TEXT,
                    trust_score  DOUBLE PRECISION DEFAULT 1.0,  -- 1.0 = trusted, 0.0 = blocked
                    total_flags  INTEGER DEFAULT 0,
                    total_items  INTEGER DEFAULT 0,
                    flag_rate    DOUBLE PRECISION DEFAULT 0.0,
                    quality_wins INTEGER DEFAULT 0,
                    win_rate     DOUBLE PRECISION DEFAULT 0.0,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source_type, source_name)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ban_patterns (
                    id           TEXT PRIMARY KEY,
                    pattern_name TEXT,
                    conditions   TEXT,     -- JSONB: {"price_lt": 0.50, "volume_lt": 10000}
                    source_bans  INTEGER DEFAULT 0,
                    auto_filter  BOOLEAN DEFAULT TRUE,  -- opt-in by default
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id              TEXT PRIMARY KEY,
                    title           TEXT DEFAULT 'New Chat',
                    created_at      TIMESTAMP,
                    ended_at        TIMESTAMP,
                    message_count   INTEGER DEFAULT 0,
                    is_active       BOOLEAN DEFAULT FALSE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id              TEXT PRIMARY KEY,
                    session_id      TEXT,
                    ticker          TEXT,
                    user_message    TEXT,
                    bot_response    TEXT,
                    context_hash    TEXT,
                    model_used      TEXT,
                    tokens_used     INTEGER,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS semantic_memory (
                    id               TEXT PRIMARY KEY,
                    ticker           TEXT,
                    type             TEXT,     -- fact | rule | preference | threshold
                    content          TEXT,
                    confidence       DOUBLE PRECISION DEFAULT 0.5,
                    source_agent     TEXT,
                    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    access_count     INTEGER DEFAULT 0
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS episodic_memory (
                    id                 TEXT PRIMARY KEY,
                    cycle_id           TEXT,
                    ticker             TEXT,
                    timestamp          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    summary            TEXT,
                    key_decisions      TEXT,       -- JSONB array
                    outcome            TEXT,       -- positive/negative/neutral
                    outcome_score      DOUBLE PRECISION,        -- -1.0 to 1.0
                    agents_involved    TEXT        -- JSONB array
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS procedural_memory (
                    id                 TEXT PRIMARY KEY,
                    ticker             TEXT,
                    trigger_pattern    TEXT,
                    procedure          TEXT,       -- JSONB array of steps
                    success_count      INTEGER DEFAULT 0,
                    failure_count      INTEGER DEFAULT 0,
                    success_rate       DOUBLE PRECISION DEFAULT 0.0,
                    last_triggered_at  TIMESTAMP,
                    created_by_agent   TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS prospective_memory (
                    id                 TEXT PRIMARY KEY,
                    ticker             TEXT,
                    intention          TEXT,
                    trigger_condition  TEXT,
                    priority           TEXT,      -- critical/high/medium/low
                    status             TEXT,      -- pending/triggered/expired
                    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    trigger_at         TIMESTAMP,
                    context            TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_context (
                    id              TEXT PRIMARY KEY,         -- UUID
                    cycle_id        TEXT NOT NULL,
                    agent_name      TEXT NOT NULL,
                    ticker          TEXT NOT NULL,
                    raw_response    TEXT NOT NULL,            -- Full agent output (Layer 2)
                    summary         TEXT,                     -- Capsule Layer 1 summary
                    signal          TEXT,                     -- BUY | SELL | HOLD | NEUTRAL | UNKNOWN
                    confidence      DOUBLE PRECISION,         -- 0.0–1.0
                    flags           TEXT,                     -- JSON array of flags
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS youtube_channels (
                    channel_handle  TEXT PRIMARY KEY,
                    display_name    TEXT,
                    added_by        TEXT DEFAULT 'system',
                    is_active       BOOLEAN DEFAULT TRUE,
                    total_videos    INTEGER DEFAULT 0,
                    last_scraped    TIMESTAMP,
                    added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS discovered_channels (
                    channel_handle  TEXT PRIMARY KEY,
                    display_name    TEXT,
                    discovery_count INTEGER DEFAULT 1,
                    avg_view_count  DOUBLE PRECISION,
                    first_seen      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status          TEXT DEFAULT 'pending'
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ontology_nodes (
                    id                  TEXT PRIMARY KEY,
                    node_type           TEXT,
                    label               TEXT,
                    activation          DOUBLE PRECISION DEFAULT 0.0,
                    embedding           vector(384),
                    metadata_json       TEXT,
                    validated_count     INTEGER DEFAULT 0,
                    contradicted_count  INTEGER DEFAULT 0,
                    disproven           BOOLEAN DEFAULT FALSE,
                    source_cycle_id     TEXT,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ontology_edges (
                    id                  TEXT PRIMARY KEY,
                    source_id           TEXT,
                    target_id           TEXT,
                    relation            TEXT,
                    weight              DOUBLE PRECISION,
                    decay               DOUBLE PRECISION,
                    evidence_count      INTEGER DEFAULT 1,
                    metadata_json       TEXT,
                    source_cycle_id     TEXT,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS company_registry (
                    symbol        TEXT PRIMARY KEY,
                    company_name  TEXT NOT NULL,
                    aliases       TEXT,
                    sector        TEXT,
                    market_cap    DOUBLE PRECISION DEFAULT 0,
                    is_sp500      BOOLEAN DEFAULT FALSE,
                    verified      BOOLEAN DEFAULT FALSE,
                    rejected      BOOLEAN DEFAULT FALSE,
                    source        TEXT DEFAULT 'sp500_load',
                    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_metadata (
                    ticker          TEXT PRIMARY KEY,
                    name            TEXT,
                    sector          TEXT,
                    industry        TEXT,
                    market_cap      BIGINT,
                    market_cap_tier TEXT,  -- mega/large/mid/small/micro
                    asset_class     TEXT,  -- stock/crypto/commodity/etf
                    sp500           BOOLEAN DEFAULT FALSE,
                    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_correlations (
                    ticker_a    TEXT,
                    ticker_b    TEXT,
                    correlation DOUBLE PRECISION,
                    tier        TEXT,     -- highly_correlated/correlated/weakly_correlated/inversely_correlated
                    period      TEXT,     -- '30d' or '90d'
                    data_points INTEGER,
                    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (ticker_a, ticker_b, period)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sector_performance (
                    sector              TEXT,
                    date                DATE,
                    avg_return_1d       DOUBLE PRECISION,
                    avg_return_5d       DOUBLE PRECISION,
                    avg_return_30d      DOUBLE PRECISION,
                    avg_return_60d      DOUBLE PRECISION,
                    avg_return_6mo      DOUBLE PRECISION,
                    avg_return_1y       DOUBLE PRECISION,
                    relative_strength_1y DOUBLE PRECISION,
                    breadth_pct         DOUBLE PRECISION,
                    top_gainer          TEXT,
                    top_gainer_return   DOUBLE PRECISION,
                    top_loser           TEXT,
                    top_loser_return    DOUBLE PRECISION,
                    avg_volume_ratio    DOUBLE PRECISION,
                    momentum_signal     TEXT,
                    stock_count         INTEGER,
                    computed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (sector, date)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sector_correlations (
                    sector_a    TEXT,
                    sector_b    TEXT,
                    correlation DOUBLE PRECISION,
                    tier        TEXT,
                    period      TEXT,
                    data_points INTEGER,
                    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (sector_a, sector_b, period)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_commodity_correlations (
                    ticker                  TEXT,
                    commodity               TEXT,
                    correlation             DOUBLE PRECISION,
                    sensitivity             TEXT,
                    period                  TEXT,
                    data_points             INTEGER,
                    -- Quant methods (Phase 2: advanced correlation engine)
                    cointegration_pvalue    DOUBLE PRECISION,          -- Engle-Granger p-value (lower = stronger)
                    cointegrated            BOOLEAN,         -- p < 0.05
                    lead_lag_days           INTEGER,         -- negative = commodity leads stock
                    lead_lag_correlation    DOUBLE PRECISION,          -- correlation at best lag
                    vol_adj_correlation     DOUBLE PRECISION,          -- volatility-weighted correlation
                    correlation_stability   DOUBLE PRECISION,          -- std dev of rolling correlations (lower = more stable)
                    distance_correlation    DOUBLE PRECISION,          -- non-linear dependency (0-1)
                    quant_score             INTEGER,         -- composite score (0-100)
                    method_details          TEXT,         -- JSONB with full breakdown
                    computed_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (ticker, commodity, period)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sector_rotation_signals (
                    id                  TEXT PRIMARY KEY,
                    from_sector         TEXT,
                    to_sector           TEXT,
                    from_return_5d      DOUBLE PRECISION,
                    to_return_5d        DOUBLE PRECISION,
                    correlation         DOUBLE PRECISION,
                    commodity_trigger   TEXT,
                    confidence          TEXT,
                    evidence_json       TEXT,
                    detected_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS deleted_data (
                    source          TEXT NOT NULL,     -- 'news', 'reddit', 'youtube'
                    item_id         TEXT NOT NULL,     -- primary key from the source table
                    title           TEXT,              -- for audit trail
                    deleted_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reason          TEXT DEFAULT 'user_delete',
                    PRIMARY KEY (source, item_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_notes (
                    id              TEXT PRIMARY KEY,
                    ticker          TEXT,
                    note_type       TEXT,
                    content         TEXT,
                    sentiment       TEXT,
                    confidence      DOUBLE PRECISION,
                    is_active       BOOLEAN DEFAULT TRUE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_constraints (
                    id              TEXT PRIMARY KEY,
                    ticker          TEXT,
                    constraint_type TEXT,
                    value           TEXT,
                    reason          TEXT,
                    is_active       BOOLEAN DEFAULT TRUE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rss_feeds (
                    id              TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    url             TEXT NOT NULL UNIQUE,
                    is_active       BOOLEAN DEFAULT TRUE,
                    added_by        TEXT DEFAULT 'system',
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monitored_subreddits (
                    id              TEXT PRIMARY KEY,
                    subreddit       TEXT NOT NULL UNIQUE,
                    is_active       BOOLEAN DEFAULT TRUE,
                    added_by        TEXT DEFAULT 'system',
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sector_breadth (
                    sector          TEXT,
                    date            DATE,
                    pct_above_sma50 DOUBLE PRECISION,
                    pct_above_sma200 DOUBLE PRECISION,
                    new_highs       INTEGER,
                    new_lows        INTEGER,
                    net_highs       INTEGER,
                    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (sector, date)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS market_regime (
                    date                DATE PRIMARY KEY,
                    vix_level           DOUBLE PRECISION,
                    vix_signal          TEXT,
                    vix_zscore          DOUBLE PRECISION,
                    vix_term_ratio      DOUBLE PRECISION,
                    vix_term_signal     TEXT,
                    yield_2y            DOUBLE PRECISION,
                    yield_10y           DOUBLE PRECISION,
                    yield_2y10y_spread  DOUBLE PRECISION,
                    yield_signal        TEXT,
                    dollar_index        DOUBLE PRECISION,
                    dollar_change_5d    DOUBLE PRECISION,
                    sp500_level         DOUBLE PRECISION,
                    sp500_change_5d     DOUBLE PRECISION,
                    breadth_sp500       DOUBLE PRECISION,
                    regime_label        TEXT,
                    computed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cross_asset_correlations (
                    asset_a         TEXT,
                    asset_b         TEXT,
                    correlation     DOUBLE PRECISION,
                    tier            TEXT,
                    period          TEXT,
                    data_points     INTEGER,
                    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (asset_a, asset_b, period)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_benchmarks (
                    cycle_id            TEXT PRIMARY KEY,
                    started_at          TIMESTAMP,
                    finished_at         TIMESTAMP,
                    total_ms            INTEGER,
                    collect_ms          INTEGER,
                    analyze_ms          INTEGER,
                    trade_ms            INTEGER,
                    ticker_count        INTEGER,
                    avg_ticker_ms       INTEGER,
                    steps_total         INTEGER,
                    steps_skipped       INTEGER,
                    steps_ok            INTEGER,
                    steps_error         INTEGER,
                    total_tokens        INTEGER,
                    cache_hit_pct       DOUBLE PRECISION,          -- % of collector steps skipped (cache)
                    status              TEXT           -- done | error | stopped
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_ticker_benchmarks (
                    cycle_id            TEXT,
                    ticker              TEXT,
                    collect_ms          INTEGER,
                    analyze_ms          INTEGER,
                    total_ms            INTEGER,
                    steps_skipped       INTEGER,
                    steps_ok            INTEGER,
                    tokens_used         INTEGER,
                    action              TEXT,          -- BUY | SELL | HOLD
                    confidence          INTEGER,
                    PRIMARY KEY (cycle_id, ticker)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_health (
                    ticker              TEXT PRIMARY KEY,
                    -- Data quality signals (accumulated across cycles)
                    total_cycles        INTEGER DEFAULT 0,
                    news_article_count  INTEGER DEFAULT 0,
                    reddit_post_count   INTEGER DEFAULT 0,
                    youtube_count       INTEGER DEFAULT 0,
                    zero_news_streak    INTEGER DEFAULT 0,
                    collection_failures INTEGER DEFAULT 0,
                    -- Analysis quality signals
                    total_analyses      INTEGER DEFAULT 0,
                    avg_confidence      DOUBLE PRECISION DEFAULT 0,
                    hold_streak         INTEGER DEFAULT 0,
                    last_action         TEXT,
                    last_confidence     INTEGER DEFAULT 0,
                    buy_count           INTEGER DEFAULT 0,
                    sell_count          INTEGER DEFAULT 0,
                    -- Computed score
                    health_score        INTEGER DEFAULT 50,
                    health_tier         TEXT DEFAULT 'new',
                    -- Timestamps
                    first_seen_at       TIMESTAMP,
                    last_analyzed_at    TIMESTAMP,
                    last_scored_at      TIMESTAMP,
                    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sec_13f_filers (
                    cik            TEXT PRIMARY KEY,
                    filer_name     TEXT NOT NULL,
                    last_checked   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active      BOOLEAN DEFAULT TRUE,
                    latest_quarter TEXT,
                    next_expected_filing DATE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_state (
                    singleton_id TEXT PRIMARY KEY,  -- Always 'current'
                    status TEXT,
                    cycle_id TEXT,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    requested_pipeline_version TEXT,
                    effective_pipeline_version TEXT,
                    benchmark_group TEXT,
                    execution_mode TEXT,
                    v2_stage INTEGER,
                    tickers JSONB,
                    progress TEXT,
                    error TEXT,
                    phase TEXT,
                    operational_phase TEXT,
                    step_count INTEGER,
                    total_steps INTEGER,
                    collect_flag BOOLEAN,
                    analyze_flag BOOLEAN,
                    trade_flag BOOLEAN,
                    max_tickers INTEGER,
                    discovered_tickers INTEGER,
                    dynamic_selection_mode BOOLEAN DEFAULT FALSE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_events (
                    id TEXT PRIMARY KEY,
                    cycle_id TEXT,
                    timestamp TIMESTAMP,
                    phase TEXT,
                    step TEXT,
                    detail TEXT,
                    status TEXT,
                    data_json JSONB,
                    elapsed_ms INTEGER
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_resume_state (
                    cycle_id              TEXT PRIMARY KEY,
                    status                TEXT DEFAULT 'interrupted',   -- 'interrupted' | 'expired'
                    completed_phases      JSONB DEFAULT '[]',           -- ["collecting"]
                    completed_tickers     JSONB DEFAULT '{}',           -- {"analyzing": ["NVDA","PLTR"]}
                    cycle_config          JSONB DEFAULT '{}',           -- full cycle params snapshot
                    checkpoint_ts         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    original_started_at   TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_run_summaries (
                    cycle_id                TEXT PRIMARY KEY,
                    trigger_type            TEXT DEFAULT 'manual',   -- 'manual' | 'scheduler'
                    schedule_id             TEXT,                    -- FK to cycle_schedules.id (nullable)
                    started_at              TIMESTAMP,
                    finished_at             TIMESTAMP,
                    status                  TEXT,                    -- 'done' | 'failed' | 'stopped' | 'error'
                    elapsed_ms              INTEGER,
                    -- Requested intent (what the user/scheduler asked for)
                    tickers_requested       JSONB,                       -- original ticker list
                    tickers_final           JSONB,                       -- after discovery/merge/filter
                    collect_requested       BOOLEAN,
                    analyze_requested       BOOLEAN,
                    trade_requested         BOOLEAN,
                    -- Jetson/vLLM health
                    jetson_healthy_start    BOOLEAN,                    -- health check at cycle start
                    -- Collection phase outcomes
                    collector_ok            INTEGER DEFAULT 0,
                    collector_skipped       INTEGER DEFAULT 0,
                    collector_error         INTEGER DEFAULT 0,
                    collector_failures      JSONB,                       -- ["reddit", "youtube"]
                    -- Analysis phase outcomes
                    analysis_results_count  INTEGER DEFAULT 0,
                    buy_count               INTEGER DEFAULT 0,
                    sell_count              INTEGER DEFAULT 0,
                    hold_count              INTEGER DEFAULT 0,
                    review_count            INTEGER DEFAULT 0,
                    -- Trading phase outcomes
                    trade_attempted         INTEGER DEFAULT 0,
                    trade_executed          INTEGER DEFAULT 0,
                    trade_failed            INTEGER DEFAULT 0,
                    trade_skip_categories   JSONB,                       -- {"holds": N, "human_review": N, ...}
                    -- Diagnosis
                    no_trade_reason         TEXT,                    -- 'hold_only' | 'trading_disarmed' | 'no_analysis' | 'jetson_down' | 'zero_results' | null
                    primary_failure_reason  TEXT,                    -- free text: first critical error
                    -- Report (future-ready)
                    report_generated        BOOLEAN DEFAULT FALSE,
                    report_published        BOOLEAN DEFAULT FALSE,
                    -- Catch-all
                    summary_json            JSONB
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_evaluations (
                    id              TEXT PRIMARY KEY,
                    cycle_id        TEXT,
                    timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_score     DOUBLE PRECISION,
                    risk_score      DOUBLE PRECISION,
                    performance_score DOUBLE PRECISION,
                    robustness_score DOUBLE PRECISION,
                    logic_score     DOUBLE PRECISION,
                    operational_score DOUBLE PRECISION,
                    full_analysis   JSONB
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS llm_attention_weights (
                    id                  TEXT PRIMARY KEY,
                    cycle_id            TEXT,
                    agent_step          TEXT,
                    node_id             TEXT,
                    weight              DOUBLE PRECISION,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS evolution_nodes (
                    id              TEXT PRIMARY KEY,
                    session_id      TEXT NOT NULL,
                    round           INTEGER NOT NULL,
                    parent_id       TEXT,
                    motivation      TEXT,
                    code            TEXT,
                    metrics         TEXT,
                    score           DOUBLE PRECISION,
                    status          TEXT,
                    analysis        TEXT,
                    timestamp       TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS evolution_lessons (
                    id          TEXT PRIMARY KEY,
                    session_id  TEXT,
                    round       INTEGER,
                    score       DOUBLE PRECISION,
                    status      TEXT,
                    lesson_text TEXT,
                    timestamp   TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scraper_queue (
                    id                  TEXT PRIMARY KEY,
                    ticker              TEXT NOT NULL,
                    data_type_requested TEXT NOT NULL,    -- 'news' | 'reddit' | 'youtube' | 'price' | 'fundamentals' | 'options'
                    priority            INTEGER DEFAULT 5,   -- 1=JIT (blocking analysis), 5=routine sweep
                    status              TEXT DEFAULT 'PENDING',  -- PENDING | PROCESSING | RESOLVED | FAILED
                    requested_by_lens   TEXT,             -- which lens requested this data
                    retry_count         INTEGER DEFAULT 0,
                    max_retries         INTEGER DEFAULT 3,
                    error_message       TEXT,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at          TIMESTAMP,
                    resolved_at         TIMESTAMP,
                    cooldown_until      TIMESTAMP            -- prevent infinite request loops
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_candidates (
                    id                  TEXT PRIMARY KEY,
                    cycle_id            TEXT,
                    ticker              TEXT NOT NULL,
                    lens_name           TEXT NOT NULL,    -- 'fundamental' | 'technical' | 'momentum' | 'risk' | custom
                    system_prompt_hash  TEXT,             -- SHA256 of the system prompt used
                    summary             TEXT,
                    signal              TEXT,             -- BUY | SELL | HOLD
                    confidence_score    INTEGER DEFAULT 0,
                    analysis_result_id  TEXT,             -- FK to analysis_results.id
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_performance (
                    id                      TEXT PRIMARY KEY,
                    strategy_candidate_id   TEXT,             -- FK to strategy_candidates
                    decision_outcome_id     TEXT,             -- FK to decision_outcomes
                    agent_prompt_hash       TEXT,
                    ticker                  TEXT,
                    signal                  TEXT,             -- BUY | SELL | HOLD
                    entry_price             DOUBLE PRECISION,
                    exit_price              DOUBLE PRECISION,
                    hold_days               INTEGER,
                    return_pct              DOUBLE PRECISION,
                    win                     BOOLEAN,
                    active                  BOOLEAN DEFAULT TRUE,
                    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at             TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS generated_agent_prompts (
                    id                  TEXT PRIMARY KEY,
                    name                TEXT NOT NULL,
                    lens_type           TEXT DEFAULT 'custom',  -- analytical lens category
                    system_prompt       TEXT NOT NULL,
                    prompt_hash         TEXT NOT NULL,           -- SHA256 for dedup
                    performance_score   DOUBLE PRECISION DEFAULT 0.0,
                    total_trades        INTEGER DEFAULT 0,
                    win_rate            DOUBLE PRECISION DEFAULT 0.0,
                    active              BOOLEAN DEFAULT TRUE,
                    created_by          TEXT DEFAULT 'meta_agent',
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at        TIMESTAMP,
                    benched_at          TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS swarm_scorecards (
                    id                      TEXT PRIMARY KEY,
                    ticker                  TEXT NOT NULL,
                    cycle_id                TEXT NOT NULL,
                    model_label             TEXT NOT NULL,    -- quant_26B | macro_35B | cio_120B
                    model_id                TEXT,             -- actual model name from vLLM
                    predicted_action        TEXT,             -- BUY/SELL/HOLD
                    predicted_confidence    INTEGER,
                    predicted_price_target  DOUBLE PRECISION,
                    predicted_stop_loss     DOUBLE PRECISION,
                    key_signals             TEXT,             -- JSON array of signals
                    rationale               TEXT,
                    actual_action           TEXT,             -- Filled by grading pass
                    actual_price_change_pct DOUBLE PRECISION, -- Filled by grading pass
                    accuracy_score          DOUBLE PRECISION, -- 0-100, filled by grading
                    action_correct          BOOLEAN,          -- Filled by grading
                    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    graded_at               TIMESTAMP         -- NULL until graded
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS autoresearch_reports (
                    id                      TEXT PRIMARY KEY,
                    cycle_id                TEXT NOT NULL,
                    created_at              TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                
                    -- Summary scores (0-100)
                    data_quality_score      DOUBLE PRECISION,
                    decision_quality_score  DOUBLE PRECISION,
                    llm_performance_score   DOUBLE PRECISION,
                    overall_score           DOUBLE PRECISION,
                
                    -- Detailed findings (JSON blobs)
                    data_gaps               TEXT,       -- JSON: [{ticker, missing_sources, recommendation}]
                    decision_issues         TEXT,       -- JSON: [{ticker, action, issue, suggestion}]
                    llm_issues              TEXT,       -- JSON: [{model, agent, issue, count}]
                    performance_metrics     TEXT,       -- JSON: {total_ms, cache_hit_pct, ...}
                
                    -- LLM Reflection output
                    reflection              TEXT,       -- JSON: {summary, recommendations[], adjustments[]}
                
                    -- Recovery/resilience data absorbed here
                    recovery_stats          TEXT,       -- JSON: {total_failures, by_type, circuit_breakers}
                
                    -- Status
                    status                  TEXT DEFAULT 'running',  -- running | done | error
                    phase                   TEXT DEFAULT '',
                    error                   TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS hallucination_audit (
                    id                  TEXT PRIMARY KEY,
                    cycle_id            TEXT,
                    ticker              TEXT,
                    source_file         TEXT,
                    foreign_value       TEXT,
                    context_snippet     TEXT,
                    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_evolution_fixes (
                    id                      TEXT PRIMARY KEY,
                    cycle_id                TEXT NOT NULL,
                    target_type             TEXT NOT NULL,    -- 'prompt', 'scraper', 'strategy'
                    target_name             TEXT NOT NULL,    -- e.g. 'debate_prompts.py', 'reddit_scraper'
                    proposed_fix            TEXT NOT NULL,    -- JSON or raw text of the fix
                    motivation              TEXT NOT NULL,
                    proposer_model          TEXT,
                    critic_concerns         TEXT,
                    judge_score             DOUBLE PRECISION,
                    status                  TEXT DEFAULT 'pending', -- pending | approved | rejected | deployed | rolled_back
                    backup_path             TEXT,             -- path to pre-deploy backup file
                    probation_until         TIMESTAMP,        -- auto-rollback monitoring window
                    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at             TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stable_harnesses (
                    target_type     TEXT NOT NULL,
                    target_name     TEXT NOT NULL,
                    fix_id          TEXT NOT NULL,         -- FK to pending_evolution_fixes.id
                    stable_content  TEXT NOT NULL,         -- snapshot of the known-good file content
                    marked_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (target_type, target_name)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lens_scorecard (
                    id                      TEXT PRIMARY KEY,
                    lens_name               TEXT NOT NULL,
                    lens_type               TEXT NOT NULL,
                    system_prompt           TEXT,
                    cycle_id                TEXT NOT NULL,
                    ticker                  TEXT NOT NULL,
                    predicted_action        TEXT,
                    predicted_confidence    INTEGER,
                    actual_action           TEXT,
                    actual_price_change_pct DOUBLE PRECISION,
                    accuracy_score          DOUBLE PRECISION,
                    action_correct          BOOLEAN,
                    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    graded_at               TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_loop_stats (
                    id                      TEXT PRIMARY KEY,
                    cycle_id                TEXT,
                    agent_name              TEXT,
                    ticker                  TEXT,
                    loops_used              INTEGER,
                    token_usage             INTEGER,
                    cost_usd                DOUBLE PRECISION,
                    yielded                 BOOLEAN,
                    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tool_usage_stats (
                    id              SERIAL PRIMARY KEY,
                    tool_name       TEXT NOT NULL,
                    agent_name      TEXT DEFAULT '',
                    ticker          TEXT DEFAULT '',
                    cycle_id        TEXT DEFAULT '',
                    success         BOOLEAN DEFAULT TRUE,
                    execution_ms    INTEGER DEFAULT 0,
                    error_message   TEXT,
                    service_source  TEXT DEFAULT 'trading-service',
                    called_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_audit_log (
                    id          TEXT PRIMARY KEY,
                    cycle_id    TEXT NOT NULL,
                    timestamp   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    audit_type  TEXT NOT NULL,       -- phase_entry | phase_exit | ticker_result | llm_response | anomaly
                    phase       TEXT DEFAULT '',
                    ticker      TEXT DEFAULT '',
                    severity    TEXT DEFAULT 'info', -- info | warning | critical
                    message     TEXT,
                    data        JSONB DEFAULT '{}'
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS debate_history (
                    id                  TEXT PRIMARY KEY,
                    cycle_id            TEXT NOT NULL,
                    ticker              TEXT NOT NULL,
                    thesis_action       TEXT,           -- BUY/SELL/HOLD (Config C)
                    thesis_confidence   INTEGER,
                    counter_action      TEXT,           -- Devil's advocate position
                    counter_confidence  INTEGER,
                    winner              TEXT,           -- 'thesis' or 'antithesis'
                    final_action        TEXT,           -- Synthesis result
                    final_confidence    INTEGER,
                    persona_name        TEXT,
                    key_risk            TEXT,
                    pro_argument        TEXT,
                    con_argument        TEXT,
                    persona_outcomes    JSONB,
                    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_summaries (
                    ticker             TEXT,
                    cycle_id           TEXT,
                    cycle_date         TIMESTAMP,
                    agent_name         TEXT,
                    action             TEXT,
                    confidence         INTEGER,
                    confidence_tier    TEXT,
                    rationale_summary  TEXT,
                    was_correct        BOOLEAN,
                    outcome_pnl        DOUBLE PRECISION,
                    created_at         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (ticker, cycle_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS best_per_ticker (
                    ticker          TEXT PRIMARY KEY,
                    action          TEXT,
                    confidence      INTEGER,
                    rationale       TEXT,
                    is_correct      BOOLEAN,
                    score           DOUBLE PRECISION,
                    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_traces (
                    id                  TEXT PRIMARY KEY,
                    run_id              TEXT,
                    agent_name          TEXT,
                    task_type           TEXT,
                    goal                TEXT,
                    planned_next_action TEXT,
                    tool_name           TEXT,
                    tool_args           TEXT,
                    tool_result_summary TEXT,
                    why_tool_was_called TEXT,
                    tokens_before       INTEGER,
                    tokens_after        INTEGER,
                    latency_ms          INTEGER,
                    did_tool_change_decision BOOLEAN,
                    loop_step           INTEGER,
                    stop_reason         TEXT,
                    endpoint_name       TEXT,
                    model_name          TEXT,
                    service_source      TEXT DEFAULT 'trading-service',
                    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS eval_scores (
                    id                  TEXT PRIMARY KEY,
                    run_id              TEXT,
                    completion_score    DOUBLE PRECISION,
                    tool_correctness_score DOUBLE PRECISION,
                    efficiency_score    DOUBLE PRECISION,
                    error_recovery_score DOUBLE PRECISION,
                    stop_quality_score  DOUBLE PRECISION,
                    final_score         DOUBLE PRECISION,
                    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS failure_buckets (
                    id                  TEXT PRIMARY KEY,
                    run_id              TEXT,
                    bucket_type         TEXT, -- skipped_needed_tool, wrong_tool_selected, etc.
                    description         TEXT,
                    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tool_playbook (
                    id                  TEXT PRIMARY KEY,
                    task_type           TEXT,
                    market_context      TEXT,
                    agent_role          TEXT,
                    recommended_tool_sequence TEXT,
                    required_preconditions TEXT,
                    stop_conditions     TEXT,
                    bad_patterns_to_avoid TEXT,
                    example_good_trace_id TEXT,
                    score_stats         TEXT,
                    last_validated_at   TIMESTAMPTZ,
                    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_consensus (
                    ticker          TEXT PRIMARY KEY,
                    consensus       TEXT,
                    last_updated    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS janitor_run_log (
                    id SERIAL PRIMARY KEY,
                    run_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    details TEXT
                );
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    _safe_add_column(conn, "watchlist", "health_score", "INTEGER DEFAULT 50")
    _safe_add_column(conn, "watchlist", "purged_at", "TIMESTAMP")
    _safe_add_column(conn, "watchlist", "purge_reason", "TEXT")

    # --- Auto-synced missing tables from schema_pg.sql ---
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS global")
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.energy_reports (
                    id              TEXT PRIMARY KEY,
                    series_id       TEXT,        -- EIA series: 'PET.WCESTUS1.W'
                    indicator       TEXT,        -- 'crude_inventory', 'gasoline_prod', etc.
                    date            DATE,
                    value           DOUBLE PRECISION,
                    unit            TEXT,        -- 'thousand_barrels', 'thousand_bpd'
                    source          TEXT DEFAULT 'eia',
                    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.conflict_events (
                    id              TEXT PRIMARY KEY,
                    event_id_acled  INTEGER,        -- Legacy column name, 0 for GDELT-sourced events
                    event_date      DATE,
                    year            INTEGER,
                    event_type      TEXT,        -- 'Battles', 'Explosions/Remote violence', 'Protests'
                    sub_event_type  TEXT,
                    actor1          TEXT,
                    actor2          TEXT,
                    country         TEXT,
                    region          TEXT,        -- 'Middle East', 'Eastern Europe', etc.
                    admin1          TEXT,        -- province/state
                    latitude        DOUBLE PRECISION,
                    longitude       DOUBLE PRECISION,
                    fatalities      INTEGER,
                    notes           TEXT,
                    source_acled    TEXT,        -- Legacy name, stores source domain for GDELT events
                    chokepoint_proximity TEXT,   -- 'hormuz', 'suez', 'bab_el_mandeb', 'none'
                    source          TEXT DEFAULT 'gdelt',  -- 'gdelt' (was 'acled')
                    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.trade_flows (
                    id              TEXT PRIMARY KEY,
                    reporter_code   INTEGER,
                    reporter        TEXT,        -- 'United States', 'China', etc.
                    partner_code    INTEGER,
                    partner         TEXT,
                    commodity_code  TEXT,        -- HS code: '2709' (crude oil)
                    commodity_desc  TEXT,
                    trade_flow      TEXT,        -- 'Import' or 'Export'
                    value_usd       DOUBLE PRECISION,
                    net_weight_kg   DOUBLE PRECISION,
                    period          TEXT,        -- '202603' (YYYYMM)
                    source          TEXT DEFAULT 'comtrade',
                    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.gpr_index (
                    date            DATE PRIMARY KEY,
                    gpr             DOUBLE PRECISION,         -- overall GPR index
                    gpr_threats     DOUBLE PRECISION,         -- threats sub-index (GPRT)
                    gpr_acts        DOUBLE PRECISION,         -- acts sub-index (GPRA)
                    source          TEXT DEFAULT 'policyuncertainty',
                    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.anomalies (
                    id              TEXT PRIMARY KEY,
                    data_source     TEXT,        -- 'eia', 'acled', 'comtrade', 'gpr'
                    indicator       TEXT,        -- 'crude_inventory_change', 'conflict_count_mideast'
                    date            DATE,
                    observed_value  DOUBLE PRECISION,
                    expected_value  DOUBLE PRECISION,         -- EWMA or rolling mean
                    z_score         DOUBLE PRECISION,
                    severity        TEXT,        -- 'NORMAL', 'ELEVATED', 'CRITICAL'
                    description     TEXT,        -- "Oil inventories drew -6.2M vs expected -1.5M"
                    affected_assets TEXT,        -- JSONB: ["XLE","OXY","CL=F","GLD"]
                    detected_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.cross_correlations (
                    id              TEXT PRIMARY KEY,
                    series_a        TEXT,        -- 'eia_crude_inventory'
                    series_b        TEXT,        -- 'acled_mideast_events'
                    correlation     DOUBLE PRECISION,         -- Pearson
                    granger_pvalue  DOUBLE PRECISION,         -- Granger causality p-value (A→B)
                    granger_reverse DOUBLE PRECISION,         -- Granger causality p-value (B→A)
                    lead_lag_days   INTEGER,        -- optimal lag (negative = A leads B)
                    method          TEXT,        -- 'pearson', 'granger', 'distance'
                    window_days     INTEGER,        -- rolling window size
                    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.regime_states (
                    date            DATE PRIMARY KEY,
                    regime_label    TEXT,        -- 'risk_off', 'risk_on', 'crisis', 'transition'
                    regime_prob     DOUBLE PRECISION,         -- HMM posterior probability
                    gpr_level       TEXT,        -- 'LOW', 'MODERATE', 'HIGH', 'EXTREME'
                    energy_state    TEXT,        -- 'oversupply', 'balanced', 'deficit'
                    trade_state     TEXT,        -- 'expanding', 'stable', 'contracting'
                    composite_score DOUBLE PRECISION,         -- 0-100 multi-signal composite
                    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.intelligence_briefs (
                    id              TEXT PRIMARY KEY,
                    brief_type      TEXT,        -- 'daily_macro', 'energy', 'geopolitical', 'trade', 'composite'
                    period_start    DATE,
                    period_end      DATE,
                    summary         TEXT,        -- LLM-generated intelligence brief
                    risk_level      TEXT,        -- 'NORMAL', 'ELEVATED', 'CRITICAL'
                    anomaly_count   INTEGER,
                    affected_assets TEXT,        -- JSONB: ["XLE","GLD","CL=F"]
                    raw_data_count  INTEGER,
                    tokens_used     INTEGER,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.war_news_feed (
                    id              TEXT PRIMARY KEY,
                    headline        TEXT,
                    url             TEXT,
                    source_domain   TEXT,
                    latitude        DOUBLE PRECISION,
                    longitude       DOUBLE PRECISION,
                    location_name   TEXT,
                    tone            DOUBLE PRECISION,         -- GDELT average tone (-100 to +100)
                    themes          TEXT,        -- JSONB array of GDELT themes
                    timestamp       TIMESTAMP,
                    data_source     TEXT DEFAULT 'gdelt',
                    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.tanker_positions (
                    id              TEXT PRIMARY KEY,
                    mmsi            TEXT,        -- Maritime Mobile Service Identity
                    vessel_name     TEXT,
                    vessel_type     TEXT,        -- 'Tanker', 'Cargo', etc.
                    latitude        DOUBLE PRECISION,
                    longitude       DOUBLE PRECISION,
                    speed           DOUBLE PRECISION,         -- knots
                    heading         DOUBLE PRECISION,         -- degrees
                    destination     TEXT,
                    flag            TEXT,        -- country flag
                    zone            TEXT,        -- 'hormuz', 'bab_el_mandeb', 'suez', 'malacca'
                    timestamp       TIMESTAMP,
                    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.chokepoint_alerts (
                    id              TEXT PRIMARY KEY,
                    zone            TEXT,        -- 'hormuz', 'suez', 'bab_el_mandeb', 'malacca'
                    alert_level     TEXT,        -- 'NORMAL', 'ELEVATED', 'CRITICAL'
                    tanker_count    INTEGER,
                    nearby_conflict_count INTEGER,
                    avg_tanker_speed DOUBLE PRECISION,        -- low speed = congestion
                    reroute_count   INTEGER,        -- tankers avoiding the zone
                    war_news_count  INTEGER DEFAULT 0,
                    price_impact_score DOUBLE PRECISION,      -- 0-1 estimated oil price impact
                    timestamp       TIMESTAMP,
                    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global.tracked_commodities (
                    hs_code         TEXT PRIMARY KEY,
                    name            TEXT,
                    category        TEXT,        -- 'energy', 'metals', 'agriculture', 'livestock', 'soft'
                    trading_symbol  TEXT,        -- futures ticker: 'CL=F', 'GC=F', etc.
                    is_active       BOOLEAN DEFAULT TRUE
                );
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Trade Results (V3 Decision Pipeline Layer 5) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_results (
                    id                  TEXT PRIMARY KEY,
                    ticker              TEXT NOT NULL,
                    cycle_id            TEXT NOT NULL,
                    action              TEXT NOT NULL,          -- BUY / SELL / HOLD
                    confidence          INTEGER DEFAULT 0,     -- 0-100
                    reasoning           TEXT,
                    signal_weights      JSONB,                 -- {"quant": 0.25, "fundamental": 0.25, ...}
                    signal_assessments  JSONB,                 -- per-signal assessment text
                    risk_flags          JSONB,                 -- ["flag1", "flag2"]
                    stop_loss           DOUBLE PRECISION,
                    take_profit         DOUBLE PRECISION,
                    position_size_pct   DOUBLE PRECISION,
                    persona_used        TEXT,
                    regime              TEXT,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_results_ticker
                ON trade_results(ticker);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_results_cycle
                ON trade_results(cycle_id);
            """)
            # 2026-07-21 audit: both fields existed only inside the artifact
            # JSON, making consensus-scaled sizing and trigger audits
            # unverifiable by SQL.
            cur.execute("""
                ALTER TABLE trade_results
                ADD COLUMN IF NOT EXISTS internal_consensus_score INTEGER;
            """)
            cur.execute("""
                ALTER TABLE trade_results
                ADD COLUMN IF NOT EXISTS dynamic_trigger JSONB;
            """)
            # 2026-07-23 audit: the ENFORCED policy label was never persisted,
            # so blocked SELL/BUYs were indistinguishable from executed ones.
            cur.execute("""
                ALTER TABLE trade_results
                ADD COLUMN IF NOT EXISTS policy_action TEXT;
            """)
            # 2026-07-25: decision_provenance was added to the DESK but not
            # here, so the table the UI, the replay API and the freshness gate
            # all read still could not tell a degraded fallback from a real
            # agent verdict — the same laundering this field exists to stop,
            # left half-fixed. See docs/DECISION_INTEGRITY_PLAN.md.
            cur.execute("""
                ALTER TABLE trade_results
                ADD COLUMN IF NOT EXISTS decision_provenance TEXT;
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Agent Tool Telemetry (Phase 3: per-tool-call metrics) ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_tool_telemetry (
                    id                  TEXT PRIMARY KEY,
                    cycle_id            TEXT NOT NULL,
                    agent_name          TEXT NOT NULL,
                    tool_name           TEXT NOT NULL,
                    args_hash           TEXT,
                    success             BOOLEAN NOT NULL DEFAULT TRUE,
                    elapsed_ms          INTEGER DEFAULT 0,
                    error_message       TEXT,
                    was_blocked         BOOLEAN NOT NULL DEFAULT FALSE,
                    ticker              TEXT,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                ALTER TABLE agent_tool_telemetry ADD COLUMN IF NOT EXISTS ticker TEXT;
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_tool_telemetry_cycle
                ON agent_tool_telemetry(cycle_id);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_tool_telemetry_agent
                ON agent_tool_telemetry(agent_name);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_tool_telemetry_tool
                ON agent_tool_telemetry(tool_name);
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Freshness Gate: analysis snapshot columns ──
    _safe_add_column(conn, "analysis_results", "analysis_price", "FLOAT")
    _safe_add_column(conn, "analysis_results", "analysis_rsi", "FLOAT")
    _safe_add_column(conn, "analysis_results", "analysis_fund_count", "INTEGER DEFAULT 0")

    # ── Freshness Gate: tunable threshold config ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS freshness_gate_config (
                    id SERIAL PRIMARY KEY,
                    threshold_name VARCHAR(64) UNIQUE NOT NULL,
                    threshold_value FLOAT NOT NULL,
                    weight FLOAT NOT NULL DEFAULT 1.0,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    updated_by VARCHAR(64) DEFAULT 'system',
                    rationale TEXT
                );
            """)
            # Seed defaults (idempotent)
            cur.execute("""
                INSERT INTO freshness_gate_config (threshold_name, threshold_value, weight, rationale) VALUES
                    ('price_delta_max_pct', 5.0, 0.30, '~2σ daily move for large-cap stocks'),
                    ('news_count_max', 3.0, 0.25, 'Median ticker gets 1-2 articles/day'),
                    ('volume_ratio_max', 2.0, 0.20, 'Standard institutional activity threshold'),
                    ('rsi_boundary_weight', 1.0, 0.15, 'Binary: RSI crossed 30 or 70'),
                    ('fund_delta_max', 3.0, 0.10, 'Meaningful shift in institutional positioning'),
                    ('composite_threshold', 0.40, 1.0, 'Minimum delta_score to classify as CHANGED')
                ON CONFLICT (threshold_name) DO NOTHING;
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Collector tables present in schema_pg.sql but missing here ──
    # (put_call_ratio, sec_13f_performance, congress_members) — keeps
    # migrations.py complete for databases created before schema_pg.sql ran
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS put_call_ratio (
                    symbol      TEXT DEFAULT 'SPY',
                    date        DATE,
                    pcr_volume  DOUBLE PRECISION,
                    pcr_oi      DOUBLE PRECISION,
                    total_put_vol  BIGINT,
                    total_call_vol BIGINT,
                    total_put_oi   BIGINT,
                    total_call_oi  BIGINT,
                    PRIMARY KEY (symbol, date)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sec_13f_performance (
                    cik            TEXT PRIMARY KEY,
                    return_1y      DOUBLE PRECISION,
                    return_3y_ann  DOUBLE PRECISION,
                    win_rate       DOUBLE PRECISION,
                    last_calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Smart-money real-alpha tables. The returns engine also ensures
            # these at run time, but declaring them here means a fresh DB has
            # them before anything queries them.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS smart_money_trade_scores (
                    trade_key       TEXT PRIMARY KEY,
                    actor_type      TEXT NOT NULL,
                    actor_id        TEXT NOT NULL,
                    actor_name      TEXT,
                    ticker          TEXT NOT NULL,
                    direction       TEXT NOT NULL,
                    event_date      DATE NOT NULL,
                    size_est_usd    DOUBLE PRECISION,
                    size_confidence TEXT,
                    entry_price     DOUBLE PRECISION,
                    ret_1m DOUBLE PRECISION, ret_3m DOUBLE PRECISION,
                    ret_6m DOUBLE PRECISION, ret_1y DOUBLE PRECISION,
                    alpha_1m DOUBLE PRECISION, alpha_3m DOUBLE PRECISION,
                    alpha_6m DOUBLE PRECISION, alpha_1y DOUBLE PRECISION,
                    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS smart_money_performance (
                    actor_type      TEXT NOT NULL,
                    actor_id        TEXT NOT NULL,
                    actor_name      TEXT,
                    horizon         TEXT NOT NULL,
                    trade_count     INTEGER,
                    scored_count    INTEGER,
                    coverage_pct    DOUBLE PRECISION,
                    avg_return      DOUBLE PRECISION,
                    avg_alpha       DOUBLE PRECISION,
                    median_alpha    DOUBLE PRECISION,
                    win_rate        DOUBLE PRECISION,
                    total_size_est  DOUBLE PRECISION,
                    rankable        BOOLEAN DEFAULT FALSE,
                    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (actor_type, actor_id, horizon)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_backfill_progress (
                    ticker       TEXT PRIMARY KEY,
                    status       TEXT NOT NULL,
                    rows_written INTEGER DEFAULT 0,
                    error        TEXT,
                    attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS congress_members (
                    bioguide_id VARCHAR PRIMARY KEY,
                    first_name VARCHAR,
                    last_name VARCHAR,
                    full_name VARCHAR,
                    party VARCHAR,
                    chamber VARCHAR,
                    state VARCHAR,
                    collected_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Watch Desk: agent-defined watch conditions + fire log ────────────────
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_watches (
                    id                TEXT PRIMARY KEY,
                    ticker            TEXT NOT NULL,
                    bot_id            TEXT,
                    triggers          TEXT NOT NULL,
                    reason            TEXT,
                    thesis_summary    TEXT,
                    is_active         BOOLEAN DEFAULT TRUE,
                    cooldown_minutes  INTEGER DEFAULT 240,
                    fire_count        INTEGER DEFAULT 0,
                    last_fired_at     TIMESTAMPTZ,
                    last_evaluated_at TIMESTAMPTZ,
                    source_cycle_id   TEXT,
                    expiry_at         TIMESTAMPTZ,
                    created_at        TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    updated_at        TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticker_watches_active ON ticker_watches (is_active, ticker);"
            )
            # Migrate the legacy `sentinel_events` fire log to `watch_events`,
            # preserving live rows. Robust to schema_pg.sql having already created
            # an (empty) watch_events: rename when the target is absent, else copy
            # rows across (id-safe) and drop the orphan. Idempotent — a no-op once
            # sentinel_events is gone.
            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'sentinel_events') THEN
                        IF NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'watch_events') THEN
                            ALTER TABLE sentinel_events RENAME TO watch_events;
                            ALTER INDEX IF EXISTS idx_sentinel_events_ticker RENAME TO idx_watch_events_ticker;
                        ELSE
                            INSERT INTO watch_events SELECT * FROM sentinel_events
                                ON CONFLICT (id) DO NOTHING;
                            DROP TABLE sentinel_events;
                        END IF;
                    END IF;
                END $$;
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watch_events (
                    id           TEXT PRIMARY KEY,
                    watch_id     TEXT,
                    ticker       TEXT NOT NULL,
                    trigger_type TEXT,
                    detail       TEXT,
                    trigger_json TEXT,
                    value        DOUBLE PRECISION,
                    fired_at     TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    cycle_id     TEXT,
                    consumed_at  TIMESTAMPTZ
                );
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_watch_events_ticker ON watch_events (ticker, fired_at DESC);"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── SkillOpt: per-agent learned skill docs (agent_skills) + audit log of
    # edits that failed the validation gate (rejected_skill_edits). Written by
    # app/autoresearch/skill_optimizer.py, read by skill_loader.py.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_skills (
                    id          BIGSERIAL PRIMARY KEY,
                    agent_name  TEXT NOT NULL,
                    version     INTEGER NOT NULL DEFAULT 1,
                    skill_text  TEXT NOT NULL,
                    skill_hash  TEXT,
                    cycle_id    TEXT,
                    score       DOUBLE PRECISION,
                    action      TEXT,
                    rationale   TEXT,
                    status      TEXT NOT NULL DEFAULT 'active',
                    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_skills_active
                ON agent_skills (agent_name, status, version DESC);
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rejected_skill_edits (
                    id          BIGSERIAL PRIMARY KEY,
                    agent_name  TEXT NOT NULL,
                    skill_hash  TEXT,
                    cycle_id    TEXT,
                    reason      TEXT,
                    score_delta DOUBLE PRECISION,
                    rationale   TEXT,
                    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── shkreli_opinions (2026-07-27) ──
    # Per-company opinion cards distilled from YouTube analysis transcripts by
    # scripts/mine_shkreli_doctrine.py --opinions, read at desk build by
    # app/v3/opinion_block.py.
    #
    # Deliberately NOT stored in `youtube_transcripts`: that table feeds
    # per-ticker retrieval in web_search.py and mention-trend counts in
    # pipeline_service.py, and hundreds of commentary transcripts would show up
    # in both as if they were news.
    #
    # `recorded_on` is NOT NULL by intent. An opinion whose date is unknown
    # cannot be safely injected — the whole risk of this feature is a 2024 view
    # on a stock being read as a 2026 one, and a NULL date would render as a
    # confident undated claim. No date, no row.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shkreli_opinions (
                    id              BIGSERIAL PRIMARY KEY,
                    ticker          TEXT NOT NULL,
                    video_id        TEXT NOT NULL,
                    recorded_on     DATE NOT NULL,
                    company_name    TEXT,
                    stance          TEXT,
                    thesis          TEXT,
                    valuation_view  TEXT,
                    likes           TEXT,
                    dislikes        TEXT,
                    price_context   TEXT,
                    source_title    TEXT,
                    confidence      INTEGER,
                    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (ticker, video_id)
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_shkreli_opinions_ticker
                ON shkreli_opinions (ticker, recorded_on DESC);
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── cycle_schedules.job_type (2026-07-25) ──
    # Distinguishes agent-created schedules from system jobs. Nothing writes
    # 'system' today: the ~26 APScheduler jobs (market_open_cycle, stop-loss
    # monitor, collectors, ...) are surfaced read-only via
    # /api/diagnostics/system-jobs rather than mirrored into this table,
    # because research_governor counts active rows against
    # ScheduleValidator.MAX_SYSTEM_SCHEDULES (10) and 26 extra rows would
    # permanently reject new agent schedules. The column exists so that count
    # can exclude them, and so the distinction is expressible if it is ever
    # needed.
    _safe_add_column(conn, "cycle_schedules", "job_type", "TEXT DEFAULT 'user'")

    # ── V3 guardrail firings (2026-07-25) ──
    # The table only ever came into existence lazily, on the first firing, via
    # telemetry._ensure_guardrail_table(). That made an empty result ambiguous
    # between "no guardrail fired" and "the table did not exist yet" — the very
    # ambiguity the 2026-07-25 audit walked into when it concluded from an empty
    # grep that coerce_unshortable_sell had never run (it had; the backstop was
    # load-bearing). A counter that might simply be absent cannot be used to
    # argue a guardrail is unnecessary, so the table now exists from deploy.
    #
    # The DDL below is deliberately byte-identical to _ensure_guardrail_table()'s
    # (same column types, same index name). The lazy CREATE stays as
    # belt-and-braces for any path reaching telemetry before migrations run;
    # if these two ever diverge, whichever runs first silently wins.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS v3_guardrail_firings (
                    id SERIAL PRIMARY KEY,
                    guardrail TEXT NOT NULL,
                    cycle_id TEXT,
                    ticker TEXT,
                    detail JSONB,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_v3_guardrail_name
                ON v3_guardrail_firings (guardrail, created_at)
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── trade_fills.decision_price (2026-07-26) ──
    #
    # The reference price the decision was made at, before execution costs. With
    # `fill_price` (post-cost) alongside it, realized implementation shortfall
    # (Perold 1988) becomes measurable directly from the ledger:
    #
    #     IS = (fill_price - decision_price) / decision_price, signed by side
    #
    # Until 2026-07-26 the two were identical by construction — paper_trader
    # filled at exactly the reference price with fees=0 — so every performance
    # number was gross of all friction. NULL on historical rows means "filled
    # frictionlessly", which is the truth about them, and is deliberately not
    # backfilled with a modeled estimate.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE trade_fills "
                "ADD COLUMN IF NOT EXISTS decision_price DOUBLE PRECISION"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── decision_outcomes.skill_versions (2026-07-25) ──
    #
    # Which skill doc governed the decision this row scores.
    #
    # Without this the SkillOpt loop is UNFALSIFIABLE, and measurably so: as of
    # 2026-07-25 `agent_skills` carried 145 versions and `decision_outcomes`
    # 2028 resolved rows, and the number of rows joining the two was ZERO. The
    # `cycle_id` on agent_skills is the cycle that PRODUCED an edit, not the
    # cycles that edit later governed, so it cannot answer "did version 20 trade
    # better than version 19" — the only question that justifies the loop's cost.
    #
    # JSONB of {agent_name: version} rather than one column per agent: the
    # target-agent roster changes, and a decision is governed by the whole
    # fleet's docs at once.
    #
    # NULL means "we do not know" — rows written before this column existed are
    # deliberately not backfilled to some assumed version. The 2026-07-25 audit's
    # standing rule: missing must never be defaulted into a confident value.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE decision_outcomes "
                "ADD COLUMN IF NOT EXISTS skill_versions JSONB"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── decision_outcomes.overridden_from (2026-07-28) ──
    #
    # The action the BOARD authorised, when the decision synthesizer changed it.
    #
    # Execution reads `trade_decision or final_decision`, so the synthesizer has
    # the final say — and it exercises it: over 7 days it downgraded 21 of 41
    # Board BUYs to HOLD, a 51% veto rate. That veto is the single largest
    # filter on trade flow in the pipeline and NOTHING measured it, because the
    # outcome row records only the surviving action. A HOLD the whole desk
    # agreed on and a HOLD that overruled a Board BUY are the same row.
    #
    # With this column the question becomes a query: group resolved HOLDs by
    # overridden_from and compare the counterfactual long-side move against the
    # BUYs that survived. That is the only way to learn whether the veto adds
    # or destroys alpha — the confidence floor of 70 has that evidence
    # (conf <70: n=130, mean -1.91%; >=70: n=698, mean +3.76%), the veto has
    # none.
    #
    # NULL means "not overridden", which is the common case and the honest
    # default: rows written before this column existed are not backfilled to a
    # guess.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE decision_outcomes "
                "ADD COLUMN IF NOT EXISTS overridden_from TEXT"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── decision_outcomes.models_used (2026-08-03) ──
    #
    # Which LLM model produced each agent's contribution to the decision this
    # row scores. JSONB of {agent_name: model_id}, same shape rationale as
    # skill_versions above: a decision is made by a fleet of agents that can be
    # served by different models (Jetson vs Gold Spark routing), and the roster
    # changes. Values come from v3_agent_telemetry.model_used — prism's
    # server-side resolved model, not the requested one — captured per desk at
    # record time.
    #
    # This is the join that makes a per-model P&L leaderboard possible at all:
    # before it, no table on the outcome path knew the model (llm_audit_logs
    # said "v3_pipeline" on every row).
    #
    # NULL means "we do not know" — pre-attribution rows are not backfilled.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE decision_outcomes "
                "ADD COLUMN IF NOT EXISTS models_used JSONB"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _create_decision_scores(conn):
    """The deterministic baseline score, recorded per (cycle, ticker).

    A SEPARATE table rather than columns on `trade_results`, for two reasons.

    A score exists for every ticker the desk opened, including the ones that
    died before producing a verdict — and those are the rows most worth having,
    because "the baseline said STRONG_BUY and the desk never finished" is a
    finding that a column on the decision row structurally cannot hold.

    And the point of the table is the COMPARISON. `board_confidence` and
    `board_action` are copied in at write time so the deterministic read and
    the agent's read sit on one row and can be joined against
    `decision_outcomes` without reconstructing either. Reconstructing the
    baseline later is not possible in any case: `fundamentals` is not a
    point-in-time panel, so a score recomputed next month is not the score the
    desk was shown.

    NULL board_action means no decision was reached, which is distinct from a
    HOLD and must stay that way.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS decision_scores (
                    id                TEXT PRIMARY KEY,
                    cycle_id          TEXT NOT NULL,
                    ticker            TEXT NOT NULL,
                    score             REAL,
                    band              TEXT NOT NULL,
                    baseline_confidence INTEGER,
                    coverage_pct      REAL,
                    percentile        REAL,
                    fundamental_score REAL,
                    technical_score   REAL,
                    hybrid_score      REAL,
                    risk_reward       REAL,
                    rr_target_source  TEXT,
                    rr_target_horizon TEXT,
                    gates_failed      TEXT[],
                    gates_unknown     TEXT[],
                    detail            JSONB,
                    board_action      TEXT,
                    board_confidence  INTEGER,
                    created_at        TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (cycle_id, ticker)
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_decision_scores_ticker "
                "ON decision_scores (ticker, created_at DESC)"
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _create_persistent_research_tables(conn):
    """
    Creates tables for cross-cycle persistent research memory (dossiers)
    and explicit research queues (lead, deep dive, monitor, exit review).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_dossiers (
                    ticker                 TEXT PRIMARY KEY,
                    lifecycle_state        TEXT NOT NULL DEFAULT 'NEW',
                    canonical_thesis       JSONB DEFAULT '{}',
                    lead_analyst_id        TEXT,
                    open_questions         JSONB DEFAULT '[]',
                    monitoring_triggers    JSONB DEFAULT '{}',
                    hold_spec              JSONB DEFAULT '{}',
                    decision_history       JSONB DEFAULT '[]',
                    attached_artifact_ids  JSONB DEFAULT '[]',
                    created_at             TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    updated_at             TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS v3_research_queues (
                    id             TEXT PRIMARY KEY,
                    ticker         TEXT NOT NULL,
                    queue_type     TEXT NOT NULL,
                    priority       INTEGER DEFAULT 0,
                    reason         TEXT,
                    source_agent   TEXT,
                    status         TEXT DEFAULT 'pending',
                    payload        JSONB DEFAULT '{}',
                    created_at     TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    updated_at     TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_queues_type_status "
                "ON v3_research_queues (queue_type, status, priority DESC, created_at ASC)"
            )
            # The orphan path (2026-08-08, open item 14). A claim that is
            # reclaimed and re-claimed forever is the stall it replaced wearing
            # a different status, so the count is what makes a poison item
            # terminal instead of invisible. Added separately from the CREATE
            # because the table already exists in production.
            cur.execute(
                "ALTER TABLE v3_research_queues "
                "ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
            )
            # `reclaim_stale` scans processing rows by age on every pop.
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_queues_stale "
                "ON v3_research_queues (status, updated_at)"
            )
            # `model_shadow_runs.bot_id` (open item 1e). `model_shadow.py`'s own
            # `_ensure_shadow_table` also adds this, but that runs lazily on the
            # FIRST `_record()` call — so after the 2026-08-08 deploy the column
            # did not exist until a shadow happened to fire, and a schema check
            # taken straight after the deploy read MISSING. That is the wrong
            # answer to "did the migration land", and this file is where that
            # question is supposed to be answerable. Both paths are idempotent;
            # this one runs at boot.
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE model_shadow_runs ADD COLUMN IF NOT EXISTS bot_id TEXT;
                    UPDATE model_shadow_runs SET bot_id = 'unknown' WHERE bot_id IS NULL;
                EXCEPTION WHEN others THEN NULL;
                END $$;
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticker_dossiers_state "
                "ON ticker_dossiers (lifecycle_state)"
            )
            # The question ledger. `ticker_dossiers.open_questions` is a JSONB
            # list of strings — it holds the CURRENT set an agent should read,
            # and it cannot answer "did research resolve anything", because a
            # question that disappears from the list leaves no trace.
            #
            # One row per (ticker, question), stamped at ask time. This is the
            # same shape that made SkillOpt measurable: record the governing
            # fact when it governs, not by reconstructing it later.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dossier_question_log (
                    id              BIGSERIAL PRIMARY KEY,
                    ticker          TEXT NOT NULL,
                    question_hash   TEXT NOT NULL,
                    question        TEXT NOT NULL,
                    source_agent    TEXT,
                    first_cycle_id  TEXT,
                    first_asked_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    last_cycle_id   TEXT,
                    last_asked_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    ask_count       INTEGER DEFAULT 1,
                    status          TEXT NOT NULL DEFAULT 'open',
                    resolved_at     TIMESTAMPTZ,
                    resolved_cycle  TEXT,
                    evidence_ref    TEXT,
                    queue_item_id   TEXT,
                    UNIQUE (ticker, question_hash)
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_dossier_questions_status "
                "ON dossier_question_log (status, last_asked_at DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_dossier_questions_ticker "
                "ON dossier_question_log (ticker, status)"
            )
            conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass


    # ── Backfill: direction-aware HOLD grading (2026-08-08) ─────────────────
    #
    # `outcome_tracker._classify` used to grade a HOLD on |pnl| alone, which is
    # direction-blind. This book is long-only — the desk can buy, so the only
    # thing a HOLD forgoes is an UPSIDE move — and a name held through a
    # DECLINE was held correctly. Of the 154 graded HOLD_MISS rows on record,
    # 69 had fallen.
    #
    # This relabels those rows to HOLD_AVOIDED_DECLINE. It INVENTS NOTHING:
    # `pnl_pct` is already stored on every row, so the new label is derived
    # from the same number the original grade was derived from.
    #
    # It ships WITH the _classify change on purpose. Without it, HOLD_MISS
    # would mean "any 1% move" on rows written before the deploy and "a 1% RISE"
    # on rows written after, with nothing in the row to distinguish them —
    # every hold-accuracy consumer would then be averaging two different
    # metrics and could not tell.
    #
    # The threshold is a LITERAL rather than an import of WIN_THRESHOLD_PCT:
    # these rows were graded under a 1.0% band, and if that constant is ever
    # retuned, re-deriving history under the NEW band would rewrite grades
    # that were correct when they were made. Historical repair is frozen.
    #
    # Idempotent: after the first run no HOLD_MISS row has pnl_pct <= -1.0,
    # so every subsequent boot matches zero rows.
    #
    # NOTE: on the production database this was already applied by hand on
    # 2026-08-08 (a parallel session ran the same UPDATE), so this statement
    # will match 0 rows there and that is EXPECTED — not evidence it failed.
    # It stays because an ad-hoc UPDATE is not in the codebase: without this,
    # a fresh environment or a restored backup would carry the old labels
    # while the code assumed the new ones.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE decision_outcomes
                   SET outcome = 'HOLD_AVOIDED_DECLINE'
                 WHERE action = 'HOLD'
                   AND outcome = 'HOLD_MISS'
                   AND pnl_pct IS NOT NULL
                   AND pnl_pct <= -1.0
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Backfill: resolve the working-memory episodes (2026-08-12) ──────────
    #
    # `episodic_memory` rows are written once, at decision time, with
    # `outcome='pending'` (orchestrator) or the `write_episode` default of
    # `'neutral'`, and NOTHING ever revised them — 572 pending and 3,080
    # neutral rows against 2,394 resolved `decision_outcomes` on 2026-08-12.
    # `EpisodicMemoryStore.retrieve` claims to rank "by most successful
    # outcomes" over a column where every row tied at 0, and the "Relevant
    # Past Cycles" block in every agent prompt showed `Outcome Score: 0.0`
    # forever.
    #
    # `outcome_tracker.write_outcome_to_memory` closes this going forward.
    # This is the other half: 1,843 episodes on record already join a RESOLVED
    # decision on (cycle_id, ticker). Without it the tier stays blank for the
    # months of history the agents actually read back.
    #
    # It INVENTS NOTHING — `outcome` and `pnl_pct` are already stored on the
    # decision row; only the join is new.
    #
    # Three deliberate constraints:
    #
    #  - DISTINCT ON, because 239 (cycle_id, ticker) pairs carry duplicate
    #    decision rows (exact duplicates from before the dedup guard), and an
    #    unqualified UPDATE ... FROM picks among them non-deterministically.
    #    Newest resolution wins, deterministically.
    #  - DEGRADED_ARTIFACT / CANCELED excluded — the same exclusion
    #    decision_audit, confidence_calibration and power_report already apply
    #    to this column. 225 of the joinable rows are DEGRADED_ARTIFACT:
    #    pipeline crashes scored against a price, not outcomes.
    #  - The /10.0 clamp matches `EPISODE_SCORE_ANCHOR_PCT` in the forward
    #    path, so backfilled and live rows land on ONE scale. A literal rather
    #    than an import for the same reason the HOLD backfill above uses one:
    #    retuning the constant later must not silently rewrite history.
    #
    # Idempotent: after the first run no matched row is still pending/neutral.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE episodic_memory e
                   SET outcome = d.outcome,
                       outcome_score = GREATEST(-1.0, LEAST(1.0, d.pnl_pct / 10.0))
                  FROM (
                        SELECT DISTINCT ON (cycle_id, ticker)
                               cycle_id, ticker, outcome, pnl_pct
                          FROM decision_outcomes
                         WHERE resolved_at IS NOT NULL
                           AND pnl_pct IS NOT NULL
                           AND outcome IS NOT NULL
                           AND outcome NOT IN ('DEGRADED_ARTIFACT', 'CANCELED')
                         ORDER BY cycle_id, ticker, resolved_at DESC
                       ) d
                 WHERE e.cycle_id = d.cycle_id
                   AND e.ticker = d.ticker
                   AND (e.outcome IS NULL OR e.outcome IN ('pending', 'neutral'))
            """)
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ── Backfill: NULL cycle start times (2026-08-09) ───────────────────────
    #
    # `ORDER BY started_at DESC LIMIT 1` is the standard "newest cycle" query,
    # and Postgres sorts NULLs FIRST on DESC. 29 rows in cycle_benchmarks and 2
    # in cycle_run_summaries carried a NULL start, so every such query was
    # pinned to a 2026-05-27 row for eleven weeks while fresh rows sat below it
    # — `/run-cycle/audit/latest` fed that stale cycle into live agent chat
    # context. The call sites now carry NULLS LAST, but that only fixes the
    # queries that exist today; this closes the hole at the source.
    #
    # The start is reconstructed from the cycle's first pipeline_event, clamped
    # by LEAST(..., finished_at) because some old rows recorded events after
    # the summary was finalised and an unclamped backfill would invent a start
    # LATER than the finish. LEAST ignores NULLs, so a cycle with no events
    # falls back to finished_at on its own.
    for _tbl in ("cycle_benchmarks", "cycle_run_summaries"):
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE {_tbl} t
                       SET started_at = LEAST(pe.first_ts, t.finished_at)
                      FROM (SELECT cycle_id, MIN(timestamp) AS first_ts
                              FROM pipeline_events GROUP BY cycle_id) pe
                     WHERE pe.cycle_id = t.cycle_id
                       AND t.started_at IS NULL
                       AND t.finished_at IS NOT NULL
                """)
                # Cycles with no surviving events at all: the finish is the
                # only timestamp left, and it beats NULL for ordering.
                cur.execute(f"""
                    UPDATE {_tbl}
                       SET started_at = finished_at
                     WHERE started_at IS NULL AND finished_at IS NOT NULL
                """)
                # Only NOT NULL once nothing recoverable is left. If a row has
                # neither timestamp there is nothing to derive, so leave the
                # column nullable rather than fail the whole migration.
                cur.execute(f"SELECT count(*) FROM {_tbl} WHERE started_at IS NULL")
                if cur.fetchone()[0] == 0:
                    cur.execute(f"ALTER TABLE {_tbl} ALTER COLUMN started_at SET NOT NULL")
                conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
