"""
V3 Agent Runner — Wraps the existing agent_loop with V3 guardrails.

This is the bridge between the V3 orchestrator and the existing
run_agent_loop() infrastructure. It handles:
1. Building the system prompt from agent config + SharedDesk context
2. Injecting the tool whitelist for the agent's role
3. Passing V3AgentBudget with role-specific limits
4. Parsing the output into the expected artifact schema
5. Appending the artifact to the SharedDesk
6. Running context compression
7. Recording telemetry
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.v3.shared_desk import SharedDesk, PhaseOutcome
# NOTE: the live turn budget is get_agent_budget_turns (tool_whitelists), not
# guardrails.get_budget_for_role — that one only serves the non-V3 prism path.
from app.v3.guardrails import (
    enter_v3_session,
    exit_v3_session,
)
from app.v3.artifacts import validate_artifact
from app.v3.quality_scorer import score_artifact

logger = logging.getLogger(__name__)

# Tool-playbook tips cache: (agent_name -> (tips, fetched_monotonic)).
_PLAYBOOK_CACHE: dict[str, tuple[str, float]] = {}
_PLAYBOOK_TTL_SEC = 3600.0


_REQUESTED_MAX_TOKENS = 8192


def _safe_max_tokens(
    *,
    agent_name: str,
    system_prompt: str,
    user_prompt: str,
    tool_whitelist: list[str] | None,
) -> int:
    """Output budget computed from the assembled payload, not a flat constant.

    The V3 path previously passed ``max_tokens=8192`` unconditionally while
    ``context_gate`` — a complete, tested tiktoken budgeter — had no production
    callers at all. A tool-enabled agent carries its schemas as the single
    largest fixed input cost, and none of it was measured.

    Never raises: a budgeting failure must not take down an agent, so any error
    falls back to the historical constant.
    """
    try:
        from app.services.context_gate import compute_safe_max_tokens
        from app.config.context_budget import get_context_budget

        tools = None
        if tool_whitelist:
            from app.agents.tool_whitelists import get_agent_tools
            tools = get_agent_tools(agent_name)

        return compute_safe_max_tokens(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=tools,
            model_context=get_context_budget().raw_context_tokens,
            requested_max=_REQUESTED_MAX_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 — budgeting must never block a cycle
        logger.warning(
            "[V3Runner] %s: context_gate budgeting failed (%s) — falling back to %d",
            agent_name, e, _REQUESTED_MAX_TOKENS,
        )
        return _REQUESTED_MAX_TOKENS


def _get_tool_playbook_tips(agent_name: str, limit: int = 3) -> str:
    """Compact per-agent tool guidance from the eval layer's tool_playbook."""
    cached = _PLAYBOOK_CACHE.get(agent_name)
    if cached and (time.monotonic() - cached[1]) < _PLAYBOOK_TTL_SEC:
        return cached[0]
    tips = ""
    try:
        from app.db.connection import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT recommended_tool_sequence FROM tool_playbook "
                "WHERE agent_role = %s ORDER BY created_at DESC LIMIT %s",
                [agent_name, limit],
            ).fetchall()
        tips = "\n".join(f"- {r[0]}" for r in rows if r and r[0])
    except Exception as e:  # noqa: BLE001 — advisory context, never blocks the agent
        logger.debug("[V3Runner] tool_playbook fetch failed: %s", e)
    _PLAYBOOK_CACHE[agent_name] = (tips, time.monotonic())
    return tips


async def run_v3_agent(
    desk: SharedDesk,
    agent_module: Any,
    *,
    cycle_id: str = "",
    bot_id: str = "",
    emit: Any = None,
    timeout_seconds: float = 600.0,
    include_debate_context: bool = False,
    custom_instructions: str = "",
    parent_agent: str = "",
) -> PhaseOutcome:
    """Run a V3 agent against the SharedDesk.

    This wraps run_agent_loop() with V3-specific behavior:
    - Builds the user prompt from SharedDesk compressed context
    - Uses role-specific tool whitelists
    - Enforces V3AgentBudget (real limits, not V2's 9999)
    - Parses and validates the artifact output
    - Appends to SharedDesk on success

    Args:
        desk: The SharedDesk to read from and append to.
        agent_module: The agent module (e.g. app.v3.agents.junior_analyst).
        cycle_id: Current cycle ID.
        bot_id: Current bot ID.
        emit: Event emitter callback.
        timeout_seconds: Hard timeout for the entire agent run.
        include_debate_context: If True, include debate artifacts in context.

    Returns:
        PhaseOutcome indicating success or failure type.
    """
    # Scope any in-process tool execution (whiteboard, peer requests) to this
    # agent + cycle; the HTTP bridge path sets the same context from headers.
    from app.tools.tool_context import set_tool_context

    set_tool_context(
        agent_name=getattr(agent_module, "AGENT_NAME", None), cycle_id=cycle_id
    )
    from app.utils.pipeline_utils import noop as _noop
    if emit is None:
        emit = _noop

    agent_name = agent_module.AGENT_NAME
    artifact_type = agent_module.ARTIFACT_TYPE

    # Check for custom agent override execution
    if hasattr(agent_module, "run_custom_agent"):
        try:
            return await agent_module.run_custom_agent(
                desk=desk,
                cycle_id=cycle_id,
                bot_id=bot_id,
                emit=emit,
                timeout_seconds=timeout_seconds,
            )
        except Exception as custom_err:
            logger.error("[V3Runner] Custom agent execution failed: %s", custom_err)
            return PhaseOutcome.AGENT_ERROR

    system_prompt = agent_module.SYSTEM_PROMPT
    tool_whitelist = agent_module.TOOL_WHITELIST

    # SkillOpt: prepend this agent's learned skill doc ("" when none; served
    # from an in-process cache, so no per-run DB hit). The prefix only changes
    # when autoresearch accepts an edit, so the system prompt stays
    # byte-identical between mutations and vLLM prefix-cache reuse survives.
    try:
        from app.autoresearch.skill_loader import load_skill_prefix
        _skill_prefix = load_skill_prefix(agent_name)
        if _skill_prefix:
            system_prompt = _skill_prefix + system_prompt
    except Exception as skill_err:  # noqa: BLE001 — advisory, never blocks an agent
        logger.debug("[V3Runner] skill prefix load failed for %s: %s", agent_name, skill_err)

    session_key = f"{cycle_id}:{desk.ticker}:{agent_name}"
    t_start = time.monotonic()
    sys_prompt_chars = 0
    user_prompt_chars = 0

    emit(
        "analyzing",
        f"v3_{agent_name}_{desk.ticker}",
        f"🔬 {desk.ticker}: V3 {agent_name} starting...",
        status="running",
        data={
            "kind": "agent_start",
            "agent": agent_name,
            "ticker": desk.ticker,
            # parent_agent is the upstream agent whose artifact this one consumes —
            # the office uses it as the "who talks to whom" edge for face-to-face
            # talking/hand-off animations. `parent` kept for back-compat.
            "parent": parent_agent,
            "target": parent_agent,
        },
    )

    try:
        # Guard: prevent recursive agent spawning
        enter_v3_session(session_key)

        # ── KV-cache prompt split (plan 4.1/4.2, gated for rollback — 8.4) ──
        # The system prompt stays byte-identical across cycles/tickers for a
        # given agent type so the vLLM prefix cache can reuse it. ALL
        # cycle-specific content goes into the user message. Setting
        # V3_PROMPT_SPLIT=false restores the legacy append-to-system layout.
        from app.config import settings as _settings
        prompt_split = bool(getattr(_settings, "V3_PROMPT_SPLIT", True))

        desk_context = desk.get_compressed_context(include_debate=include_debate_context)

        # Locale directive: constant per deployment config → system prompt
        # (identical across cycles for the same locale, still cacheable).
        agent_locale = desk.cycle_metadata.get("agent_locale", "default")
        if agent_locale and agent_locale != "default":
            try:
                from app.config.locales import AGENT_LOCALES
                locale_override = AGENT_LOCALES.get(agent_locale)
                if locale_override:
                    system_prompt += locale_override
                else:
                    logger.warning(
                        "[V3Runner] %s: unknown agent_locale '%s' — no directive applied "
                        "(known: %s)", agent_name, agent_locale, sorted(AGENT_LOCALES),
                    )
            except Exception as e:
                logger.warning("[V3Runner] Failed to apply agent_locale %s: %s", agent_locale, e)

        # ── Cycle-specific (dynamic) sections ──
        # (shed_order, text). shed_order 0 never sheds; higher numbers are
        # dropped first when the block would overflow Prism's memory embedder.
        # Previously an oversized block was moved wholesale into the system
        # prompt, which relocated the tokens instead of removing them (the model
        # still received every one) AND silently defeated KV-cache reuse.
        _KEEP = 0
        dynamic_sections: list[tuple[int, str]] = []

        # Live macro snapshot — ONLY for the Regime Engine, which classifies
        # the global market state. Scoped to that agent so it doesn't bloat
        # every prompt (and the KV-cache user portion) with macro it ignores.
        if agent_name == "v3_regime_engine":
            macro_briefing = desk.cycle_metadata.get("macro_briefing", "")
            if macro_briefing:
                dynamic_sections.append((
                    _KEEP,
                    f"## LIVE MACRO SNAPSHOT (use this to classify the regime)\n{macro_briefing}",
                ))

        # Market data briefing first — it's the shared factual base (plan 4.2)
        data_report = desk.cycle_metadata.get("data_report", "")
        if data_report:
            if len(data_report) > 5000:
                data_report = data_report[:5000] + "\n...[TRUNCATED FOR LENGTH]..."
            dynamic_sections.append((
                _KEEP,
                f"## MARKET DATA BRIEFING FOR THIS CYCLE\n{data_report}",
            ))

        portfolio_ctx = desk.cycle_metadata.get("portfolio_context", "")
        if portfolio_ctx:
            dynamic_sections.append((2, f"## Portfolio Context\n{portfolio_ctx}"))

        directives_ctx = desk.cycle_metadata.get("directives_context", "")
        if directives_ctx:
            dynamic_sections.append((
                1,
                "## Active Directives (from AutoResearch — address if relevant)\n"
                f"{directives_ctx}",
            ))

        memory_context = desk.cycle_metadata.get("memory_context", "")
        if memory_context:
            dynamic_sections.append((5, f"## Past Cycle Memory\n{memory_context}"))

        # Deep decomposed recall — set by the orchestrator just before the
        # decision_synthesizer dispatch (only on low-confidence verdicts), so
        # in practice only the synthesizer sees it.
        deep_retrieval = desk.cycle_metadata.get("deep_retrieval_context", "")
        if deep_retrieval:
            dynamic_sections.append((
                3,
                f"## Deep Retrieved Context (conflicting-signal recall)\n{deep_retrieval}",
            ))

        previous_desk_context = desk.cycle_metadata.get("previous_desk_context", "")
        if previous_desk_context:
            dynamic_sections.append((
                4,
                f"## Previous Cycle's SharedDesk (Manila Envelope)\n{previous_desk_context}",
            ))

        if desk_context and desk_context != "No artifacts on desk yet.":
            dynamic_sections.append((_KEEP, f"## SharedDesk Context Summary\n{desk_context}"))

        # Current whiteboard summary (changes per agent within a cycle)
        try:
            from app.agents.whiteboard import whiteboard
            wb_summary = await whiteboard.summarize(ticker=desk.ticker, cycle_id=cycle_id)
            if wb_summary:
                dynamic_sections.append((6, wb_summary))
        except Exception as wb_err:
            logger.warning("[V3Runner] Failed to fetch whiteboard summary: %s", wb_err)

        # Tool playbook: the eval layer grades every trace into tool-success
        # stats, but tool_playbook had ZERO readers — all that compute landed
        # in a write-only table. Surface this agent's proven tools (compact).
        try:
            playbook_tips = _get_tool_playbook_tips(agent_name)
            if playbook_tips:
                dynamic_sections.append((
                    7,
                    "## Tool Playbook (your historically highest-scoring tools)\n" + playbook_tips,
                ))
        except Exception as pb_err:
            logger.debug("[V3Runner] Tool playbook lookup skipped: %s", pb_err)

        dynamic_block = "\n\n".join(text for _, text in dynamic_sections)

        # ── Assemble user prompt ──
        user_prompt = (
            f"## Ticker: {desk.ticker}\n"
            f"## Cycle: {cycle_id}\n\n"
        )

        # Peer-request text rides in the USER message and cannot be rerouted
        # to the system prompt like dynamic_block — cap it, or a long peer
        # query alone can blow Prism's 2048-token memory-embed limit.
        if custom_instructions and len(custom_instructions) > 3000:
            custom_instructions = custom_instructions[:3000] + " …[truncated]"

        # Prism's server-side agent memory embeds the USER message with
        # embeddinggemma, which has a hard 2048-token positional limit — a
        # larger user message fails with a "memory:embed ... maximum context
        # length is 2048 tokens" error that can starve the desk of this agent's
        # artifact. Prism does NOT embed the system prompt (see base_agent.py),
        # so when the KV-cache-friendly user-message layout would overflow the
        # embedder, ride the dynamic block in the SYSTEM prompt instead. Common
        # (small) prompts still get prefix-cache reuse; only oversized ones fall
        # back. ~4 chars/token, with headroom below 2048 to absorb tokenizer
        # density differences on numeric/ticker-heavy text.
        _EMBED_TOKEN_LIMIT = 2048
        _USER_SCAFFOLD_CHARS = 1900  # tool/output directives + reminder appended below
        # custom_instructions (peer-request text) is appended to the user
        # prompt AFTER this guard runs — it must be counted in _fixed_chars or a
        # long peer query can push the real message past the embed limit.
        # Divisor 3, not 4: embeddinggemma splits digits ~1 char/token, so
        # quant-heavy text lands at ~2.5-3 chars/token — a 6.5k-char block
        # that passed the //4 gate could weigh 2,200+ real tokens and blow
        # the 2048 embed limit anyway.
        _EMBED_CHAR_BUDGET = (_EMBED_TOKEN_LIMIT - 400) * 3
        _fixed_chars = (
            len(user_prompt) + len(custom_instructions or "") + _USER_SCAFFOLD_CHARS
        )

        def _fits(block: str) -> bool:
            return (_fixed_chars + len(block)) < _EMBED_CHAR_BUDGET

        # Shed lowest-priority sections until the block fits the embedder rather
        # than relocating it to the system prompt. Relocation kept every token in
        # the payload (the model saw all of it) and broke prefix-cache reuse; the
        # only thing it avoided was Prism's embed error.
        shed: list[str] = []
        if prompt_split and dynamic_block and not _fits(dynamic_block):
            kept = list(dynamic_sections)
            while kept and not _fits("\n\n".join(t for _, t in kept)):
                sheddable = [s for s in kept if s[0] != _KEEP]
                if not sheddable:
                    break
                victim = max(sheddable, key=lambda s: s[0])
                kept.remove(victim)
                shed.append(victim[1].split("\n", 1)[0].lstrip("# ").strip() or "unnamed")
            dynamic_block = "\n\n".join(t for _, t in kept)

        _fits_embedder = _fits(dynamic_block)

        if shed:
            logger.info(
                "[V3Runner] %s: shed %d dynamic section(s) to fit Prism's %d-token "
                "memory embedder: %s",
                agent_name, len(shed), _EMBED_TOKEN_LIMIT, ", ".join(shed),
            )

        if prompt_split and dynamic_block and _fits_embedder:
            user_prompt += dynamic_block + "\n\n"
        elif dynamic_block:
            # Either V3_PROMPT_SPLIT is off (legacy layout), or the non-sheddable
            # core alone still overflows the embedder. The system prompt is the
            # only place left that Prism does not embed.
            system_prompt += "\n\n" + dynamic_block
            if prompt_split and not _fits_embedder:
                logger.warning(
                    "[V3Runner] %s: non-sheddable context (~%d tok) still exceeds "
                    "Prism's %d-token memory embedder after shedding %d section(s) "
                    "— routing to system prompt (KV-cache reuse skipped).",
                    agent_name, (_fixed_chars + len(dynamic_block)) // 3,
                    _EMBED_TOKEN_LIMIT, len(shed),
                )

        if tool_whitelist:
            user_prompt += (
                "You have access to a specific subset of tools for your domain. "
                "Use them only if you need deeper research beyond the pre-collected data. "
                "Do not redundantly fetch data already provided.\n\n"
            )
        else:
            user_prompt += (
                "You have NO external tools. Reason from the SharedDesk data.\n\n"
            )

        user_prompt += (
            "## OUTPUT DIRECTIVE REMINDER\n"
            f"When you generate your final response containing your analysis report (i.e. when you do NOT call any tools), "
            f"you MUST output ONLY a valid JSON object matching the `{artifact_type}` schema.\n"
            f"Do NOT include any conversational intro/outro, preambles, summary comments, or markdown headings.\n"
            f"Do NOT wrap the JSON response in markdown code blocks (do NOT use ```json).\n"
            f"Your entire response MUST start with '{{' and end with '}}'.\n"
            f"You MAY include an optional \"tags\" array of short hashtag labels "
            f"(e.g. [\"#catalyst\", \"#earnings_risk\", \"#verify_later\"]) to flag "
            f"data points for other agents and future cycles.\n\n"
        )

        # Append custom peer instructions if requested
        if custom_instructions:
            user_prompt += (
                f"\n## Peer Request / Instructions\n"
                f"A peer agent requested your specific analysis:\n"
                f"\"{custom_instructions}\"\n\n"
                f"Address this request directly in your findings.\n\n"
            )

        user_prompt += "Begin your analysis now.\n"

        # Context budget report (plan 4.5): prompt sizes ride with telemetry
        sys_prompt_chars = len(system_prompt)
        user_prompt_chars = len(user_prompt)

        # Call via base_agent.run_agent() which handles:
        # - Dynamic prompt generation
        # - Harness routing (Local/Prism)
        # - Real message & tool execution flow
        from app.agents.base_agent import run_agent


        model_override = getattr(agent_module, "MODEL_OVERRIDE", None)

        prism_overrides = desk.cycle_metadata.get("prism_overrides", {})

        # Reserve output space from what the assembled payload actually leaves,
        # instead of asking for a flat 8192 regardless of input size. The tool
        # schemas are counted too — they are the largest fixed cost on a
        # tool-enabled agent and were invisible to the old constant.
        safe_max_tokens = _safe_max_tokens(
            agent_name=agent_name,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_whitelist=tool_whitelist,
        )

        result = await asyncio.wait_for(
            run_agent(
                agent_name=agent_name,
                ticker=desk.ticker,
                cycle_id=cycle_id,
                bot_id=bot_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=safe_max_tokens,
                enable_tools=bool(tool_whitelist),
                model_override=model_override,
                prism_overrides=prism_overrides,
            ),
            timeout=timeout_seconds,
        )

        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        final_text = result.get("response", "")
        loops_used = result.get("loops_used", 1)
        token_usage = result.get("tokens_used", 0)
        stop_reason = result.get("stop_reason", "completed")

        # Budget exhaustion — the harness hit max_iterations without a final
        # answer, so the "response" is a sentinel, not an artifact. (The old
        # max_tokens/length check here was dead: run_agent never set those.)
        if stop_reason == "max_iterations":
            logger.warning(
                "[V3Runner] %s exhausted its turn budget for %s — "
                "artifact parsing will fail. Consider raising its "
                "AGENT_BUDGET_OVERRIDES entry.",
                agent_name, desk.ticker,
            )

        # Parse the artifact from the agent's output
        artifact = _parse_artifact(final_text, artifact_type, agent_name)

        # Salvage pass. A tool-enabled agent that reaches its iteration ceiling
        # is told by the harness to "summarize", and models frequently answer
        # with one more *pseudo* tool call in plain text (e.g.
        # `call:mcp__lazy-tool-service__get_sec_filings{ticker:WFC}`) instead of
        # the JSON artifact. Nothing executes it, so the literal string becomes
        # the final answer and parsing fails — burning the whole run's research.
        # One tool-less retry that shows the model its own output and asks only
        # for the JSON recovers it, so re-running every agent from scratch (or
        # tripping the breaker) is not the first resort.
        if artifact is None and final_text and bool(tool_whitelist):
            logger.warning(
                "[V3Runner] %s: unparseable output for %s (%d chars) — "
                "attempting tool-less artifact repair",
                agent_name, desk.ticker, len(final_text),
            )
            try:
                repair_prompt = (
                    f"{user_prompt}\n\n"
                    f"## PREVIOUS ATTEMPT (UNPARSEABLE)\n"
                    f"Your previous reply could not be parsed as the "
                    f"required artifact:\n\n{final_text[:2000]}\n\n"
                    f"Do NOT call any tools — you have none available "
                    f"now. Using the analysis you already performed, "
                    f"reply with ONLY the '{artifact_type}' JSON "
                    f"object. Start with '{{' and end with '}}'. "
                    f"No markdown fences, no commentary.\n"
                )
                # Measured separately: this prompt carries the failed attempt
                # back in (so it is larger), but runs tool-less (so the schemas
                # are gone). Reusing the first call's budget would be wrong twice.
                repair_result = await asyncio.wait_for(
                    run_agent(
                        agent_name=agent_name,
                        ticker=desk.ticker,
                        cycle_id=cycle_id,
                        bot_id=bot_id,
                        system_prompt=system_prompt,
                        user_prompt=repair_prompt,
                        max_tokens=_safe_max_tokens(
                            agent_name=agent_name,
                            system_prompt=system_prompt,
                            user_prompt=repair_prompt,
                            tool_whitelist=None,
                        ),
                        enable_tools=False,
                        model_override=model_override,
                        prism_overrides=prism_overrides,
                    ),
                    timeout=timeout_seconds,
                )
                repair_text = repair_result.get("response", "")
                artifact = _parse_artifact(repair_text, artifact_type, agent_name)
                if artifact is not None:
                    logger.info(
                        "[V3Runner] %s: artifact repair succeeded for %s",
                        agent_name, desk.ticker,
                    )
                    elapsed_ms = int((time.monotonic() - t_start) * 1000)
                    token_usage += repair_result.get("tokens_used", 0)
            except Exception as e:
                logger.warning(
                    "[V3Runner] %s: artifact repair failed for %s: %s: %s",
                    agent_name, desk.ticker, type(e).__name__, e,
                )

        if artifact is None:
            logger.error(
                "[V3Runner] %s produced no parseable artifact for %s",
                agent_name, desk.ticker,
            )
            emit(
                "analyzing",
                f"v3_{agent_name}_fail_{desk.ticker}",
                f"❌ {desk.ticker}: V3 {agent_name} — no valid artifact produced",
                status="error",
            )
            _record_telemetry(desk, agent_name, elapsed_ms, loops_used, token_usage, "AGENT_ERROR",
                              sys_prompt_chars=sys_prompt_chars, user_prompt_chars=user_prompt_chars)
            return PhaseOutcome.AGENT_ERROR

        # Validate the artifact
        errors = validate_artifact(artifact_type, artifact)
        if errors:
            missing_required = [e for e in errors if e.startswith("Missing required field")]
            if missing_required and artifact_type in ("final_decision", "trade_decision"):
                # A decision artifact without action/confidence/reasoning is a
                # failed run, not a salvageable one: appending it and returning
                # SUCCESS silently drops the board/synthesizer vote (the
                # synthesizer then zeroes the board's signal weight), while
                # AGENT_ERROR engages the circuit breaker's existing retry.
                logger.error(
                    "[V3Runner] %s decision artifact for %s is missing required fields %s — "
                    "treating as AGENT_ERROR so the circuit breaker can retry",
                    agent_name, desk.ticker, missing_required,
                )
                emit(
                    "analyzing",
                    f"v3_{agent_name}_fail_{desk.ticker}",
                    f"❌ {desk.ticker}: V3 {agent_name} — decision artifact missing required fields",
                    status="error",
                )
                _record_telemetry(desk, agent_name, elapsed_ms, loops_used, token_usage, "AGENT_ERROR",
                                  sys_prompt_chars=sys_prompt_chars, user_prompt_chars=user_prompt_chars)
                return PhaseOutcome.AGENT_ERROR
            logger.warning(
                "[V3Runner] %s artifact validation warnings for %s: %s",
                agent_name, desk.ticker, errors,
            )
            # Non-fatal for analyst artifacts — we still append, but log the issues
            artifact["_validation_warnings"] = errors

        # Append to SharedDesk
        desk.append_artifact(artifact_type, artifact)

        # Quality scoring — detect dead ends / weak artifacts
        quality_result = score_artifact(artifact_type, artifact)
        quality_score = quality_result.get("quality_score", -1)
        quality_flag = quality_result.get("flag", "unknown")
        failure_patterns = quality_result.get("failure_patterns", [])

        if quality_flag == "dead_end":
            logger.warning(
                "[V3Runner] %s produced DEAD END artifact for %s "
                "(quality=%d, patterns=%s)",
                agent_name, desk.ticker, quality_score, failure_patterns,
            )
        elif quality_flag == "weak":
            logger.info(
                "[V3Runner] %s produced WEAK artifact for %s (quality=%d)",
                agent_name, desk.ticker, quality_score,
            )

        # Store quality info on the artifact itself for downstream visibility
        artifact["_quality_score"] = quality_score
        artifact["_quality_flag"] = quality_flag
        if failure_patterns:
            artifact["_failure_patterns"] = failure_patterns

        # Log success
        direction = artifact.get("thesis_direction", artifact.get("action", "?"))
        confidence = artifact.get("confidence", artifact.get("final_confidence", 0))

        quality_emoji = "🟢" if quality_flag == "good" else "🟡" if quality_flag == "weak" else "🔴"

        emit(
            "analyzing",
            f"v3_{agent_name}_done_{desk.ticker}",
            f"✅ {desk.ticker}: V3 {agent_name} → {direction} @ {confidence}% "
            f"({loops_used} turns, {elapsed_ms}ms) {quality_emoji} Q:{quality_score}",
            status="ok",
            data={
                "kind": "agent_done",
                "agent": agent_name,
                "ticker": desk.ticker,
                "target": parent_agent,
                # The office speaks this as the agent's TTS line and shows it in
                # the speech bubble; trimmed so a long report isn't read aloud.
                # Analysts use `summary`; the board/synthesizer use `reasoning`
                # and the regime engine `rationale` — fall through so the
                # decision-makers actually say something instead of a fallback.
                "summary": (
                    artifact.get("summary")
                    or artifact.get("reasoning")
                    or artifact.get("rationale")
                    or ""
                )[:240],
                "direction": direction,
                "confidence": confidence,
                "elapsed_ms": elapsed_ms,
                "loops_used": loops_used,
                "tool_calls_made": max(0, loops_used - 1),
                "quality_score": quality_score,
                "quality_flag": quality_flag,
            },
        )

        try:
            artifact_size_bytes = len(json.dumps(artifact, default=str))
        except Exception:
            artifact_size_bytes = 0
        _record_telemetry(desk, agent_name, elapsed_ms, loops_used, token_usage, "SUCCESS", quality_score,
                          sys_prompt_chars=sys_prompt_chars, user_prompt_chars=user_prompt_chars,
                          artifact_size_bytes=artifact_size_bytes)

        # Classify outcome
        data_gaps = artifact.get("data_gaps", [])
        if data_gaps and len(data_gaps) > 2:
            return PhaseOutcome.DATA_GAP
        return PhaseOutcome.SUCCESS

    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        logger.error(
            "[V3Runner] %s TIMEOUT for %s after %dms",
            agent_name, desk.ticker, elapsed_ms,
        )
        emit(
            "analyzing",
            f"v3_{agent_name}_timeout_{desk.ticker}",
            f"⏰ {desk.ticker}: V3 {agent_name} TIMEOUT after {elapsed_ms}ms",
            status="error",
        )
        _record_telemetry(desk, agent_name, elapsed_ms, 0, 0, "TIMED_OUT",
                          sys_prompt_chars=sys_prompt_chars, user_prompt_chars=user_prompt_chars)
        return PhaseOutcome.TIMED_OUT

    except asyncio.CancelledError:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        logger.info(
            "[V3Runner] %s CANCELLED for %s after %dms — stop requested",
            agent_name, desk.ticker, elapsed_ms,
        )
        emit(
            "analyzing",
            f"v3_{agent_name}_cancelled_{desk.ticker}",
            f"🛑 {desk.ticker}: V3 {agent_name} CANCELLED after {elapsed_ms}ms",
            status="error",
        )
        _record_telemetry(desk, agent_name, elapsed_ms, 0, 0, "CANCELLED")
        raise  # Re-raise so orchestrator and pipeline_service see the cancellation

    except Exception as e:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        logger.error(
            "[V3Runner] %s CRASHED for %s: %s",
            agent_name, desk.ticker, e,
        )
        emit(
            "analyzing",
            f"v3_{agent_name}_crash_{desk.ticker}",
            f"💥 {desk.ticker}: V3 {agent_name} CRASHED — {str(e)[:100]}",
            status="error",
        )
        _record_telemetry(desk, agent_name, elapsed_ms, 0, 0, "AGENT_ERROR")
        return PhaseOutcome.AGENT_ERROR

    finally:
        exit_v3_session(session_key)


