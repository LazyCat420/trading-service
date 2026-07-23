"""
Central configuration for the vLLM Trading Bot.
All settings loaded from .env — no hardcoded values anywhere else.
"""
import json
import os
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings


ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env"

if not ENV_FILE.exists():
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists():
            ENV_FILE = parent / ".env"
            break
        elif (parent / "trading-client" / ".env").exists():
            ENV_FILE = parent / "trading-client" / ".env"
            break
        elif (parent / "trading-service" / ".env").exists():
            ENV_FILE = parent / "trading-service" / ".env"
            break


# --- projects.json dynamic loader ---
def _load_projects_json() -> dict:
    curr = Path(__file__).resolve()
    for parent in curr.parents:
        p1 = parent / "vault-service" / "projects.json"
        if p1.is_file():
            try:
                with open(p1, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        p2 = parent / "projects.json"
        if p2.is_file():
            try:
                with open(p2, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}

_projects_data = _load_projects_json()
_config = _projects_data.get("config", {})
_default_host = _projects_data.get("defaultHost", "10.0.0.16")


# Ensure data directories exist
DATA_DIR = ROOT_DIR / "data"
MEMORY_DB_PATH = DATA_DIR / "memory.db"


class Settings(BaseSettings):
    # ── Default Host ──
    DEFAULT_HOST: str = _default_host
    PROJECT_NAME: str = "vllm-trading-bot"

    # ── Prism VLLM Providers (Source of Truth from vault-service/projects.json) ──
    PROVIDER_VLLM_1_URL: str = _config.get("PROVIDER_VLLM_1_URL", "http://10.0.0.30:8000")
    PROVIDER_VLLM_1_NICKNAME: str = _config.get("PROVIDER_VLLM_1_NICKNAME", "Jetson")
    PROVIDER_VLLM_1_CONCURRENCY: int = int(_config.get("PROVIDER_VLLM_1_CONCURRENCY", "8") or "8")

    PROVIDER_VLLM_2_URL: str = _config.get("PROVIDER_VLLM_2_URL", "http://10.0.0.141:8000")
    PROVIDER_VLLM_2_NICKNAME: str = _config.get("PROVIDER_VLLM_2_NICKNAME", "Gold Spark")
    PROVIDER_VLLM_2_CONCURRENCY: int = Field(default=int(_config.get("PROVIDER_VLLM_2_CONCURRENCY", "16") or "16"), validation_alias="DGX_MAX_CONCURRENT")

    PROVIDER_VLLM_3_URL: str = _config.get("PROVIDER_VLLM_3_URL", "")
    PROVIDER_VLLM_3_NICKNAME: str = _config.get("PROVIDER_VLLM_3_NICKNAME", "")
    PROVIDER_VLLM_3_CONCURRENCY: int = int(_config.get("PROVIDER_VLLM_3_CONCURRENCY", "0") or "0")

    ACTIVE_MODEL: str = ""  # Auto-discovered from vLLM /v1/models at startup

    # ── Concurrency (tuned from saturation benchmarks — see tests/benchmarks/outputs/) ──
    RLM_MAX_CONCURRENT: int = (
        2  # max concurrent RLM sessions (uses own client, occupies slots)
    )

    # ── Batch Dispatch (prevents queue overload) ──
    # Items are drained from the queue in batches, not one-at-a-time.
    # Each batch completes before the next is dispatched.
    BATCH_TIMEOUT: int = 60           # 60s per batch (Jetson inference is 5-20s; prevents queue backup)
    BATCH_CIRCUIT_BREAKER_THRESHOLD: int = 5  # consecutive failed batches → disable endpoint 60s (raised from 3: burst patterns hit 3 too easily)

    # ── Adaptive Concurrency (caller-side LLM throttling) ──
    ADAPTIVE_MIN_CONCURRENCY: int = 8   # floor when KV cache pressure is high (>80%)
    ADAPTIVE_MAX_CONCURRENCY: int = 24   # ceiling when cache pressure is low (<60%)

    # ── Pipeline ──
    MAX_ANALYSIS_TICKERS: int = 30  # hard cap on tickers per cycle
    MAX_CYCLE_TICKERS: int = 0  # 0 = unlimited; 1-N caps total tickers for fast testing
    MIN_MARKET_CAP: float = 50_000_000  # $50M floor — reject OTC/penny
    CYCLE_TIMEOUT_MINUTES: int = 120  # 2-hour hard cap per cycle
    V2_TICKER_CONCURRENCY: int = (
        2  # parallel tickers — reduced from 8; each worker spawns 6-10 LLM calls,
        # so 8 workers = 48-80 concurrent requests → vLLM saturation → mass thesis timeouts.
        # 2 workers keeps total LLM load within adaptive concurrency ceiling.
    )
    VLLM_FUTURE_TIMEOUT: int = 900  # seconds before a hung LLM future is killed (aligned with batch timeout)
    ANALYSIS_WORKER_TIMEOUT_SECONDS: int = (
        900  # 15-min hard cap per ticker — if thesis already failed 2 retries (5.5min),
        # don't let the worker sit idle for another 25 min. Fail fast, move on.
    )
    POST_CYCLE_HOUSEKEEPING_TIMEOUT_SECONDS: int = 300
    BOT_ID: str = "lazy-trader-v4"
    COLLECTION_MAX_CONCURRENT: int = 5  # parallel per-ticker scrapers
    # Cap concurrent per-ticker ANALYSIS pipelines. Each ticker pipeline spawns
    # several agents that each borrow DB connections (whiteboard, telemetry,
    # desk/artifact saves); with the pool at max_size=50, running the whole
    # watchlist at once (35+) exhausts the pool so hard the STOP_CYCLE poller
    # can't get a connection and the loop deadlocks (2026-07-20). 6 keeps the
    # concurrent connection demand well under the pool while still parallelizing.
    MAX_CONCURRENT_TICKERS: int = int(_config.get("MAX_CONCURRENT_TICKERS", "6") or "6")

    # Pipeline modes:
    #   "scout"      — wait for all data, run macro scout in parallel, then analyze (recommended)
    #   "sequential" — wait for all data, then analyze (no macro scout)
    #   "overlap"    — start analysis as each ticker finishes collection (legacy)
    PIPELINE_MODE: str = "scout"
    PIPELINE_VERSION: str = "v2"  # "v1" | "v2" | "ab"
    PIPELINE_BENCHMARK_GROUP: str = "baseline"
    MACRO_SCOUT_ENABLED: bool = True  # enable/disable macro strategy scout

    # ── Decision Pipeline ──
    DECISION_AGENT_ENABLED: bool = True  # enable Layer 5 decision synthesis agent
    # KV-cache prompt split (2026-07-15): keep every V3 agent's system prompt
    # byte-identical across cycles (vLLM prefix-cache reuse) by moving all
    # cycle-specific context into the user message. False = legacy layout
    # (dynamic content appended to the system prompt) — the rollback path.
    V3_PROMPT_SPLIT: bool = True
    # Portfolio-level circuit breaker: refuse NEW BUYs once mark-to-market
    # portfolio value falls this far below its recorded peak (0.25 = 25%).
    # SELLs are never blocked. 0 disables the breaker.
    MAX_PORTFOLIO_DRAWDOWN_PCT: float = 0.25
    ANALYSIS_CONFIDENCE_THRESHOLD: int = 65  # minimum confidence (0-100) to execute trades
    MAX_POSITION_SIZE_PCT: float = 0.10  # hard cap on a single trade's cash fraction (agent sizing is clamped to this)

    # ── World Simulator ──
    EXECUTION_MODE: str = "production"  # "production" | "staging" | "simulation"
    SIMULATION_TREND: str = "bullish"  # "bullish" | "bearish" | "neutral" | "volatile"
    SIMULATION_NEWS_SENTIMENT: str = "positive"  # "positive" | "negative" | "neutral"

    # ── Queue & Utility ──
    PIPELINE_QUEUE_HIGH_WATERMARK: int = 200
    PIPELINE_QUEUE_LOW_WATERMARK: int = 100
    EMBEDDING_SERVER_URL: str = "http://localhost:8001/embed"
    REDIS_URL: str = "redis://localhost:6379"
    SCRAPER_SERVICE_URL: str = "http://scraper-service:8001"

    # ── Per-API Concurrency Limits ──
    # Caps concurrent requests to each external service when multiple
    # tickers collect data in parallel. Prevents IP bans and API rate limits.
    YFINANCE_MAX_CONCURRENT: int = 3  # yfinance HTTP (no auth, IP-based)
    FINNHUB_MAX_CONCURRENT: int = 5  # finnhub API (60 calls/min free tier)
    REDDIT_MAX_CONCURRENT: int = 2  # reddit public JSON (no auth, conservative)
    YOUTUBE_MAX_CONCURRENT: int = 2  # yt-dlp subprocess (CPU + network heavy)

    # ── LLM Curation (Pass 2.7) ──
    LLM_CURATION_ENABLED: bool = True  # toggle on/off
    LLM_CURATION_MAX_PROMOTE: int = 5  # max tickers promoted per cycle
    LLM_CURATION_FALLBACK: str = "pass_all"  # "pass_all" | "block_all" on failure

    # ── Watchlist Health & Auto-Purge ──
    WATCHLIST_PURGE_ENABLED: bool = True  # toggle on/off
    WATCHLIST_MAX_PURGE: int = 2  # max tickers purged per cycle
    WATCHLIST_PURGE_MIN_SCORE: int = 30  # only purge below this health score
    WATCHLIST_GRACE_CYCLES: int = 3  # new tickers get N cycles before scoring

    # ── Morning Briefing ──
    MORNING_BRIEFING_ENABLED: bool = True  # toggle on/off

    # ── Smart Ticker Triage ──
    TRIAGE_ENABLED: bool = True  # toggle triage on/off (flat list if disabled)
    TRIAGE_GLANCE_HOURS: int = 48  # analyzed within N hours → Glance tier
    TRIAGE_DEEP_HOURS: int = 72  # not analyzed in N hours → Deep tier
    TRIAGE_NEGLECT_MAX_DAYS: int = 5  # flag neglected after N days
    TRIAGE_MAX_CONSECUTIVE_GLANCE: int = 5  # force Standard after N Glance skips
    TRIAGE_DEEP_NEWS_VOLUME: int = 5  # >= N news articles in 24h → Deep tier

    # ── Alpha Decay Purge (Mathematical Pruning) ──
    ALPHA_DECAY_ENABLED: bool = True  # toggle fundamental math purge
    ALPHA_MAX_DEBT_TO_EQUITY: float = (
        50.0  # purge > 5000% debt (catches true rot, not leverage)
    )
    ALPHA_MIN_CURRENT_RATIO: float = (
        0.3  # purge if assets can't cover 30% short-term liabilities
    )
    ALPHA_MAX_52_WK_DRAWDOWN: float = 0.85  # purge if down 85% from 52-week high
    ALPHA_PENNY_FLOOR: float = (
        3.00  # actively purge if price crashes below $3 (deep OTC)
    )
    ALPHA_EXEMPT_DEBT_SECTORS: list[
        str
    ] = [  # these inherently run high debt; ignore D/E rules
        "Financial Services",
        "Real Estate",
        "Banks",
        "Utilities",
        "Energy",
    ]

    # ── Paper Trading ──
    STARTING_CASH: float = 100000.0

    # ── Janitor Agent (Data Hygiene) ──
    JANITOR_MAX_TOKENS: int = 4096
    AUDIT_LOG_TTL_DAYS: int = 14  # Delete llm_audit_logs older than this
    NEWS_DUPLICATE_TTL_DAYS: int = 30  # Delete duplicate news older than this
    NEWS_URL_FANOUT_CAP: int = 5  # Max per-ticker copies of one article URL (0 = unlimited)
    LESSON_CONSOLIDATION_THRESHOLD: int = 50  # Consolidate when lessons exceed this

    # ── Database ──
    DATABASE_URL: str = _config.get(
        "DATABASE_URL", "postgresql://localhost:5432/trading_bot"
    )
    TEST_DATABASE_URL: str = _config.get(
        "TEST_DATABASE_URL", f"postgresql://{_default_host}:5433/trading_bot_test"
    )

    # ── Finnhub ──
    FINNHUB_API_KEY: str = ""

    # ── FRED (Federal Reserve) ──
    FRED_API_KEY: str = ""

    # ── Financial Modeling Prep ──
    FMP_API_KEY: str = ""

    # ── EIA (Energy Information Administration) ──
    EIA_API_KEY: str = ""

    # ── News API Rotator Keys ──
    MARKETAUX_API_KEY: str = ""
    NEWSAPI_API_KEY: str = ""
    ALPHAVANTAGE_API_KEY: str = ""
    POLYGON_API_KEY: str = ""
    MASSIVE_API_KEY: str = ""  # Polygon rebranded to Massive — same API
    GNEWS_API_KEY: str = ""
    CURRENTS_API_KEY: str = ""
    THENEWSAPI_KEY: str = ""
    WORLDNEWSAPI_KEY: str = ""
    STOCKDATA_API_KEY: str = ""
    TWELVEDATA_API_KEY: str = ""

    # ── AISStream (real-time vessel tracking) ──
    AISSTREAM_API_KEY: str = ""

    # ── New Data Sources API Keys ──
    TIINGO_API_KEY: str = ""
    BLS_API_KEY: str = ""

    # ── War/Oil Intelligence Map ──
    GDELT_POLL_INTERVAL_MIN: int = 15
    AIS_POLL_INTERVAL_MIN: int = 5
    WAR_CONTEXT_ENABLED: bool = True

    # ── Prism AI Gateway (MongoDB mirror) ──
    # Default routes through lazy-tool-service's prism-proxy (external port
    # 5591), matching production; direct prism is :7777.
    PRISM_URL: str = _config.get("PRISM_URL", f"http://{_default_host}:5591/prism-proxy")
    PRISM_PROJECT: str = "vllm-trading-bot"
    PRISM_USERNAME: str = "lazy-trader"
    PRISM_ENABLED: bool = True
    PRISM_AGENT: str = "CUSTOM_MARKET_ALPHA"  # Routes through the CUSTOM_MARKET_ALPHA persona in Prism — custom agent with tailored trading tools
    # DEPRECATED: never read by any code path. The real prism bypass is
    # PRISM_ENABLED=false (lazycat-sdk calls vLLM directly). Kept only so a
    # stale PRISM_AGENT_ROUTING env var doesn't crash pydantic settings.
    PRISM_AGENT_ROUTING: bool = True
    PRISM_MONGO_URI: str = _config.get("PRISM_MONGO_URI", f"mongodb://{_default_host}:27017/?directConnection=true")
    PRISM_MONGO_DB: str = "prism"
    # Postgres → Mongo consolidation: trading document collections live in their
    # OWN Mongo DB (not prism's), on the same client/URI as PRISM_MONGO_URI.
    # See app/db/mongo_store.py and .agents/PLAN-mongodb-consolidation.md.
    TRADING_MONGO_DB: str = _config.get("TRADING_MONGO_DB", "trading_bot")
    PRISM_SKIP_CONVERSATION: bool = False
    PRISM_AUTO_APPROVE: bool = True
    PRISM_WORKSPACE_ENABLED: bool = False
    PRISM_MAX_ITERATIONS: int = 100
    PRISM_MAX_SUB_AGENT_ITERATIONS: int = 100
    PRISM_MAX_RECURSION_DEPTH: int = 2
    PRISM_THOUGHT_STRUCTURE: str = "chain_of_thought"

    # ── SEC 13F Tracking ──
    SEC_USER_AGENT: str = "vllm-trading-bot LazyCat420@users.noreply.github.com"
    SEC_13F_MAX_FILERS: int = 0  # 0 means scrape all

    # ── Prism Working Memory ──
    WORKING_MEMORY_MAX_SLOTS: int = 18

    # ── Tool Calling Bypass ──
    USE_TOOL_CALLING: bool = False

    API_SERVER_KEY: str = "change-me-local-dev"

    @model_validator(mode="after")
    def validate_api_key(self) -> "Settings":
        if self.API_SERVER_KEY == "change-me-local-dev":
            if self.EXECUTION_MODE == "production":
                raise ValueError(
                    "API_SERVER_KEY cannot be set to the default insecure value in production execution mode!"
                )
            else:
                import warnings
                warnings.warn(
                    "API_SERVER_KEY is set to the default insecure value!",
                    UserWarning,
                    stacklevel=2
                )
        return self



    # ── JIT Scraper Queue ──
    SCRAPER_MAX_QUEUE_SIZE: int = 1000
    SCRAPER_JIT_PRIORITY: int = 1  # highest priority (blocks analysis)
    SCRAPER_ROUTINE_PRIORITY: int = 5  # routine sweep priority
    SCRAPER_MAX_RETRIES: int = 3
    SCRAPER_WORKER_POLL_SECS: int = 5  # how often workers poll for new requests

    # ── Data Lifecycle ──
    RAW_DATA_TTL_HOURS: int = 72  # raw content kept for 72h
    ARCHIVE_TTL_DAYS: int = 30  # archived summaries kept for 30d
    MAX_ANALYSES_PER_RECORD: int = 5  # multi-angle re-analysis cap per record

    # ── Re-Analysis ──
    REANALYSIS_ENABLED: bool = False  # gate: disabled until Phase 3 verified
    REANALYSIS_SLOT_PCT: float = 0.60  # % of analysis slots for re-analysis
    FRESH_DATA_SLOT_PCT: float = 0.40  # % of slots for fresh data analysis

    # ── Strategy Ranking ──
    MIN_TRADES_BEFORE_BENCH: int = 10  # need N trades before benching
    WIN_RATE_BENCH_THRESHOLD: float = 0.40  # bench prompts below 40% win rate
    WIN_RATE_BONUS_THRESHOLD: float = 0.55  # bonus confidence for >55% win rate

    # ── Meta-Agent ──
    META_AGENT_ENABLED: bool = False  # gate: disabled until Phase 6 verified
    META_AGENT_INTERVAL_HOURS: int = 6  # how often the meta-agent runs
    MAX_ACTIVE_GENERATED_PROMPTS: int = 20  # cap on active generated lenses

    # ── P&L Evaluation Intervals ──
    TRADE_EVAL_INTERVALS_DAYS: list[int] = [1, 3, 7, 14]

    model_config = {
        "env_file": str(ENV_FILE),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
