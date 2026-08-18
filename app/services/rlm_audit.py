import logging
import uuid
import hashlib
from datetime import datetime, timezone
from app.db import mongo_store

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
    """Log to MongoDB context_blobs and llm_audit_logs with context dedup + telemetry."""
    from app.utils.text_utils import sanitize_surrogates
    context = sanitize_surrogates(context)
    trading_system_prompt = sanitize_surrogates(trading_system_prompt)
    response_text = sanitize_surrogates(response_text)

    try:
        # SHA256-hash context and system prompt for dedup storage
        ctx_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
        prompt_hash = hashlib.sha256(
            trading_system_prompt.encode("utf-8")
        ).hexdigest()

        now_utc = datetime.now(timezone.utc)
        for blob_hash, blob_content in [
            (ctx_hash, context),
            (prompt_hash, trading_system_prompt),
        ]:
            mongo_store.upsert_doc(
                "context_blobs",
                {"context_hash": blob_hash},
                {
                    "context_hash": blob_hash,
                    "content": blob_content,
                    "byte_size": len(blob_content.encode("utf-8")),
                    "created_at": now_utc,
                },
                insert_only=True,
            )

        # Compute tokens per second
        exec_ms = int(execution_time * 1000)
        tok_per_sec = None
        if completion_tokens > 0 and exec_ms > 0:
            tok_per_sec = round(completion_tokens / (exec_ms / 1000), 1)

        # Store only hashes in the audit log row
        _rec = {
            "id": str(uuid.uuid4()),
            "cycle_id": cycle_id,
            "bot_id": bot_id,
            "ticker": ticker,
            "agent_step": agent_step,
            "model": active_model,
            "system_prompt_hash": prompt_hash,
            "context_hash": ctx_hash,
            "raw_response": response_text,
            "tokens_used": tokens_used,
            "execution_ms": exec_ms,
            "created_at": now_utc,
            "endpoint_name": endpoint_name or None,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "queue_wait_ms": queue_wait_ms,
            "tokens_per_second": tok_per_sec,
        }
        mongo_store.insert_docs("llm_audit_logs", [_rec])
        logger.debug(
            "[DB] Successfully wrote trace to llm_audit_logs for %s (ctx_hash=%s...)",
            ticker,
            ctx_hash[:12],
        )
    except Exception as db_e:
        logger.error("[RLM] [DB] Audit log un-writable for %s: %s", ticker, db_e)
