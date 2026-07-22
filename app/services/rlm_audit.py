import logging
import uuid
import hashlib
from datetime import datetime, timezone
from app.db.connection import get_db

logger = logging.getLogger(__name__)


def log_rlm_audit_trail(
    cycle_id: str,
    bot_id: str,
    ticker: str,
    context: str,
    trading_system_prompt: str,
    active_model: str,
    response_text: str,
    tokens_used: int,
    execution_time: float,
    agent_step: str = "analysis",
    endpoint_name: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    queue_wait_ms: int = 0,
) -> None:
    """Log to PostgreSQL (with context dedup + per-box telemetry)."""
    from app.utils.text_utils import sanitize_surrogates
    context = sanitize_surrogates(context)
    trading_system_prompt = sanitize_surrogates(trading_system_prompt)
    response_text = sanitize_surrogates(response_text)

    try:
        with get_db() as db:
            # SHA256-hash context and system prompt for dedup storage
            ctx_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
            prompt_hash = hashlib.sha256(
                trading_system_prompt.encode("utf-8")
            ).hexdigest()

            # Insert blobs only if they don't already exist (dedup)
            for blob_hash, blob_content in [
                (ctx_hash, context),
                (prompt_hash, trading_system_prompt),
            ]:
                db.execute(
                    """
                    INSERT INTO context_blobs (context_hash, content, byte_size)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (context_hash) DO NOTHING
                """,
                    [blob_hash, blob_content, len(blob_content.encode("utf-8"))],
                )

            # Compute tokens per second
            exec_ms = int(execution_time * 1000)
            tok_per_sec = None
            if completion_tokens > 0 and exec_ms > 0:
                tok_per_sec = round(completion_tokens / (exec_ms / 1000), 1)

            # Store only hashes in the audit log row. Build once so PG + Mongo share the id.
            _rec = {
                "id": str(uuid.uuid4()), "cycle_id": cycle_id, "bot_id": bot_id, "ticker": ticker,
                "agent_step": agent_step, "model": active_model, "system_prompt_hash": prompt_hash,
                "context_hash": ctx_hash, "raw_response": response_text, "tokens_used": tokens_used,
                "execution_ms": exec_ms, "created_at": datetime.now(timezone.utc),
                "endpoint_name": endpoint_name or None, "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens, "queue_wait_ms": queue_wait_ms,
                "tokens_per_second": tok_per_sec,
            }
            db.execute(
                """
                INSERT INTO llm_audit_logs (
                    id, cycle_id, bot_id, ticker, agent_step, model, system_prompt_hash,
                    context_hash, raw_response, tokens_used, execution_ms, created_at,
                    endpoint_name, prompt_tokens, completion_tokens,
                    queue_wait_ms, tokens_per_second
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                [_rec["id"], _rec["cycle_id"], _rec["bot_id"], _rec["ticker"], _rec["agent_step"],
                 _rec["model"], _rec["system_prompt_hash"], _rec["context_hash"], _rec["raw_response"],
                 _rec["tokens_used"], _rec["execution_ms"], _rec["created_at"], _rec["endpoint_name"],
                 _rec["prompt_tokens"], _rec["completion_tokens"], _rec["queue_wait_ms"], _rec["tokens_per_second"]],
            )
            try:
                from app.db import mongo_store
                if mongo_store.writes_mongo("llm_audit_logs"):
                    mongo_store.insert_docs("llm_audit_logs", [_rec])
            except Exception:
                pass
            logger.debug(
                "[DB] Successfully wrote trace to llm_audit_logs for %s (ctx_hash=%s...)",
                ticker,
                ctx_hash[:12],
            )
    except Exception as db_e:
        logger.error("[RLM] [DB] Audit log un-writable for %s: %s", ticker, db_e)
