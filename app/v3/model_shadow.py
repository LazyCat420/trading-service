"""Model shadow runs — benchmark a second box on the SAME input, off the critical path.

Purpose: answer "which jobs can the Jetson actually do" with evidence instead of
a guess, WITHOUT letting a weaker model touch a live trading decision.

For a configured agent, the primary call runs exactly as today and its answer is
the only one the pipeline ever sees. Afterwards a shadow copy of the same prompt
is sent to a second endpoint and both sides are recorded side by side, so
agreement can be scored later.

Three properties this module must never lose:

1. **The shadow cannot change a decision.** Its result is written to
   `model_shadow_runs` and returned to nobody. There is no code path from a
   shadow result into the desk.
2. **The shadow cannot fail the cycle.** Every exception is swallowed and
   recorded as a shadow_outcome. A benchmark that can break trading is not a
   benchmark worth having.
3. **The shadow cannot slow the cycle.** It is dispatched as a detached task
   after the primary already returned, and nothing awaits it.

Deliberately NOT written to `v3_agent_telemetry`: that table feeds the model
leaderboard and `decision_outcomes.models_used`, so a shadow row there would
credit the shadow model with participating in a decision it had no part in —
the double-counting trap in [[model-attribution-and-model-stats-tab]].
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_TABLE_ENSURED = False

#: Cap on stored response text. Enough to parse the artifact JSON out of, far
#: short of letting a runaway response bloat the table.
_MAX_TEXT = 20000


def _ensure_shadow_table() -> None:
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return
    from app.db.connection import get_db
    try:
        with get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS model_shadow_runs (
                    id SERIAL PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    primary_model TEXT,
                    primary_provider TEXT,
                    primary_elapsed_ms INTEGER DEFAULT 0,
                    primary_tokens INTEGER DEFAULT 0,
                    primary_loops INTEGER DEFAULT 0,
                    primary_text TEXT,
                    shadow_model TEXT,
                    shadow_provider TEXT,
                    shadow_elapsed_ms INTEGER DEFAULT 0,
                    shadow_tokens INTEGER DEFAULT 0,
                    shadow_loops INTEGER DEFAULT 0,
                    shadow_outcome TEXT NOT NULL,
                    shadow_error TEXT,
                    shadow_text TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            # The prompt that was actually compared (2026-08-05). Without it a
            # row records two answers to a question nobody kept, so the run can
            # never be replayed or re-scored — and replay is the only way to
            # reach a useful n: a real cycle costs ~8 minutes per sample, the
            # same prompt replayed costs seconds.
            db.execute("""
                DO $$ BEGIN
                    ALTER TABLE model_shadow_runs ADD COLUMN IF NOT EXISTS system_prompt TEXT;
                    ALTER TABLE model_shadow_runs ADD COLUMN IF NOT EXISTS user_prompt TEXT;
                EXCEPTION WHEN others THEN NULL;
                END $$;
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_model_shadow_agent
                ON model_shadow_runs (agent_name, created_at)
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_model_shadow_cycle
                ON model_shadow_runs (cycle_id)
            """)
        _TABLE_ENSURED = True
    except Exception as e:
        logger.warning("[ModelShadow] Failed to ensure table: %s", e)


def shadow_endpoint_for(agent_name: str) -> str | None:
    """The endpoint to shadow this agent on, or None if it isn't configured.

    Reads config at call time rather than import time so the agent list can be
    changed by redeploy without a code change.
    """
    try:
        from app.config.config_cognition import cognition_settings
        raw = (cognition_settings.MODEL_SHADOW_AGENTS or "").strip()
        if not raw:
            return None
        wanted = {a.strip() for a in raw.split(",") if a.strip()}
        if agent_name not in wanted:
            return None
        return (cognition_settings.MODEL_SHADOW_ENDPOINT or "jetson").strip() or None
    except Exception as e:
        logger.debug("[ModelShadow] config read failed: %s", e)
        return None


#: Harness/guard failures come back as a NORMAL return value with an error
#: string in `response` — no exception is raised. Recording those as SUCCESS
#: is how a box that refused the job entirely gets reported as "28x faster
#: than Gold Spark" (observed 2026-08-04: prism returned a 0-token-window
#: error for jetson in 1046ms and it was booked as a win).
_FAILURE_MARKERS = (
    "context window is critically full",
    "⚠️ **Error:**",
    "Summarizing progress so far",
)


def classify_shadow(text: str, tokens: int, loops: int) -> tuple[str, str | None]:
    """(outcome, error) for a shadow that returned without raising.

    Fail-closed: anything that does not look like real generated work is a
    failure, because the cost of crediting a refusal as a success is a
    benchmark that recommends the wrong box.
    """
    body = (text or "").strip()
    if not body:
        return "EMPTY_RESPONSE", "empty response text"
    for marker in _FAILURE_MARKERS:
        if marker in body:
            return "HARNESS_ERROR", body[:500]
    if not tokens:
        # Zero billed tokens means the model never generated anything, however
        # plausible the returned string looks.
        return "NO_TOKENS", "0 tokens billed — no generation occurred"
    return "SUCCESS", None


def _record(row: dict) -> None:
    _ensure_shadow_table()
    try:
        from app.db.connection import get_db
        with get_db() as db:
            db.execute(
                """
                INSERT INTO model_shadow_runs (
                    cycle_id, ticker, agent_name, endpoint,
                    primary_model, primary_provider, primary_elapsed_ms,
                    primary_tokens, primary_loops, primary_text,
                    shadow_model, shadow_provider, shadow_elapsed_ms,
                    shadow_tokens, shadow_loops, shadow_outcome,
                    shadow_error, shadow_text, system_prompt, user_prompt
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    row["cycle_id"], row["ticker"], row["agent_name"], row["endpoint"],
                    row.get("primary_model"), row.get("primary_provider"),
                    row.get("primary_elapsed_ms") or 0, row.get("primary_tokens") or 0,
                    row.get("primary_loops") or 0, (row.get("primary_text") or "")[:_MAX_TEXT],
                    row.get("shadow_model"), row.get("shadow_provider"),
                    row.get("shadow_elapsed_ms") or 0, row.get("shadow_tokens") or 0,
                    row.get("shadow_loops") or 0, row["shadow_outcome"],
                    (row.get("shadow_error") or None), (row.get("shadow_text") or "")[:_MAX_TEXT],
                    # NOT capped at _MAX_TEXT: a truncated prompt cannot be
                    # replayed, which is the whole reason it is stored.
                    row.get("system_prompt"), row.get("user_prompt"),
                ),
            )
    except Exception as e:
        logger.warning("[ModelShadow] Failed to record %s: %s", row.get("agent_name"), e)


