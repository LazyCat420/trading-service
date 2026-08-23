"""
Base agent pattern — every agent follows this exact structure.

Phase 2: Agents receive pre-computed data from processors.
Phase 3: Optional dynamic meta-prompt generates context-aware system prompts.
LLM only analyzes — never calculates.
"""

import datetime
import logging

from app.config import settings

from app.utils.text_utils import parse_json_response, sanitize_ascii
from app.utils.resilience import aresilient_call
from app.db import mongo_query

logger = logging.getLogger(__name__)

_active_agents = set()


def _base_agent_accepts_min_p() -> bool:
    """Does the INSTALLED lazycat SDK take `min_p` on BaseAgent?

    deploy.sh syncs lazycat-sdk alongside app/, so these move together in a
    normal deploy — but a partial one would make every BaseAgent construction
    raise TypeError, i.e. turn a sampling fix into a total agent outage.
    Checked once at import; see the min_p block in run_agent for the why.
    """
    try:
        import inspect

        from lazycat.agent import BaseAgent as _BA

        return "min_p" in inspect.signature(_BA.__init__).parameters
    except Exception:  # noqa: BLE001 — never let a capability probe stop boot
        return False


_BASE_AGENT_ACCEPTS_MIN_P = _base_agent_accepts_min_p()

#: Model-name markers for providers that never had the speculative-decoding
#: problem. Matched on the MODEL, not the provider, because prism routes on the
#: model name and `provider` is "vllm" by default even for an overridden model.
_CLOUD_MODEL_MARKERS = ("gpt-", "claude-", "gemini-")


def min_p_for(provider: str | None, model: str | None) -> float | None:
    """`0.0` for the local vLLM boxes, `None` (gateway default) otherwise.

    WHY THIS EXISTS. Prism's ParameterRegistry gives `minP` an agentDefault of
    0.05 and injects it into every /agent call, because we never sent the
    field. vLLM with speculative decoding REFUSES any min_p > 0:

        ValueError: The min_p and logit_bias sampling parameters are not yet
        supported with speculative decoding

    and raises it INSIDE the stream generator, after already answering HTTP
    200 — so prism sees an empty stream rather than an error and reports a
    successful call with no content. That is what the gatekeeper's "empty
    response from v3_portfolio_manager" was (GATEKEEPER_DEGRADED x4 on
    2026-08-06, degrading ticker selection to the raw scoring engine).

    Measured that day against the Jetson, interleaved, same prompt, one
    variable changed: prism default 0/3 non-empty; min_p=0.0 3/3 non-empty and
    3/3 a valid artifact. 0.0 is vLLM's OWN default, so this restores standard
    sampling rather than tuning it.

    Fail-safe by omission: an unknown provider gets None and keeps today's
    behaviour, so a new endpoint cannot silently inherit a sampling override.
    """
    if any(marker in (model or "").lower() for marker in _CLOUD_MODEL_MARKERS):
        return None
    # `provider` is None on the model_override path, where BaseAgent falls back
    # to "vllm" itself — so the fallback here must match BaseAgent's, or that
    # path keeps the broken default.
    return 0.0 if (provider or "vllm").startswith("vllm") else None


def transport_for(enable_tools: bool, agent_tools: list | None) -> str:
    """`"agent"` or `"chat"` — the agent's own tool declaration picks the route.

    WHY DERIVE IT. The transport used to be hardcoded at each call site, which
    let the declaration and the route disagree silently: the gatekeeper's
    system prompt instructs it to call `get_parameters`, its TOOL_WHITELIST
    carries 14 tools, and its call site passed `enable_tools=False`. Nothing
    could reconcile those, because nothing read both.

    THE RULE. Tools declared -> `/agent` (prism attaches the catalog and runs
    the agentic loop server-side). No tools -> `/chat`, where tool attachment
    is opt-in and we opt out. Prism's `/chat` is NOT a tool-less endpoint —
    `ChatRoutes` honours `functionCallingEnabled`/`enabledTools` and executes
    calls via ToolOrchestratorService — so this is a choice about who decides,
    not about what is possible.

    MEASURED 2026-08-06, n=10 interleaved, replayed `v3_regime_engine` prompts
    (a role whose tools show zero calls in 60 days):

        /chat          10/10 non-empty  10/10 valid  16.2s median  2.9s ttft
        /agent         10/10             8/10        68.1s         8.4s
        /agent+tools    9/10             8/10        45.4s         8.8s

    The catalog costs ~5.5s before the first token even when no tool is ever
    called, and both /agent arms lost runs to the model narrating instead of
    emitting its artifact. That is evidence about a TOOL-LESS job only; it says
    nothing about a role that genuinely calls something, which is why the rule
    keys on the declaration rather than preferring one endpoint outright.

    Fail-safe direction: when in doubt, `/agent`. A tool-using agent routed to
    `/chat` silently loses its tools (the failure the gatekeeper is living
    proof of); a tool-less agent routed to `/agent` is merely slower.
    """
    if not enable_tools:
        return "chat"
    # enable_tools=True with an EMPTY whitelist is not "all tools" — it is a
    # role with nothing to call. Sending it to /agent would attach the catalog
    # for no reason. (`agent_runner` already computes enable_tools from
    # bool(tool_whitelist), so this is belt-and-braces for direct callers.)
    return "agent" if agent_tools else "chat"

