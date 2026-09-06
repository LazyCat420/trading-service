from app.utils.text_utils import parse_json_response
import logging
import json
import uuid
import asyncio
from datetime import datetime, timezone

from app.services.memory.procedural_memory import procedural_memory_store

from app.db.memory_repo import (
    get_unpromoted_observations,
    get_active_canonical_memories,
    upsert_canonical_memories,
    deprecate_canonical_memories,
    mark_observations_promoted,
    log_consolidation_run,
)
from app.services.prism_agent_caller import Priority
from app.services.prism_agent_caller import call_prism_agent

import time

logger = logging.getLogger(__name__)

NEW_EPISODIC_THRESHOLD = 5

# Per-ticker attempt cooldown. Without it, a persistently failing LLM pass
# leaves the observations unpromoted, should-consolidate stays true, and the
# 8k-token consolidation call re-fires on EVERY subsequent cycle.
CONSOLIDATION_COOLDOWN_SECONDS = 6 * 3600
#: After a TRANSIENT failure (a dropped socket, a 5xx, a timeout — whatever the
#: SDK's classify_exception calls transient) the gate re-opens this soon
#: instead of after the full cooldown. MEASURED 2026-09-06, cycle-v3-1788660665
#: 03:51:27: "Failed consolidation for NBIS: Server disconnected without
#: sending a response" — the stamp was written BEFORE the call, so one socket
#: cost the ticker six hours of memory consolidation.
TRANSIENT_RETRY_SECONDS = 10 * 60
_last_attempt: dict[str, float] = {}

CONSOLIDATION_SYSTEM_PROMPT = """
You are the Autodream Memory Consolidator, a background system optimizing a trading AI's knowledge base.
Your job is to read raw "episodic observations" and existing "canonical memories" for a given ticker, and merge them into a cleaner set of canonical rules.

RULES:
1. Combine redundant rules/observations.
2. Contradictions: If new episodic observations heavily contradict an existing canonical memory, you must DEPRECATE the old memory and replace it, or lower its confidence score.
3. Your output MUST be strictly valid JSON without markdown wrapping or backticks.
4. PROCEDURAL PATTERNS: If the observations reveal a repeatable setup→action that worked (or should be repeated), emit it under `procedural_patterns` as a trigger→procedure pair. Omit the field entirely if none are evident. Do NOT invent patterns.

OUTPUT FORMAT:
{
  "new_or_updated_memories": [
    {
       "id": "UUID-OR-EXISTING-ID",
       "type": "market_pattern", // "market_pattern" | "ticker_quirk" | "failure_pattern" | "regime" | "execution_rule"
       "ticker": "...",
       "sector": "...",
       "summary": "...",
       "tags": ["..."],
       "confidence_score": 0.0 - 1.0,
       "evidence_count": integer
    }
  ],
  "deprecated_memory_ids": ["id-1", "id-2"],
  "procedural_patterns": [
    {
       "trigger_pattern": "the recurring condition/setup, e.g. 'RSI < 30 after an earnings gap-down'",
       "procedure": "the action/playbook that worked, e.g. 'wait for reclaim of prior day high before entering'"
    }
  ]
}

Important notes on output:
- To UPDATE an existing canonical memory, emit it in `new_or_updated_memories` using its CURRENT `id`.
- To CREATE a new canonical memory, leave the `id` blank or generate a descriptive string.
- To DEPRECATE a memory completely, place its `id` in `deprecated_memory_ids`.
"""


async def maybe_consolidate(ticker: str) -> None:
    """Threshold + cooldown gate, then consolidate. Never raises — safe to
    schedule fire-and-forget off the pipeline's critical path."""
    try:
        now = time.monotonic()
        last = _last_attempt.get(ticker)
        if last is not None and (now - last) < CONSOLIDATION_COOLDOWN_SECONDS:
            return
        observations = get_unpromoted_observations(ticker)
        if len(observations) < NEW_EPISODIC_THRESHOLD:
            return
        # Stamp AFTER the outcome is known. A transient transport failure
        # re-opens the gate after TRANSIENT_RETRY_SECONDS; success and every
        # non-transient failure keep the full cooldown.
        try:
            outcome = await run_ticker_consolidation(ticker, observations=observations)
        except Exception as e:
            # An exception that ESCAPES the callee is still an attempt.
            # `run_ticker_consolidation` classifies only what its own inner try
            # catches; a store blip in `get_active_canonical_memories`, which
            # runs before it, lands here — and with the stamp moved after the
            # outcome, the gate never closed at all: the ticker re-attempted on
            # every cycle instead of once every six hours.
            from lazycat.resilience import FailureType, classify_exception

            outcome = "transient" if classify_exception(e) is FailureType.TRANSIENT else "failed"
            logger.warning(
                "Consolidation for %s failed before it could classify itself (%s): %s",
                ticker, outcome, e,
            )
        if outcome == "transient":
            _last_attempt[ticker] = now - CONSOLIDATION_COOLDOWN_SECONDS + TRANSIENT_RETRY_SECONDS
        else:
            _last_attempt[ticker] = now
    except Exception as e:
        logger.warning("maybe_consolidate(%s) failed (non-fatal): %s", ticker, e)


async def run_ticker_consolidation(ticker: str, observations: list | None = None):
    logger.info(f"Starting consolidation for {ticker}...")
    if observations is None:
        observations = get_unpromoted_observations(ticker)

    if not observations:
        logger.info(f"No unpromoted observations for {ticker}.")
        return

    canonicals = get_active_canonical_memories(ticker)

    # Prompt synthesis
    user_prompt = f"TICKER: {ticker}\n\n"

    user_prompt += "=== EXISTING CANONICAL MEMORIES ===\n"
    if not canonicals:
        user_prompt += "(None)\n"
    else:
        for c in canonicals:
            user_prompt += f"ID: {c['id']} | Type: {c.get('type')} | Conf: {c.get('confidence_score')}\n"
            user_prompt += f"Summary: {c.get('summary')}\n\n"

    user_prompt += "=== NEW EPISODIC OBSERVATIONS ===\n"
    # Two kinds of row land here and they must not be rendered alike. A
    # decision-time row carries the ACTION in `outcome_label` ("BUY"), and this
    # block used to print it as `Outcome: BUY (None)` — telling the model that
    # deciding to buy WAS the outcome. Only `source_type='outcome'` rows, which
    # the resolvers write once the 7-day move is known, carry a real one.
    for o in observations:
        user_prompt += f"Obs [{o['created_at']}]: {o.get('observation_text')}\n"
        label = o.get("outcome_label")
        if o.get("source_type") == "outcome":
            score = o.get("outcome_score")
            move = f"{float(score):+.2f}%" if score is not None else "unknown move"
            user_prompt += f"RESOLVED OUTCOME: {label} ({move})\n\n"
        else:
            user_prompt += f"Action taken: {label} (outcome not yet resolved)\n\n"

    # Execute LLM call
    try:
        response_text, _, _ = await call_prism_agent(
            agent_id="CUSTOM_CONSOLIDATOR_AGENT",
            user_message=user_prompt,
            fallback_system_prompt=CONSOLIDATION_SYSTEM_PROMPT,
            fallback_agent_name="memory_consolidator",
            temperature=0.2,
            max_tokens=8192,
            priority=Priority.LOW,
            ticker=ticker,
        )

        # Clean JSON blocks
        parsed_res = parse_json_response(response_text)

        updated_mems = parsed_res.get("new_or_updated_memories", [])
        deprecated_ids = parsed_res.get("deprecated_memory_ids", [])

        if not updated_mems and not deprecated_ids:
            # Nothing extracted — either the LLM output failed to parse or the
            # response was genuinely empty. Do NOT mark the observations
            # promoted: promotion consumes them (the janitor deletes promoted
            # rows after 30 days), so promoting with zero memories created
            # permanently destroys the knowledge. Leave them for the next run
            # (the per-ticker cooldown prevents hammering).
            logger.warning(
                "Consolidation for %s extracted nothing — leaving %d observations "
                "unpromoted for retry. Raw response head: %r",
                ticker, len(observations), (response_text or "")[:500],
            )
            log_consolidation_run(
                {
                    "id": str(uuid.uuid4()),
                    "ticker": ticker,
                    "observations_consumed": 0,
                    "memories_created": 0,
                    "memories_deprecated": 0,
                }
            )
            return

        # Fill missing IDs and defaults
        for mem in updated_mems:
            if "id" not in mem or not mem["id"]:
                mem["id"] = str(uuid.uuid4())
            mem["ticker"] = ticker
            mem["status"] = "active"
            if "created_at" not in mem:
                mem["created_at"] = datetime.now(timezone.utc).isoformat()

        upsert_canonical_memories(updated_mems)
        deprecate_canonical_memories(deprecated_ids)

        # Procedural patterns (setup→action playbooks) extracted from the same
        # LLM pass. Deduped by (ticker, trigger_pattern) so re-runs don't pile up.
        procedural_written = 0
        for pat in parsed_res.get("procedural_patterns", []) or []:
            try:
                trig = (pat.get("trigger_pattern") or "").strip()
                proc = pat.get("procedure")
                if isinstance(proc, (dict, list)):
                    proc = json.dumps(proc)
                proc = (proc or "").strip() if isinstance(proc, str) else str(proc or "")
                if trig and proc:
                    procedural_memory_store.write_procedure_if_new(
                        ticker, trig, proc, created_by_agent="consolidator"
                    )
                    procedural_written += 1
            except Exception as pe:
                logger.warning("Procedural write failed for %s: %s", ticker, pe)
        if procedural_written:
            logger.info(
                "Consolidation wrote %d procedural pattern(s) for %s",
                procedural_written, ticker,
            )

        obs_ids = [o["id"] for o in observations]
        mark_observations_promoted(obs_ids)

        log_consolidation_run(
            {
                "id": str(uuid.uuid4()),
                "ticker": ticker,
                "observations_consumed": len(obs_ids),
                "memories_created": len(updated_mems),
                "memories_deprecated": len(deprecated_ids),
            }
        )

        logger.info(
            f"Consolidation complete for {ticker}: {len(updated_mems)} upserted, {len(deprecated_ids)} deprecated."
        )

    except asyncio.TimeoutError:
        logger.error(f"Timeout during consolidation for {ticker}")
        return "transient"
    except Exception as e:
        logger.error(f"Failed consolidation for {ticker}: {e}")
        # One classifier for "was that the transport or the model" — the same
        # one aresilient_call uses, so this never disagrees with a retry.
        try:
            from lazycat.resilience import FailureType, classify_exception
            if classify_exception(e) == FailureType.TRANSIENT:
                return "transient"
        except Exception:  # noqa: BLE001 — classification must not mask the error
            pass
        return "failed"
    return "ok"


# _parse_json_response removed — use app.utils.text_utils.parse_json_response