async def _prism_chat(
    *, provider: str, model: str, system_prompt: str, user_prompt: str,
    max_tokens: int, timeout_seconds: float,
) -> dict:
    """Call prism's TOOL-LESS /chat endpoint and collect the SSE stream.

    Thin alias for `prism_agent_caller.chat_toolless`, which holds the
    transport and the measurements for why `/agent` is unusable here. Kept as a
    named function because the CAVEAT below is specific to the shadow bench and
    does not belong on the shared helper.

    CAVEAT this imposes on the comparison: the shadow runs the job WITHOUT
    tools while the primary ran WITH them. For v3_regime_engine that is still
    like-for-like — its own module records that its tools have had zero calls
    in 60 days, so the tool-enabled primary is not actually using them — but
    it would NOT be a fair comparison for a genuinely tool-using role. Do not
    reuse this path for one without re-checking that agent's tool call counts.
    """
    from app.services.prism_agent_caller import chat_toolless

    return await chat_toolless(
        provider=provider, model=model,
        system_prompt=system_prompt, user_prompt=user_prompt,
        max_tokens=max_tokens, timeout_seconds=timeout_seconds,
    )


async def _run_and_record(
    *, endpoint: str, agent_name: str, ticker: str, cycle_id: str, bot_id: str,
    system_prompt: str, user_prompt: str, max_tokens: int,
    timeout_seconds: float, primary: dict,
) -> None:
    # NB: deliberately takes no enable_tools/prism_overrides. The /chat path
    # cannot honour either, and a parameter the executor ignores is how
    # `endpoint_override` sat dead in three signatures for months.
    import time as _time

    base = {
        "cycle_id": cycle_id, "ticker": ticker, "agent_name": agent_name,
        "endpoint": endpoint,
        "primary_model": primary.get("model_used"),
        "primary_provider": primary.get("provider"),
        "primary_elapsed_ms": primary.get("elapsed_ms"),
        "primary_tokens": primary.get("tokens_used"),
        "primary_loops": primary.get("loops_used"),
        "primary_text": primary.get("response"),
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
    }

    t0 = _time.monotonic()
    try:
        from app.services.prism_agent_caller import (
            ENDPOINT_PROVIDERS, get_live_model_from_vllm, llm,
        )
        ep = llm._endpoints.get(endpoint)
        if not ep or not ep.url:
            raise RuntimeError(f"endpoint {endpoint!r} is not configured")
        result = await asyncio.wait_for(
            _prism_chat(
                provider=ENDPOINT_PROVIDERS[endpoint],
                model=await get_live_model_from_vllm(ep.url),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                # The primary's budget was sized against Gold Spark's 1M
                # window; reusing it here can push prompt+output past 65k and
                # produce a context rejection that looks like a model failure.
                # Floor 4096 is prism's ContextExhaustionGuard minimum.
                max_tokens=max(4096, min(max_tokens, 8192)),
                timeout_seconds=timeout_seconds,
            ),
            timeout=timeout_seconds,
        )
        elapsed = int((_time.monotonic() - t0) * 1000)
        text = result.get("response") or ""
        tokens = result.get("tokens_used") or 0
        loops = result.get("loops_used") or 0
        outcome, err = classify_shadow(text, tokens, loops)
        _record({
            **base,
            "shadow_model": result.get("model_used"),
            "shadow_provider": result.get("provider"),
            "shadow_elapsed_ms": elapsed,
            "shadow_tokens": tokens,
            "shadow_loops": loops,
            "shadow_outcome": outcome,
            "shadow_error": err,
            "shadow_text": text,
        })
        if outcome == "SUCCESS":
            logger.info(
                "[ModelShadow] %s on %s: SUCCESS %dms/%dtok vs primary %sms/%stok (model=%s)",
                agent_name, endpoint, elapsed, tokens,
                primary.get("elapsed_ms"), primary.get("tokens_used"),
                result.get("model_used"),
            )
        else:
            # Loud, because a fast failure is the one result most likely to be
            # mistaken for a fast success.
            logger.warning(
                "[ModelShadow] %s on %s: %s after %dms (%dtok) — NOT a win: %s",
                agent_name, endpoint, outcome, elapsed, tokens, (err or "")[:160],
            )
    except asyncio.TimeoutError:
        # Recorded, not dropped: a box that times out on a job has FAILED that
        # job, and dropping the row would flatter it by making the failure
        # invisible in the comparison.
        _record({**base, "shadow_outcome": "TIMED_OUT",
                 "shadow_elapsed_ms": int((_time.monotonic() - t0) * 1000)})
        logger.warning("[ModelShadow] %s on %s TIMED OUT", agent_name, endpoint)
    except Exception as e:
        _record({**base, "shadow_outcome": "AGENT_ERROR", "shadow_error": str(e)[:500],
                 "shadow_elapsed_ms": int((_time.monotonic() - t0) * 1000)})
        logger.warning("[ModelShadow] %s on %s FAILED: %s", agent_name, endpoint, e)


def dispatch_shadow(**kwargs) -> None:
    """Fire a shadow run without awaiting it. Never raises.

    Detached on purpose — the cycle must not wait for the slower box. The task
    is held in a module-level set so it is not garbage-collected mid-flight
    (asyncio keeps only a weak reference to bare tasks).
    """
    try:
        task = asyncio.create_task(_run_and_record(**kwargs))
        _INFLIGHT.add(task)
        task.add_done_callback(_INFLIGHT.discard)
    except Exception as e:
        logger.warning("[ModelShadow] dispatch failed for %s: %s",
                       kwargs.get("agent_name"), e)


_INFLIGHT: set = set()
