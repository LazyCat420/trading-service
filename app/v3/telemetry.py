"""
V3 Telemetry — Per-agent metrics, phase outcomes, and pipeline summary.

Records telemetry to:
1. Standard Python logger (container logs)
2. Existing log_manager.log_v2_cycle() for cycle-level tracking
3. PostgreSQL v3_agent_telemetry table for dashboard queries
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.v3.shared_desk import SharedDesk

logger = logging.getLogger(__name__)

_TABLE_ENSURED = False


def _ensure_telemetry_table() -> None:
    """Create the v3_agent_telemetry table if it doesn't exist."""
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return

    from app.db.connection import get_db

    try:
        with get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS v3_agent_telemetry (
                    id SERIAL PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    elapsed_ms INTEGER DEFAULT 0,
                    loops_used INTEGER DEFAULT 0,
                    token_usage INTEGER DEFAULT 0,
                    artifact_size_bytes INTEGER DEFAULT 0,
                    quality_score INTEGER DEFAULT -1,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            # Add quality_score column to existing tables (idempotent)
            db.execute("""
                DO $$ BEGIN
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS quality_score INTEGER DEFAULT -1;
                EXCEPTION WHEN others THEN NULL;
                END $$;
            """)
            # KV-cache probe + prompt footprint (2026-08-03). cached_tokens /
            # prompt_tokens are the harness's LAST-request usage snapshot;
            # sys/user_prompt_chars were computed in agent_runner since the
            # context-budget work but never persisted.
            db.execute("""
                DO $$ BEGIN
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS cached_tokens INTEGER DEFAULT 0;
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER DEFAULT 0;
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS sys_prompt_chars INTEGER DEFAULT 0;
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS user_prompt_chars INTEGER DEFAULT 0;
                EXCEPTION WHEN others THEN NULL;
                END $$;
            """)
            # Model attribution (2026-08-03). model_used is prism's
            # server-side resolved model from the stream's done event — the
            # per-model leaderboard joins on it. NULL = pre-attribution row.
            db.execute("""
                DO $$ BEGIN
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS model_used TEXT;
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS provider TEXT;
                EXCEPTION WHEN others THEN NULL;
                END $$;
            """)
            # Failure diagnosis + attempt identity (2026-08-11). Until this
            # landed the table recorded THAT a run failed and nothing about
            # why: the crash path threw `str(e)` away entirely, so an
            # AGENT_ERROR row was indistinguishable from a timeout, a schema
            # rejection, or a model that returned nothing.
            #
            # attempt_no has NO DEFAULT, deliberately. The circuit breaker has
            # retried since long before this column existed, so every
            # historical row is of unknown attempt identity — defaulting them
            # to 1 would assert 79 rows were first attempts when one of them
            # (ASIC/v3_junior_analyst, cycle-v3-1786455000) demonstrably was
            # not. NULL means "written before attempts were recorded"; readers
            # must treat it as unknown, not as 1.
            db.execute("""
                DO $$ BEGIN
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS error_message TEXT;
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS failure_reason TEXT;
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS attempt_no INTEGER;
                EXCEPTION WHEN others THEN NULL;
                END $$;
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_v3_telemetry_model
                ON v3_agent_telemetry (model_used, created_at)
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_v3_telemetry_cycle
                ON v3_agent_telemetry (cycle_id)
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_v3_telemetry_agent
                ON v3_agent_telemetry (agent_name, created_at)
            """)
        _TABLE_ENSURED = True
        logger.debug("[V3Telemetry] Table v3_agent_telemetry ensured")
    except Exception as e:
        logger.warning("[V3Telemetry] Failed to ensure table: %s", e)


#: Marks a telemetry entry already written to `v3_agent_telemetry`. Lives INSIDE
#: the entry dict (like `_recorded_at`) so it round-trips through
#: `SharedDesk.to_dict`/`from_dict` — a desk reloaded from Postgres therefore
#: knows what has already been billed and cannot double-write it.
PERSISTED_FLAG = "_persisted"


def flush_agent_telemetry(desk: SharedDesk) -> int:
    """Write the agent cost rows not yet written. Returns how many.

    WHY THIS IS NOT JUST `persist_telemetry`
    ----------------------------------------
    `persist_telemetry` ran ONCE, at the very end of `run_v3_pipeline`
    (orchestrator.py). Every agent's cost accumulated in memory on
    `desk.agent_telemetry` until then — so a ticker that died before that line
    lost its ENTIRE cost record, despite having already spent the tokens.

    Measured since 2026-07-12 (when this telemetry began; before that the table
    is empty and any coverage number spanning the boundary is two populations):

        PM_DONE         429 desks    99.5% have cost rows
        ABORTED          40 desks     0.0%
        DEBATE_DONE       9 desks     0.0%
        RESEARCH_DONE    22 desks     4.5%

    71 desks with no cost record at a median 664,627 tokens per completed desk
    is **up to ~47M tokens, ~14.5% of true spend, invisible** — an upper bound,
    since a desk that dies mid-flight may have spent less than a full one. It
    also means any share-of-spend figure computed from this table (e.g. "the
    tournament is 31% of all tokens") was measured against a denominator missing
    the crashed tickers.

    Idempotent by construction: entries are marked as written, and the mark is
    part of the entry, so it survives the desk's own round-trip to Postgres.
    """
    entries = getattr(desk, "agent_telemetry", None) or []
    pending = [
        e for e in entries
        if isinstance(e, dict) and not e.get(PERSISTED_FLAG)
    ]
    if not pending:
        return 0

    try:
        _persist_entries(desk, pending)
    except Exception as e:  # noqa: BLE001 — cost accounting must not break a save
        logger.warning(
            "[V3Telemetry] flush failed for %s (%d entries stay pending): %s",
            getattr(desk, "ticker", "?"), len(pending), e,
        )
        return 0

    # Mark only after the write returns, so a failed write is retried by the
    # next save rather than being recorded as billed and lost.
    for e in pending:
        e[PERSISTED_FLAG] = True
    return len(pending)


def persist_telemetry(desk: SharedDesk) -> None:
    """Flush any remaining agent telemetry at the end of the pipeline.

    Kept as the end-of-pipeline call, but it is now a backstop rather than the
    only writer: `save_desk` flushes as the desk progresses, so a crash keeps
    what was already spent. Delegates to `flush_agent_telemetry`, which makes
    calling both harmless.
    """
    flush_agent_telemetry(desk)


def _persist_entries(desk: SharedDesk, entries: list[dict]) -> None:
    """Insert the given telemetry entries for this desk."""
    _ensure_telemetry_table()

    if not entries:
        return

    from app.db.connection import get_db

    try:
        _recs = [
            {
                "cycle_id": desk.cycle_id, "ticker": desk.ticker,
                "agent_name": entry.get("agent_name", "?"), "phase": entry.get("phase", "?"),
                "outcome": entry.get("outcome", "?"), "elapsed_ms": entry.get("elapsed_ms", 0),
                "loops_used": entry.get("loops_used", 0), "token_usage": entry.get("token_usage", 0),
                "quality_score": entry.get("quality_score", -1),
                "artifact_size_bytes": entry.get("artifact_size_bytes", 0),
                "cached_tokens": entry.get("cached_tokens", 0),
                "prompt_tokens": entry.get("prompt_tokens", 0),
                "sys_prompt_chars": entry.get("sys_prompt_chars", 0),
                "user_prompt_chars": entry.get("user_prompt_chars", 0),
                "model_used": entry.get("model_used") or None,
                "provider": entry.get("provider") or None,
                # None, not "" / 0: these three are nullable on purpose, and an
                # empty string would read as "we looked and there was no
                # error" on a SUCCESS row rather than "not applicable".
                "error_message": entry.get("error_message") or None,
                "failure_reason": entry.get("failure_reason") or None,
                "attempt_no": entry.get("attempt_no"),
            }
            for entry in entries
        ]
        # RETURNING id, created_at: the mirror must carry PG's serial id and
        # default timestamp, not invent its own. (Until 2026-08-16 it stamped
        # a random uuid + its own now(), leaving the Mongo collection with an
        # id-space disjoint from PG — unfindable by key, unverifiable.)
        _keyed = []
        with get_db() as db:
            for r in _recs:
                row = db.execute(
                    """
                    INSERT INTO v3_agent_telemetry
                        (cycle_id, ticker, agent_name, phase, outcome,
                         elapsed_ms, loops_used, token_usage, quality_score,
                         artifact_size_bytes, cached_tokens, prompt_tokens,
                         sys_prompt_chars, user_prompt_chars, model_used, provider,
                         error_message, failure_reason, attempt_no)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, created_at
                    """,
                    [r["cycle_id"], r["ticker"], r["agent_name"], r["phase"], r["outcome"],
                     r["elapsed_ms"], r["loops_used"], r["token_usage"], r["quality_score"],
                     r["artifact_size_bytes"], r["cached_tokens"], r["prompt_tokens"],
                     r["sys_prompt_chars"], r["user_prompt_chars"], r["model_used"], r["provider"],
                     r["error_message"], r["failure_reason"], r["attempt_no"]],
                ).fetchone()
                # row is None only under mocked cursors; a real INSERT..RETURNING
                # always yields one row. Mirror only what PG confirmed.
                if row is not None:
                    _keyed.append({**r, "id": row[0], "created_at": row[1]})
        try:
            from app.db import mongo_store
            if _keyed and mongo_store.writes_mongo("v3_agent_telemetry"):
                mongo_store.insert_docs("v3_agent_telemetry", _keyed)
        except Exception as me:
            logger.warning("[V3Telemetry] Mongo mirror failed (non-fatal): %s", me)
        logger.info(
            "[V3Telemetry] Persisted %d telemetry entries for %s/%s",
            len(_recs),
            desk.cycle_id[:12] if desk.cycle_id else "?",
            desk.ticker,
        )
    except Exception as e:
        # RE-RAISE. The caller marks entries as written only when this returns,
        # so swallowing here would mark unwritten rows as billed and lose the
        # cost record permanently — the very defect this path exists to fix.
        # `flush_agent_telemetry` catches it, leaving the entries pending for the
        # next save.
        logger.warning("[V3Telemetry] Failed to persist telemetry: %s", e)
        raise


#: Long enough to keep a real exception line intact, short enough that a
#: runaway model buffer cannot turn one failed run into a megabyte row.
_ERROR_MESSAGE_MAX = 512


def sanitize_error_message(raw_error: str | None, max_length: int = _ERROR_MESSAGE_MAX) -> str:
    """Flatten an exception or diagnostic into one storable, readable line.

    Three jobs, in order:

    1. **Keep the useful line.** A formatted traceback's last line is the
       exception; its first is the word "Traceback". Storing the head throws
       away the only part that names the failure, so a multi-line traceback is
       reduced to its FINAL line rather than to a generic placeholder.
    2. **Flatten.** Newlines and control characters make a row unreadable in a
       tooltip and unsearchable with a LIKE, so every run of whitespace (and
       any C0 control byte) collapses to a single space.
    3. **Cap.** Truncation is marked with an ellipsis so a clipped message is
       never mistaken for a complete short one.
    """
    if not raw_error:
        return ""

    text = str(raw_error)
    if text.lstrip().startswith("Traceback"):
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if lines:
            text = lines[-1]

    # Control characters (including \n, \r, \t) → space, then collapse runs.
    text = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)
    text = " ".join(text.split()).strip()

    if len(text) > max_length:
        text = text[: max(0, max_length - 1)].rstrip() + "…"
    return text