#: Bounds on the tool transcript handed to the artifact repair pass. The whole
#: point is to give the repair the agent's own findings, but it rides in a
#: prompt that is already ~27k chars, so the ceiling is deliberate and low:
#: ~12 calls x 1,200 chars ≈ 14k chars worst case, and the repair runs
#: TOOL-LESS so the schemas that dominate the first call's payload are gone.
_TRANSCRIPT_MAX_ENTRIES = 12
_TRANSCRIPT_ENTRY_CHARS = 1200

# The pseudo-tool-call shape ("a final answer that is really an unexecuted tool
# call the model wrote as prose") moved to `app/v3/output_rules.py` on
# 2026-08-09, where it sits beside the other failure shapes and is read by both
# the stop_reason derivation below and the repair pass in agent_runner. Two
# copies of that regex is the drift defect: the runner would repair a class the
# tripwire had already declined to name.

# ─── Meta-prompt: generates a context-aware system prompt ───────────
AGENT_META_SYSTEM = """You are an expert at creating specialized analyst system prompts for stock market analysis.

Given an agent's role description and a preview of the market data, create an IMPROVED system prompt tailored to THIS specific analysis.

STRICT GUARDRAILS — you MUST follow these:
1. PRESERVE the exact JSON output schema from the original prompt (same keys, same value types)
2. The generated prompt must ONLY instruct the agent to analyze the data it receives — never tell it to fetch, search, or hallucinate data
3. Keep the prompt under 200 words — concise prompts produce better LLM output
4. Include "Respond with JSON:" followed by the exact schema from the original prompt
5. NEVER remove the instruction "do NOT recalculate" or "the data given is authoritative"

WHAT TO ADAPT based on the data preview:
- Identify the asset class: blue-chip stock, growth stock, penny stock, crypto, commodity, ETF
- For PENNY STOCKS (price < $5): emphasize liquidity risk, dilution risk, and pump-and-dump patterns
- For CRYPTO (BTC/ETH/XRP): skip P/E and fundamentals, focus on momentum and sentiment cycles
- For BLUE CHIPS: emphasize macro sensitivity, dividend sustainability, institutional positioning
- Reference specific data patterns you see (e.g., "RSI is oversold" or "revenue declining")
- Name the sector/industry if identifiable from the ticker or data

Respond with ONLY JSON:
{"system_prompt": "the full improved system prompt with JSON schema preserved", "focus_rationale": "1 sentence on what you adapted and why"}"""

AGENT_META_USER = """## Agent Role: {agent_name}

## Original System Prompt (template — preserve its JSON output schema exactly):
{static_prompt}

## Data Preview (first 8000 chars of what the agent will analyze):
{data_preview}

---

Create a better, more specific system prompt for this agent. You MUST preserve the exact JSON output schema from the original prompt. Adapt the analytical focus to what matters most for this specific ticker and data."""


# _parse_json_response moved to app.utils.text_utils.parse_json_response
_parse_json_response = parse_json_response

# ── Agents that receive prior trade outcome context ──
_OUTCOME_CONTEXT_AGENTS = frozenset({
    "sentiment", "technical", "fundamental", "risk", "fund_flow",
    "comparative", "retriever",
})


def get_ticker_outcome_context(ticker: str) -> str:
    """Pull resolved trade outcomes for this ticker from the DB.

    Returns a formatted string for analyst prompt injection, or empty string
    if no history exists. Queries Mongo (`decision_outcomes`) — deterministic,
    bounded, no flat-file I/O. A failed read logs a warning: 31% of desks
    carry no prior-trade lines (audit 2026-08-23), and a silent "" here is
    indistinguishable from "no history".
    """
    if not ticker or ticker.startswith("_"):
        return ""  # Skip synthetic tickers like _AUDIT_
    try:
        from app.db import mongo_query

        rows = mongo_query.find_rows(
            'decision_outcomes',
            {'ticker': ticker, 'outcome': {'$in': ['WIN', 'LOSS', 'FLAT', 'HOLD_CORRECT', 'HOLD_AVOIDED_DECLINE', 'HOLD_MISS']}},
            ['outcome', 'entry_price', 'exit_price', 'pnl_pct', 'confidence', 'resolved_at'],
            sort=[('resolved_at', -1)],
            limit=5
        )
        outcomes = [
            {
                "outcome": r[0],
                "entry_price": r[1] or 0,
                "exit_price": r[2] or 0,
                "pnl_pct": r[3] or 0,
                "confidence": r[4] or 0,
                "resolved_at": r[5],
            }
            for r in rows
        ]
        if not outcomes:
            return ""

        lines = [f"\n## PRIOR TRADE HISTORY FOR {ticker}"]
        for o in outcomes:
            lines.append(
                f"- {o['outcome']}: entry=${o.get('entry_price', 0):.2f} → "
                f"exit=${o.get('exit_price', 0):.2f} ({o.get('pnl_pct', 0):+.1f}%) "
                f"conf={o.get('confidence', 0)} [{o.get('resolved_at', '?')}]"
            )
        lines.append(
            "Use this history to calibrate your confidence — "
            "do not repeat past mistakes.\n"
        )
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — history is optional, but say so
        logger.warning(
            "[OutcomeContext] prior-trade history read failed for %s: %s: %s "
            "— prompt will carry no history for this ticker",
            ticker, type(exc).__name__, exc,
        )
        return ""