def _parse_artifact(
    text: str, artifact_type: str, agent_name: str
) -> dict | None:
    """Parse the agent's text output into an artifact dict.

    Tries multiple strategies:
    1. Direct JSON parse
    2. Extract JSON from markdown code blocks
    3. Extract JSON from anywhere in the text

    Returns None if no valid JSON is found.
    """
    if not text or not text.strip():
        return None

    # The Board (and any persona prompt using scratchpad XML) emits a
    # <thought_process> block before its JSON. Strip it first: if the block
    # itself contains braces, the first-{/last-} extraction below would grab
    # an invalid span and needlessly degrade to the lossiest parse strategy.
    if "</thought_process>" in text:
        text = text.rsplit("</thought_process>", 1)[-1]

    # Delegate to the shared util — it already covers what the old 4-strategy
    # ladder did here (direct parse, fenced blocks, balanced-brace scan) plus
    # placeholder filtering and the malformed-text fallback.
    try:
        from app.utils.text_utils import parse_json_response
        parsed = parse_json_response(text)
        if isinstance(parsed, dict) and parsed:
            return parsed
    except Exception:
        pass

    logger.warning(
        "[V3Runner] Failed to parse artifact from %s output (%d chars)",
        agent_name, len(text),
    )
    return None


def _record_telemetry(
    desk: SharedDesk,
    agent_name: str,
    elapsed_ms: int,
    loops_used: int,
    token_usage: int,
    outcome: str,
    quality_score: int = -1,
    sys_prompt_chars: int = 0,
    user_prompt_chars: int = 0,
    artifact_size_bytes: int = 0,
) -> None:
    """Record telemetry for a V3 agent run."""
    entry = {
        "agent_name": agent_name,
        "ticker": desk.ticker,
        "elapsed_ms": elapsed_ms,
        "loops_used": loops_used,
        "token_usage": token_usage,
        "outcome": outcome,
        "phase": desk.phase.value,
        "quality_score": quality_score,
        "artifact_size_bytes": artifact_size_bytes,
        # Context budget report: per-agent prompt footprint (chars). The DB
        # insert ignores extra keys; these surface in logs/v3_metadata.
        "sys_prompt_chars": sys_prompt_chars,
        "user_prompt_chars": user_prompt_chars,
    }
    desk.record_agent_telemetry(entry)