_GUARDRAIL_TABLE_ENSURED = False


def _ensure_guardrail_table() -> None:
    """Create the v3_guardrail_firings table if it doesn't exist."""
    global _GUARDRAIL_TABLE_ENSURED
    if _GUARDRAIL_TABLE_ENSURED:
        return

    from app.db.connection import get_db

    try:
        with get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS v3_guardrail_firings (
                    id SERIAL PRIMARY KEY,
                    guardrail TEXT NOT NULL,
                    cycle_id TEXT,
                    ticker TEXT,
                    detail JSONB,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_v3_guardrail_name
                ON v3_guardrail_firings (guardrail, created_at)
            """)
        _GUARDRAIL_TABLE_ENSURED = True
    except Exception as e:
        logger.warning("[V3Telemetry] Failed to ensure guardrail table: %s", e)


def record_guardrail_firing(
    guardrail: str,
    *,
    ticker: str = "",
    cycle_id: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """Record that a safety guardrail rewrote or blocked something.

    Guardrails used to leave their evidence ONLY in artifact metadata. On
    2026-07-25 that caused a real misdiagnosis: `coerce_unshortable_sell` fired
    on AMD, the log line named no ticker, and a reviewer grepping the logs
    concluded the guardrail had never run — and therefore that the prompt fix
    alone had been sufficient. It had not; the backstop was load-bearing.

    A guardrail that cannot be counted cannot be trusted or tuned, so every
    firing lands in a queryable table. Fail-open: telemetry must never break
    the pipeline it observes.
    """
    _ensure_guardrail_table()
    try:
        from app.db.connection import get_db

        with get_db() as db:
            mongo_store.insert_docs('v3_guardrail_firings', [{'guardrail': guardrail, 'cycle_id': cycle_id or None, 'ticker': ticker or None, 'detail': json.dumps(detail or {}, default=str)}])
    except Exception as e:
        logger.warning(
            "[V3Telemetry] guardrail firing not recorded (non-fatal): %s", e
        )


def get_pipeline_summary(desk: SharedDesk) -> dict[str, Any]:
    """Build a summary of the pipeline's telemetry for logging/display."""
    total_ms = sum(e.get("elapsed_ms", 0) for e in desk.agent_telemetry)
    total_tokens = sum(e.get("token_usage", 0) for e in desk.agent_telemetry)
    agents_run = [e.get("agent_name", "?") for e in desk.agent_telemetry]
    outcomes = {
        e.get("agent_name", "?"): e.get("outcome", "?")
        for e in desk.agent_telemetry
    }

    return {
        "cycle_id": desk.cycle_id,
        "ticker": desk.ticker,
        "final_phase": desk.phase.value,
        "agents_run": agents_run,
        "agent_count": len(agents_run),
        "total_elapsed_ms": total_ms,
        "total_tokens": total_tokens,
        "outcomes": outcomes,
        "phase_outcomes": desk.phase_outcomes,
    }