# Fleet-level, not per-ticker, so one cached copy serves every agent call.
_CALIBRATION_CTX_TTL_S = 1800
_calibration_ctx_cache = {"expires": 0.0, "text": ""}


def get_confidence_calibration_context() -> str:
    """Empirical win rate per stated-confidence bucket (90d, resolved, ex-flat).

    Stated confidence has historically clustered at 70-85 regardless of
    evidence strength. Showing agents the realized accuracy of each bucket is
    the feedback loop that makes the number mean something.
    """
    import time

    now = time.monotonic()
    if now < _calibration_ctx_cache["expires"]:
        return _calibration_ctx_cache["text"]

    text = ""
    try:
        from app.db import mongo_store
        import datetime

        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=90)
        docs = mongo_store.find_docs(
            'decision_outcomes',
            {
                'resolved_at': {'$ne': None, '$gte': cutoff},
                'outcome': {'$in': ['WIN', 'LOSS']},
                'confidence': {'$gte': 40},
            }
        )
        if docs:
            buckets = {}
            for d in docs:
                conf = d.get('confidence')
                if conf is None:
                    continue
                try:
                    conf = float(conf)
                except Exception:
                    continue
                bucket = int(conf // 10) * 10
                if bucket not in buckets:
                    buckets[bucket] = {"n": 0, "wins": 0}
                buckets[bucket]["n"] += 1
                if d.get("outcome") == "WIN":
                    buckets[bucket]["wins"] += 1

            valid_buckets = [(b, stats["n"], stats["wins"]) for b, stats in sorted(buckets.items()) if stats["n"] >= 10]
            if valid_buckets:
                lines = [
                    "## CONFIDENCE CALIBRATION (fleet track record, last 90 days)",
                    "Realized win rate of resolved trades at each stated confidence level:",
                ]
                for bucket, n, wins in valid_buckets:
                    lines.append(
                        f"- stated {bucket}-{bucket + 9}%: won {wins / n:.0%} of {n} trades"
                    )
                lines.append(
                    "State the confidence the evidence actually supports. If your number "
                    "lands in a bucket that wins less than it claims, you are overconfident — "
                    "mixed or conflicting evidence belongs at 40-60, not 70-85.\n"
                )
                text = "\n".join(lines)
    except Exception:
        text = ""

    _calibration_ctx_cache["expires"] = now + _CALIBRATION_CTX_TTL_S
    _calibration_ctx_cache["text"] = text
    return text





async def run_agent(
    agent_name: str,
    ticker: str,
    cycle_id: str,
    bot_id: str,
    system_prompt: str,
    user_prompt: str,
    data_context: str = "",
    temperature: float = 0.3,
    # 8192 matches the SDK default that was silently in effect for every
    # caller before these params were actually honored — 1024 would truncate
    # callers that never passed a value (e.g. the gatekeeper).
    max_tokens: int = 8192,
    endpoint_override: str | None = None,
    enable_tools: bool = False,
    response_format: dict | None = None,
    parent_conversation_id: str | None = None,
    parent_agent_session_id: str | None = None,
    model_override: str | None = None,
    prism_overrides: dict | None = None,
) -> dict:
    """
    Generic agent runner:
    1. Optionally generate a dynamic system prompt via meta-prompt
    2. Inject data_context (pre-computed signals) into user prompt
    3. Call llm.chat() with monitoring metadata
    4. Return structured result dict

    Every specific agent builds its own prompts and calls this.
    """
    # ── V3 relies on specialized static prompts and no DB queries ──

    # ── Inject prior trade outcome context for analysis agents ──
    outcome_ctx = ""
    if agent_name in _OUTCOME_CONTEXT_AGENTS:
        outcome_ctx = get_ticker_outcome_context(ticker)

    # ── Budget-aware data truncation ──
    # Prevent any single component from blowing the context window
    from app.config.context_budget import get_context_budget

    ctx_budget = get_context_budget()

    if data_context and len(data_context) > ctx_budget.data_context_chars:
        original_len = len(data_context)
        data_context = data_context[: ctx_budget.data_context_chars]
        logger.info(
            "[BaseAgent] %s data_context truncated: %d -> %d chars (budget=%d)",
            agent_name,
            original_len,
            len(data_context),
            ctx_budget.data_context_chars,
        )

    # Inject pre-computed data into the SYSTEM prompt to prevent Prism from embedding it.
    # Prism's workflow-query embeds user messages; if data_context > 2048 tokens, the embedding model crashes.
    if data_context or outcome_ctx:
        system_prompt = f"{system_prompt}\n\n[PRE-COLLECTED DATA CONTEXT]\n{outcome_ctx}{data_context}"
    full_prompt = user_prompt
        


    # ── Verbose input logging (structured, truncated to prevent log spam) ──
    _sys_preview = sanitize_ascii(system_prompt[:500])
    _user_preview = sanitize_ascii(full_prompt[:1000])
    logger.debug(
        "[BaseAgent] INPUT %s (%s) | sys_prompt=%d chars | user_prompt=%d chars\n"
        "  System: %s%s\n  User: %s%s",
        agent_name, ticker, len(system_prompt), len(full_prompt),
        _sys_preview, "..." if len(system_prompt) > 500 else "",
        _user_preview, "..." if len(full_prompt) > 1000 else "",
    )

    # Once a model resolution attempt fails (or a resolved model produced a
    # harness error), every retry re-resolves with force_refresh=True so the
    # 5-minute model cache cannot re-serve the identity that just failed.
    _resolution_state = {"force_refresh": False}

    # What the agent actually LEARNED, kept so a failed artifact can be repaired
    # from its own research (2026-08-05). The common artifact failure is an
    # agent that narrates its next step and runs out of turns — "I'll complete
    # the analysis and emit the desk_note JSON" — leaving the harness to return
    # the announcement. The repair pass previously saw only that announcement,
    # so it was asked to write a report from material containing none. Declared
    # here, OUTSIDE the retry wrapper, so it is still readable at the return.
    tool_transcript: list[dict] = []

    # Delays 5s/10s/20s/40s (~75s total) so agent calls survive a lazy-tool
    # (prism-proxy) container redeploy instead of failing the whole pipeline.
    @aresilient_call(retries=5, backoff="exponential", base_delay=5.0, max_delay=60.0)
    async def _agent_llm_call():
        from app.agents.tool_whitelists import get_agent_tools, get_agent_budget_turns

        # Extract overrides from Settings panel
        overrides = prism_overrides or {}
        domain_blocklist = overrides.get("tool_domain_blocklist", [])

        # Per-agent tool whitelist: only show tools relevant to this agent's role
        agent_tools = get_agent_tools(agent_name, domain_blocklist=domain_blocklist) if enable_tools else []

        # Per-agent turn budget: reasoning-only agents get 1, tool agents get role-specific limits
        max_turns = get_agent_budget_turns(agent_name, enable_tools)

        # Agent loop using lazycat-sdk
        from lazycat.agent import BaseAgent, AgentHarness
        from lazycat.session import ConversationSession
        import time
        from lazycat.llm import prism_client

        # NOTE: prism_client.url is set ONCE per cycle in PipelineService._run_all_v3()
        # to prevent a race condition where concurrent agent calls stomp on the global
        # singleton URL. Do NOT override prism_client.url here.


        t0 = time.time()
        tool_call_count = 0
        prior_calls = []
        # Each retry starts a fresh transcript: a repair must be built from the
        # attempt that actually failed, not from a discarded earlier one.
        tool_transcript.clear()
        # Late-bound model identity for the tool-result hook: the hook closure
        # is built before the model is resolved, so it reads this holder at
        # call time. _agent_llm_call fills it right after resolution.
        _model_holder = {"model": None, "provider": None}

        def _on_tool_result(tool_name: str, arguments: dict, result, was_blocked: bool, elapsed_ms: int = 0) -> None:
            """Post-call hook: record the actual outcome to V3 telemetry."""
            nonlocal tool_call_count
            # Prism-internal stream events sometimes carry no tool name and a
            # None result (non-tool events misrouted through the hook). They
            # produced hundreds of unattributable tool_name='' failure rows.
            if not tool_name:
                return
            tool_call_count += 1

            # Keep a bounded record of what this call returned, for the repair
            # pass. Capped per entry AND in total: an unbounded transcript
            # would land straight back in a prompt that is already large.
            if len(tool_transcript) < _TRANSCRIPT_MAX_ENTRIES and not was_blocked:
                try:
                    import json as _json_t

                    _payload = (
                        result if isinstance(result, str)
                        else _json_t.dumps(result, default=str)
                    )
                except Exception:  # noqa: BLE001 — a transcript must never break the run
                    _payload = str(result)
                tool_transcript.append({
                    "tool": tool_name,
                    "args": str(arguments)[:200],
                    "result": (_payload or "")[:_TRANSCRIPT_ENTRY_CHARS],
                })

            failed = False
            error_msg = ""
            if was_blocked:
                failed = True
                error_msg = "Blocked by ToolLoopDetector"
            elif isinstance(result, str):
                if result.startswith("Error:") or "Exception" in result:
                    failed = True
                    error_msg = result[:500]
                else:
                    # Most tool failures come back as JSON *strings* —
                    # json.dumps({"error": ...}) from the registry exception
                    # path and json.dumps({"status": "error", ...}) from
                    # whiteboard/agent tools. These used to count as success
                    # (2026-07-15 audit), so telemetry over-reported.
                    stripped = result.lstrip()
                    if stripped.startswith("{"):
                        try:
                            import json as _json
                            parsed = _json.loads(stripped)
                            if isinstance(parsed, dict) and (
                                parsed.get("error")
                                or parsed.get("is_error")
                                or parsed.get("status") == "error"
                            ):
                                failed = True
                                error_msg = str(
                                    parsed.get("error")
                                    or parsed.get("message")
                                    or parsed.get("detail", "")
                                )[:500]
                        except (ValueError, TypeError):
                            pass
            elif isinstance(result, dict):
                if (
                    result.get("error")
                    or result.get("is_error")
                    or result.get("status") == "error"
                ):
                    failed = True
                    error_msg = str(result.get("error", result.get("message", "")))[:500]
                elif not result:
                    failed = True
                    error_msg = "Empty result"
                elif isinstance(result.get("content"), str):
                    # lazy-tool bridge wraps results as {"content": "<json>"} —
                    # unwrap and apply the same string-error check.
                    inner = result["content"].lstrip()
                    if inner.startswith("{"):
                        try:
                            import json as _json
                            parsed = _json.loads(inner)
                            if isinstance(parsed, dict) and (
                                parsed.get("error")
                                or parsed.get("is_error")
                                or parsed.get("status") == "error"
                            ):
                                failed = True
                                error_msg = str(
                                    parsed.get("error")
                                    or parsed.get("message")
                                    or parsed.get("detail", "")
                                )[:500]
                        except (ValueError, TypeError):
                            pass
            elif result is None:
                failed = True
                error_msg = "None result"

            final_tool_name = tool_name
            provider = None
            if tool_name == "lazy_web_search" and not failed:
                try:
                    import json
                    res_dict = result if isinstance(result, dict) else json.loads(result)
                    provider = res_dict.get("provider")
                    if provider:
                        final_tool_name = f"lazy_web_search_{provider.lower()}"
                except Exception:
                    pass

            try:
                from app.v3.tool_telemetry import record_tool_call, _hash_args
                record_tool_call(
                    cycle_id=cycle_id,
                    agent_name=agent_name,
                    tool_name=final_tool_name,
                    args_hash=_hash_args(arguments),
                    success=not failed,
                    was_blocked=was_blocked,
                    error_message=error_msg,
                    elapsed_ms=elapsed_ms,
                    ticker=ticker,
                )

                # Feed the eval layer: agent_traces is what the autoresearch
                # rubric (eval_engine.process_pending_traces) grades. Its old
                # producer (rlm_wrapper) lost its caller in the vllm_client →
                # SDK migration (fa7cee3), so the table starved and eval_scores
                # graded nothing since 2026-06-25.
                from app.autoresearch.trace_writer import write_agent_trace
                write_agent_trace(
                    cycle_id=cycle_id,
                    ticker=ticker,
                    agent_name=agent_name,
                    tool_name=final_tool_name,
                    tool_args=arguments,
                    tool_result=result,
                    failed=failed,
                    latency_ms=elapsed_ms,
                    model_name=_model_holder["model"],
                    endpoint_name=_model_holder["provider"],
                )
                
                if provider:
                    try:
                        from app.telemetry.bus import publish_event
                        from app.telemetry.schema import TelemetryEvent
                        from datetime import datetime, timezone
                        publish_event(TelemetryEvent(
                            ts=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            cycle_id=cycle_id,
                            ticker=ticker,
                            kind="pipeline",
                            source="agent_tool",
                            status="ok",
                            step=f"{provider.lower()}_{ticker}",
                            phase="collecting",
                            detail=f"Web search via {provider}",
                        ))
                    except Exception as ev_err:
                        logger.debug(f"Failed to emit web search telemetry: {ev_err}")
            except Exception as e:
                logger.debug(f"Telemetry failed: {e}")

            # Doom loop check
            current_call = {"name": tool_name, "args": arguments, "error": error_msg,
                            "failed": failed}
            prior_calls.append(current_call)

            # Check 1: Tool loop repetition (same tool + args >= 3 times, FAILED
            # calls only). Successful identical repeats are wasteful but benign
            # — the synthesizer legitimately called think 3x identically on
            # 08-04 — and now that DoomLoopException actually aborts (SDK
            # 0.3.9), counting successes would kill healthy runs.
            same_calls = [c for c in prior_calls
                          if c["name"] == tool_name and c["args"] == arguments
                          and c.get("failed")]
            if len(same_calls) >= 3:
                from app.services.streaming_observer import DoomLoopException
                logger.error(
                    "[ManagerAgent] Caught tool doom loop for %s: repeating %s with %s",
                    agent_name, tool_name, arguments
                )
                raise DoomLoopException(f"Agent {agent_name} caught in tool doom loop calling {tool_name} 3 times.")

            # Check 2: Error loop repetition (same tool + same error >= 3 times)
            if failed and error_msg:
                same_errors = [c for c in prior_calls if c["name"] == tool_name and c["error"] == error_msg]
                if len(same_errors) >= 3:
                    from app.services.streaming_observer import DoomLoopException
                    logger.error(
                        "[ManagerAgent] Caught tool error doom loop for %s on %s: %s",
                        agent_name, tool_name, error_msg
                    )
                    raise DoomLoopException(f"Agent {agent_name} caught in tool error loop for {tool_name}: {error_msg}")

            # Check 3: Active session time — LOG ONLY, deliberately not an
            # abort. While the SDK swallowed hook exceptions this raise was a
            # no-op anyway; now that abort_agent_run propagates (SDK 0.3.9),
            # honoring 180s would kill HEALTHY runs — under big-cycle
            # concurrency on Gold Spark, successful agent runs measured
            # 200-535s on 08-04 (decode-throughput sharing, not stalling).
            # A real time abort needs a load-scaled threshold (planned); the
            # asyncio 600s ceiling in the runner remains the hard stop.
            elapsed_s = time.time() - t0
            if elapsed_s > 180 and tool_call_count > 4:
                logger.error(
                    "[ManagerAgent] Agent %s took too much time (%.1fs) over %d tool turns without completing.",
                    agent_name, elapsed_s, tool_call_count
                )

        # Pre-call hook, built here so it closes over this run's ticker — the
        # value the model keeps losing to bad JSON escaping.
        from app.v3.tool_repair import make_pre_tool_hook

        _pre_tool_hook = make_pre_tool_hook(
            ticker=ticker, agent_name=agent_name, cycle_id=cycle_id,
        )

        from app.services.prism_agent_registry import resolve_agent_id
        prism_agent_id = resolve_agent_id(agent_name)
        
        kwargs = {
            "name": prism_agent_id,
            "system_prompt": system_prompt,
            "llm_client": prism_client,
            "project": settings.PROJECT_NAME,
            "username": settings.PRISM_USERNAME,
            "auto_approve": overrides.get("prism_auto_approve", True),
            # Previously dropped: BaseAgent fell back to its own defaults
            # (temperature 0.0, max_tokens 8192) and the values run_agent's
            # callers passed were silently ignored.
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        resolved_model = model_override
        resolved_provider = None
        if not resolved_model:
            from app.services.prism_agent_caller import resolve_default_model_for_agent
            # Fail-closed: proceeding without a model hands the choice to the
            # SDK's hardcoded default (lazycat/agent.py — the Jetson's model),
            # and prism routes by model NAME, so a 2-second Gold Spark blip
            # rerouted a 62k-token junior-analyst payload onto the 65k Jetson
            # where the ContextExhaustionGuard refused it before iteration 1
            # (cycle-v3-1785905061, 2026-08-04). Raising instead lets the
            # aresilient_call backoff (~75s) ride out the blip.
            try:
                resolved_model, resolved_provider = await resolve_default_model_for_agent(
                    agent_name,
                    force_refresh=_resolution_state["force_refresh"],
                    endpoint_override=endpoint_override,
                )
                logger.info("[BaseAgent] Dynamically resolved default model for %s: %s (provider: %s)", agent_name, resolved_model, resolved_provider)
            except Exception as e:
                _resolution_state["force_refresh"] = True
                logger.warning(
                    "[BaseAgent] Model resolution failed for %s: %s — retrying "
                    "via aresilient_call rather than falling back to the SDK "
                    "default model.", agent_name, e,
                )
                raise
        
        if resolved_model:
            kwargs["model"] = resolved_model
        if resolved_provider:
            kwargs["provider"] = resolved_provider
        _model_holder["model"] = resolved_model
        _model_holder["provider"] = resolved_provider

        # See min_p_for: prism injects minP=0.05, and a spec-decoding vLLM box
        # answers that with an empty stream and an HTTP 200.
        resolved_min_p = min_p_for(
            resolved_provider or kwargs.get("provider"), resolved_model
        )
        if resolved_min_p is not None:
            if _BASE_AGENT_ACCEPTS_MIN_P:
                kwargs["min_p"] = resolved_min_p
            else:
                # A partial deploy (app/ updated, lazycat-sdk not synced) would
                # otherwise TypeError on EVERY agent construction — turning a
                # sampling fix into a total agent outage. Degrade to the old
                # broken-but-running behaviour and say so.
                logger.warning(
                    "[BaseAgent] Installed lazycat SDK does not accept min_p; "
                    "prism will inject minP=0.05 and a speculative-decoding "
                    "vLLM box will answer with an empty stream. Sync lazycat-sdk."
                )

        # ── Transport, derived from the agent's own declaration ────────────
        # See transport_for(). A tool-less role pays ~5.5s of TTFT and two
        # artifact failures in ten for a catalog it never touches.
        if transport_for(enable_tools, agent_tools) == "chat":
            from app.services.prism_agent_caller import chat_toolless

            _active_agents.add(agent_name)
            try:
                _chat = await chat_toolless(
                    provider=resolved_provider or "vllm",
                    model=resolved_model,
                    system_prompt=system_prompt,
                    user_prompt=full_prompt,
                    # Floor 4096: prism's ContextExhaustionGuard rejects any
                    # request whose output budget is under MINIMUM_VIABLE_
                    # OUTPUT_TOKENS, and callers legitimately pass small
                    # budgets (call_prism_agent expresses those as a
                    # conciseness directive instead, for the same reason).
                    max_tokens=max(4096, int(max_tokens or 8192)),
                    timeout_seconds=300.0,
                )
            finally:
                _active_agents.discard(agent_name)

            _text = _chat.get("response") or ""
            # Same fail-closed marker check the /agent branch does: prism
            # returns harness errors as ordinary assistant text, so without
            # this the error string is booked as the agent's artifact.
            from app.v3.model_shadow import _FAILURE_MARKERS
            _head = _text.lstrip()[:500]
            for _marker in _FAILURE_MARKERS:
                if _marker in _head:
                    _resolution_state["force_refresh"] = True
                    raise RuntimeError(
                        f"Prism chat error for {agent_name} (model "
                        f"{resolved_model}): {_head[:200]}"
                    )

            from app.services.prism_agent_caller import strip_reasoning_leak
            _text, _ = strip_reasoning_leak(_text, agent_name, reasoning_tokens=None)

            _model_holder["model"] = _chat.get("model_used") or resolved_model
            _model_holder["provider"] = _chat.get("provider") or resolved_provider
            return (
                _text,
                int(_chat.get("tokens_used") or 0),
                int((time.time() - t0) * 1000),
                # /chat is single-shot: one loop, and the tool transcript stays
                # empty because no tool ran. Reporting a higher count here would
                # inflate the loop stats the box comparison is built on.
                1,
                {},
                _chat.get("model_used") or resolved_model,
                _chat.get("provider") or resolved_provider,
            )

        agent = BaseAgent(**kwargs)
        if enable_tools and agent_tools:
            for t in agent_tools:
                agent.add_tool(t)
            # Prism resolves these tools under their MCP-registered names
            # (mcp__lazy-tool-service__<name>) while our schemas carry plain
            # names. enabledTools sent with plain names only → prism sees the
            # persona's availableTools (MCP names) as NOT enabled → "discovery
            # headroom" → it attaches discover/search/enable meta-tools, which
            # is how v3 agents reached execute_command/write_file (2026-07-21
            # audit F2). Advertise both name forms so headroom collapses to
            # zero. Name-only entries are safe on the prism path (enabledTools
            # is a name list; execution is server-side); guarded off the
            # direct-vLLM path where tools are sent as real schemas.
            try:
                from lazycat.llm import config as _lc_config
                from app.services.mcp_prefix import mcp_tool_name
                if getattr(_lc_config, "PRISM_ENABLED", False):
                    _plain = [
                        t.get("function", {}).get("name") or t.get("name")
                        for t in agent_tools
                    ]
                    for name in _plain:
                        # `mcp_tool_name` skips already-namespaced names and
                        # reads the emitted prefix from one place, so the
                        # lazy-tool-service -> lazy-agent-service rename does not
                        # need an edit here. Advertising a prefix prism does not
                        # route is not a soft failure: the model emits the name
                        # it was given and prism cannot resolve it, which
                        # surfaces as the agent claiming the tool is unavailable.
                        aliased = mcp_tool_name(name)
                        if aliased and aliased != name:
                            agent.add_tool({"name": aliased})
            except Exception as alias_err:
                logger.debug("[BaseAgent] MCP alias advertisement skipped: %s", alias_err)

        import uuid
        session = ConversationSession(session_id=parent_agent_session_id or f"sess_{int(time.time())}_{uuid.uuid4().hex[:6]}")
        
        _active_agents.add(agent_name)
        
        try:
            harness = AgentHarness(
                agent=agent,
                session=session,
                max_iterations=max_turns,
                on_tool_result=_on_tool_result if enable_tools else None,
                # PRE-call hook (2026-07-30). Repairs a malformed call before it
                # executes, instead of recording the failure afterwards: the
                # model routinely emits un-escaped JSON that loses the required
                # `ticker`, and the pipeline already knows which ticker this
                # desk is for. 18 rejections over 07-28..07-30, get_sec_filings
                # at 26.3% failure over 14 days.
                #
                # Repairs, never blocks — and fail-closed on an allow-list that
                # excludes every order and watchlist tool. See app/v3/tool_repair.py.
                on_tool_call=_pre_tool_hook if enable_tools else None,
                # Non-interactive pipeline: suppress Qwen <think> blocks (real
                # prism honors an explicit thinkingEnabled=false per request;
                # registration-level thinkingDefault is ignored there).
                thinking_enabled=False,
            )

            t0 = time.time()
            final_text = await harness.run(full_prompt)
            # Reasoning-leak canary — same tripwire as call_prism_agent /
            # chat_with_tools; the harness path is a third response site and
            # a shared helper only helps callers that call it.
            from app.services.prism_agent_caller import strip_reasoning_leak
            # Hand the canary the usage evidence. Without it the tripwire fires
            # on any report that opens "Let me…" and asserts a cause it cannot
            # actually see.
            _usage = dict(getattr(harness, "last_usage", {}) or {})
            _reasoning_tokens = _usage.get("reasoningOutputTokens")
            final_text, _leaked = strip_reasoning_leak(
                final_text, agent_name,
                reasoning_tokens=(
                    _reasoning_tokens if isinstance(_reasoning_tokens, int) else None
                ),
            )
            elapsed_ms = int((time.time() - t0) * 1000)

            # Harness/guard refusals come back as a NORMAL string (prism
            # injects the error as an assistant message and returns it), so
            # without this check the error text is booked as the agent's
            # artifact and parsing chews on it downstream. Same markers the
            # shadow bench uses (model_shadow.classify_shadow). Head-only
            # check: a real artifact opens with '{' JSON, so a marker in the
            # head is decisively prism's injected recovery preamble, not the
            # model quoting the phrase deep inside its own analysis.
            from app.v3.model_shadow import _FAILURE_MARKERS
            _head = (final_text or "").lstrip()[:500]
            for _marker in _FAILURE_MARKERS:
                if _marker in _head:
                    _resolution_state["force_refresh"] = True
                    raise RuntimeError(
                        f"Prism harness error for {agent_name} (model "
                        f"{resolved_model}): {_head[:200]}"
                    )
        finally:
            _active_agents.discard(agent_name)

        return (
            final_text,
            int(getattr(harness, "total_tokens", 0) or 0),
            elapsed_ms,
            tool_call_count + 1,
            dict(getattr(harness, "last_usage", {}) or {}),
            # Prefer the stream's done-event model (prism's server-side
            # resolution) over what we asked for — a gateway-side swap makes
            # the requested name a lie (observed 07-31: silent switch to
            # deepseek-v4-flash-0731).
            getattr(harness, "last_model", None) or resolved_model,
            getattr(harness, "last_provider", None) or resolved_provider,
        )

    content, tokens, elapsed_ms, loops_used, last_usage, model_used, provider_used = await _agent_llm_call()

    if not content or not str(content).strip():
        # Open item 4 (2026-08-05): the sentinel used to be the ONLY record of
        # an empty response — the intermittent v3_portfolio_manager failures
        # could not be diagnosed because nothing captured what prism actually
        # returned. Log the full harness telemetry before substituting, so the
        # next failure self-documents (was it a model swap, a zero-token
        # stream, a tool-payload timeout at full duration, ...).
        logger.error(
            "[BaseAgent] EMPTY RESPONSE from %s (%s): raw=%r | model=%s "
            "provider=%s | tokens=%d elapsed=%dms loops=%d | usage=%s",
            agent_name, ticker, content, model_used, provider_used,
            tokens, elapsed_ms, loops_used, last_usage,
        )
        content = f"Agent failed: empty response from {agent_name}"

    # ── Verbose output logging (structured, truncated to prevent log spam) ──
    _out_preview = sanitize_ascii(content[:1500]) if content else ""
    logger.debug(
        "[BaseAgent] OUTPUT %s (%s) | %d tokens | %dms | response=%d chars\n  %s%s",
        agent_name, ticker, tokens, elapsed_ms, len(content) if content else 0,
        _out_preview, "..." if content and len(content) > 1500 else "",
    )

    # Derive stop_reason so agent_runner's budget warning actually fires.
    #
    # The SDK harness has a sentinel string, but Prism does NOT: when an agent
    # hits Prism's iteration ceiling it injects an <iteration-limit> system
    # message and returns whatever the model says next. In practice the model
    # often replies with a *pseudo* tool call in plain text — a short line like
    #   call:mcp__lazy-tool-service__get_sec_filings{ticker:WFC}
    # Matching only the SDK sentinel meant this warning never fired once
    # against Prism, so budget exhaustion presented as an artifact parse bug.
    #
    # 2026-08-09: that fix saw a MINORITY of the wall. It required the reply to
    # be under 400 chars, and the majority shape is the opposite — the model
    # narrates for thousands of chars ("Let me also check the whiteboard...")
    # and the run ends mid-plan. 233 of 381 unparseable replies in 7 days were
    # over 2k chars, so they were all booked "completed" and the tripwire named
    # for this exact cause never saw its own majority case. The shape test now
    # lives in one place (`app/v3/output_rules.py`) and this reads it, so the
    # classifier the repair pass uses and the one stop_reason uses cannot drift.
    from app.v3.output_rules import classify_output

    stop_reason = "max_iterations" if classify_output(content).exhausted else "completed"

    return {
        "agent": agent_name,
        "ticker": ticker,
        "cycle_id": cycle_id,
        "bot_id": bot_id,
        "response": content,
        "tokens_used": tokens,
        "execution_ms": elapsed_ms,
        "loops_used": loops_used,
        "stop_reason": stop_reason,
        # The agent's own findings, so a failed artifact can be repaired from
        # the research it already paid for rather than from its last sentence.
        "tool_transcript": tool_transcript,
        "model_used": model_used,
        "provider": provider_used,
        # Snapshot of the harness's LAST request, not a loop-wide sum. That is
        # the right probe for "is prefix caching working at all": the final
        # iteration carries the longest shared prefix, so cacheReadInputTokens
        # = 0 here means the KV cache is doing nothing for this agent.
        "cached_tokens": int(last_usage.get("cacheReadInputTokens") or 0),
        "cache_creation_tokens": int(last_usage.get("cacheCreationInputTokens") or 0),
        "prompt_tokens": int(
            last_usage.get("totalInputTokens")
            or last_usage.get("inputTokens")
            or 0
        ),
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }
