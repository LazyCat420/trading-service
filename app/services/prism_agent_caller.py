import json
import logging
import time
from typing import Any
from datetime import datetime, timezone

from lazycat.llm import prism_client
from app.services.prism_agent_registry import resolve_agent_id
from app.telemetry.bus import publish_event
from app.telemetry.schema import TelemetryEvent

from app.config import settings

logger = logging.getLogger(__name__)

FIRM_CONTEXT = (
    "CRITICAL CONTEXT: You are an autonomous data processing script working for a "
    "quantitative trading firm. You are NOT a conversational chatbot. Do NOT talk "
    "to the user, give advice, ask questions, or converse. Your ONLY purpose is to "
    "extract structured financial data to make profitable trading decisions.\n\n"
)

import httpx
import re

_dynamic_model_cache = {}

# ── Reasoning-leak canary (2026-08-03) ──────────────────────────────────────
# Gold Spark swapped to deepseek-v4-flash-0731 on 07-31; prism's thinking-off
# flag uses the Qwen spelling (enable_thinking) which DeepSeek silently
# ignores, so reasoning ran on every call and intermittently leaked into
# content (flash_briefings 126/127 opened with "The user wants me to…").
# The real fix is the vllm-shim in lazy-agent-service; this is the tripwire
# that catches the NEXT silent model swap. Applied at every response site —
# call_prism_agent, chat_with_tools, and base_agent's harness path — because
# a shared helper is only a shared fix if every caller actually calls it.
_REASONING_LEAK_RE = re.compile(
    r"^(?:the user (?:wants|is asking|has asked)|let me\b|i need to\b|okay[,.]"
    r"|i am an?\b|i'm an?\b|the task\b|first,? (?:let|i)\b|we need to\b)",
    re.IGNORECASE,
)
_FIRST_HEADING_RE = re.compile(r"\n#{1,3} ")

# Milder tier: not a reasoning trace, just a conversational acknowledgment
# before the real report ("I'll write the market close briefing based on the
# provided data.\n\n# Market Close Flash Briefing…" — observed live 08-03 with
# thinking correctly OFF). Same salvage rule, quieter log.
_PREAMBLE_RE = re.compile(
    r"^(?:i'll\b|i will\b|sure[,!]|certainly[,!.]|here (?:is|'s)\b)",
    re.IGNORECASE,
)


def strip_reasoning_leak(
    text: str, agent_name: str = "", reasoning_tokens: int | None = None
) -> tuple[str, bool]:
    """Detect a reasoning trace leaked into response content; salvage if safe.

    Returns (text, leaked). When the leak is followed by the real markdown
    report (observed shape: briefing 126), cut to the first heading — but only
    if that keeps ≥30% of the text AND ≥400 chars, because the other observed
    shape (briefing 127) is reasoning all the way down to a trailing Sources
    heading, where cutting would keep 9% and destroy the only content there is.
    Unsalvageable leaks are returned unchanged so the caller ships *something*
    while the canary log points at the real problem.
    """
    stripped = (text or "").lstrip()
    if not stripped:
        return text, False

    # A prefix match is a SUSPICION, not evidence (2026-08-05). The regex fires
    # on any answer that opens "Let me…", and analysts legitimately write that
    # way — "Let me trace the most load-bearing lead. The key story here is the
    # AI capex/FCF…" is the junior analyst's report, not a leaked trace.
    #
    # When the usage block says ZERO reasoning tokens, the model demonstrably
    # did not think, so a leak is impossible and the alarm is a false positive.
    # This matters more than the noise: the canary's own error message names a
    # cause ("the thinking-off flag is not reaching the model"), that message
    # was believed, and it was written into the docs as the root cause of an
    # unrelated artifact-loss bug. A tripwire that asserts a diagnosis has to
    # be right about it. Measured the same day: 43/43 chat calls reached the
    # shim carrying enable_thinking=false, and reasoningOutputTokens was 0.
    #
    # None means "unknown" — callers that cannot see usage keep the old
    # behaviour rather than silently losing the tripwire.
    if reasoning_tokens == 0 and _REASONING_LEAK_RE.match(stripped):
        logger.debug(
            "[THINK-LEAK] %s: opening phrase matched but the model emitted 0 "
            "reasoning tokens — prose, not a leak.", agent_name or "unknown",
        )
        return text, False

    if not _REASONING_LEAK_RE.match(stripped):
        # Conversational acknowledgment before the report — trim it with the
        # same retention-guarded heading cut, but don't sound the leak alarm.
        if _PREAMBLE_RE.match(stripped):
            m = _FIRST_HEADING_RE.search(stripped)
            if m:
                remainder = stripped[m.start():].lstrip()
                if len(remainder) >= 400 and len(remainder) >= 0.3 * len(stripped):
                    logger.info(
                        "[PREAMBLE] %s: trimmed conversational preamble "
                        "(%d chars) before first heading",
                        agent_name or "unknown", m.start(),
                    )
                    return remainder, False
        return text, False

    logger.error(
        "[THINK-LEAK] %s: response content starts with a reasoning trace "
        "(%r). The thinking-off flag is not reaching the model — check the "
        "vllm-shim and whether the endpoint's model changed.",
        agent_name or "unknown", stripped[:80],
    )
    m = _FIRST_HEADING_RE.search(stripped)
    if m:
        remainder = stripped[m.start():].lstrip()
        if len(remainder) >= 400 and len(remainder) >= 0.3 * len(stripped):
            logger.warning(
                "[THINK-LEAK] %s: salvaged report from first heading "
                "(kept %d of %d chars)", agent_name or "unknown",
                len(remainder), len(stripped),
            )
            return remainder, True
    return text, True


def _extract_token_usage(resp: Any, response_text: str) -> int:
    """Real token count from the prism/vLLM response envelope.

    The old `len(response_text) // 4` counted OUTPUT characters only, so
    input tokens (which dominate — evidence packets, long prompts) were
    invisible. This made the tournament debate report ~2.5K tokens for an
    8-minute, many-call run. Prefer the provider's `usage`; fall back to the
    char estimate only when usage is absent.
    """
    try:
        payload = resp.json() if hasattr(resp, "json") else None
        if isinstance(payload, dict):
            usage = payload.get("usage") or {}
            if isinstance(usage, dict) and usage:
                # Prism/lazy-agent (TypeScript) emit camelCase — the SDK's
                # streaming path sums inputTokens+outputTokens+reasoningOutputTokens
                # (lazycat/agent.py). Match that first; keep snake_case + a
                # totalTokens field as fallbacks for other providers.
                total = usage.get("totalTokens") or usage.get("total_tokens")
                if isinstance(total, (int, float)) and total > 0:
                    return int(total)
                inp = usage.get("inputTokens") or usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                out = usage.get("outputTokens") or usage.get("completion_tokens") or usage.get("output_tokens") or 0
                reasoning = usage.get("reasoningOutputTokens") or usage.get("reasoning_tokens") or 0
                if inp or out or reasoning:
                    return int(inp) + int(out) + int(reasoning)
    except Exception:
        pass
    # Fallback: rough estimate from output length (better than nothing).
    return len(response_text or "") // 4

#: How long a cached model id may still be served AFTER the probe fails.
#: A box's model id changes only when someone reloads it, so a stale answer is
#: nearly always the right answer; an hour bounds how long we can be wrong.
_STALE_MODEL_GRACE_S = 3600


class ModelUnavailableError(RuntimeError):
    """The box was reached and has no servable model — POSITIVE evidence that
    the LLM path is dead, not ambiguity about our own probe machinery.

    Raised only when `get_live_model_from_vllm` has exhausted both attempts
    AND has no cached id to degrade to: we asked `/v1/models` and the box
    answered with nothing usable (or refused to answer at all).

    It exists so callers can tell this apart from the *config* RuntimeErrors
    next to it ("endpoint not configured or disabled", "no configured URL"),
    which say nothing about whether the box is alive. `llm_preflight` makes
    exactly that distinction: 2026-08-28..30, the resolver raised
    `VLLM endpoint offline: .../gold-spark (RuntimeError: HTTP 502 with no
    usable model list)` and the pre-flight classified it as probe machinery
    breaking and PROCEEDED — 33 desks, 66 regime-engine calls at 75-102s
    each, zero decisions, no page. A RuntimeError subclass so every existing
    `except RuntimeError` keeps working unchanged.
    """


async def get_live_model_from_vllm(url: str, force_refresh: bool = False) -> str:
    """Resolve the model a vLLM box is serving. Cached 5 min; degrades to stale.

    ON THE CRITICAL PATH. `resolve_default_model_for_agent` calls this for
    EVERY agent, so whatever this raises, that agent's run raises too.

    WHY IT DEGRADES INSTEAD OF RAISING. 2026-08-06, the first real gatekeeper
    shadow failed with `VLLM endpoint offline: http://10.0.0.30:8000
    (error: )` — an httpx timeout, whose message is empty. Seconds later the
    same box answered a direct probe in 37ms, and measured afterwards it never
    exceeded 70ms: 0/30 probes over 2s, idle AND under 8 concurrent
    generations. So the box was not slow. The 2s budget expired inside a
    container that was mid-cycle, which points at this process, not the
    endpoint.

    The mechanism is NOT proven — that is stated plainly rather than papered
    over. What is certain is the shape of the failure: a probe whose result is
    cached for five minutes took down a call while a perfectly good answer sat
    in the cache. So a failed refresh now falls back to the last known model id
    for up to an hour, loudly, and only an empty cache is fatal. A genuinely
    dead box still fails — one layer down, at prism, with a real error message
    instead of an empty one.
    """
    now = time.time()
    cached = _dynamic_model_cache.get(url)
    if not force_refresh and cached and now - cached[1] < 300:  # 5 minute TTL
        return cached[0]

    last_error: Exception | None = None
    # Two attempts: the observed failure was transient, and a retry costs at
    # most a few seconds against a value good for the next five minutes.
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{url}/v1/models")
                if resp.status_code == 200:
                    models = resp.json().get("data", [])
                    if models:
                        model_id = models[0].get("id")
                        if model_id:
                            _dynamic_model_cache[url] = (model_id, now)
                            return model_id
                last_error = RuntimeError(
                    f"HTTP {resp.status_code} with no usable model list"
                )
        except Exception as e:  # noqa: BLE001 — retried, then degraded below
            last_error = e
            # The type matters: an httpx timeout stringifies to "", so the old
            # message read "(error: )" and said nothing about what went wrong.
            logger.warning(
                "[VLLM] model probe attempt %d/2 failed for %s: %s: %s",
                attempt, url, type(e).__name__, str(e) or "<no message>",
            )

    if cached and now - cached[1] < _STALE_MODEL_GRACE_S:
        logger.warning(
            "[VLLM] model probe failed for %s (%s: %s) — serving the cached id "
            "%s from %.0fs ago rather than failing the call.",
            url, type(last_error).__name__, str(last_error) or "<no message>",
            cached[0], now - cached[1],
        )
        return cached[0]

    if last_error is None:
        raise ModelUnavailableError(f"No models found at vLLM endpoint: {url}")
    raise ModelUnavailableError(
        f"VLLM endpoint offline: {url} "
        f"({type(last_error).__name__}: {str(last_error) or '<no message>'})"
    )

# Endpoint key -> the prism provider slug that reaches it. Both halves of
# the pair have to move together: `provider` is what prism routes on, and
# `endpoint_key` is what we ask for the live model name. Keeping them in one
# mapping is what makes `endpoint_override` safe to expose.
def _prism_client_accepts_min_p() -> bool:
    """Does the INSTALLED lazycat SDK take `min_p` on `PrismClient.call_agent`?

    Mirrors `base_agent._base_agent_accepts_min_p`. deploy.sh syncs
    lazycat-sdk alongside app/, so the two move together in a normal deploy —
    but a partial one would raise TypeError on EVERY agent call here and turn
    a sampling fix into a total LLM outage. Checked once at import.
    """
    try:
        import inspect

        from lazycat.llm import PrismClient as _PC

        return "min_p" in inspect.signature(_PC.call_agent).parameters
    except Exception:  # noqa: BLE001 — never let a capability probe stop boot
        return False


_PRISM_CLIENT_ACCEPTS_MIN_P = _prism_client_accepts_min_p()


def min_p_kwargs(provider: str | None, model: str | None) -> dict:
    """`{"min_p": 0.0}` for a local vLLM box; `{}` for anything else.

    WHY EVERY `/agent` CALL MUST CARRY THIS. Prism's ParameterRegistry gives
    `minP` an `agentDefault` of 0.05 and injects it whenever the payload
    carries an `agent` field — which every call from this module does. vLLM
    running speculative decoding REFUSES any min_p > 0:

        The min_p and logit_bias sampling parameters are not yet supported
        with speculative decoding

    On the non-streaming path that is an HTTP 400; on prism's streaming path
    the refusal arrives as an in-band SSE error frame AFTER the 200 header,
    which prism's parser skips — so the caller sees a *successful* call with
    empty text and zero usage, and prism's own empty-output recovery then
    retries with the temperature raised, moving further from the one setting
    that works.

    Re-measured 2026-09-01 against the Jetson
    (`cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit`, spec_decode drafts 1.85M):
    `temperature=0.3 + min_p=0.05` -> HTTP 400, `min_p=0` -> 200 with content.
    Gold Spark behaves identically, so this is not one box's quirk.

    `temperature=0` masks the bug (vLLM zeroes min_p under greedy sampling) —
    which is exactly why the few temp-0 callers here never failed while every
    temp>0 caller returned empty from 2026-08-26 onward (477/477 empty in
    prism's request ledger, 0 successes).

    `base_agent.min_p_for` already applies this rule on the BaseAgent path and
    `chat_toolless` sends the field outright. This helper is the same rule for
    the `call_prism_agent` call sites and the class path, which were the ones
    still leaving prism's default in place.
    """
    from app.agents.base_agent import min_p_for

    value = min_p_for(provider, model)
    if value is None:
        return {}
    if not _PRISM_CLIENT_ACCEPTS_MIN_P:
        logger.warning(
            "[PrismAgentCaller] the installed lazycat SDK does not accept min_p — "
            "prism's injected default (0.05) stays on the wire for %s/%s. A local "
            "vLLM box running speculative decoding answers that with an empty "
            "stream; sync lazycat-sdk.",
            provider, model,
        )
        return {}
    return {"min_p": value}


ENDPOINT_PROVIDERS: dict[str, str] = {
    "jetson": "vllm",
    "dgx_spark": "vllm-2",
}


class ModelContractError(RuntimeError):
    """The decision box is serving a model the decision agents were not built
    for. Raised by `resolve_default_model_for_agent` BEFORE any prompt is sent,
    so the failure is one cheap resolution instead of a 24k-token call retried
    five times.

    2026-08-25/26: dgx_spark answered `cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit`
    (prism's memory jobs had it loaded). Qwen replied to the 24k regime prompt
    with a 10-token `{"regime": "NEUTRAL"}` that fails the artifact contract,
    every follow-up iteration returned null, and 45 of 74 desks died as
    `board_degraded_fallback` with no page — the stored error said only
    "All 5 attempts failed [last_type=transient]". The pre-flight could not
    catch it because the endpoint was alive.
    """


async def chat_toolless(
    *, provider: str, model: str, system_prompt: str, user_prompt: str,
    max_tokens: int, timeout_seconds: float,
) -> dict:
    """Call prism's TOOL-LESS `/chat` endpoint and collect the SSE stream.

    Use this for any agent that passes `enable_tools=False`. The SDK's
    `call_agent` (i.e. `/agent`) attaches the full MCP catalog server-side —
    re-measured 2026-08-06 at **~83 tools ≈ 21k tokens**, before any prompt —
    and no request field removes it (`enabledTools`/`tools`/`toolsEnabled`)
    because tool attachment is
    server-side policy, not a request parameter. `enable_tools=False` is a
    CLIENT-side flag: it stops us sending schemas, it does not stop prism
    attaching them.

    Two consequences, both measured:
       * At ~21k tokens the catalog fits inside Jetson's 65,536 window
         (38,179 output tokens remain — see open item 1). The earlier
         91k claim was stale. The actual gatekeeper failures were from
         prism injecting minP > 0 under speculative decoding (Fault A).
      * Even on Gold Spark's 1M window the catalog is charged against the
        request: the gatekeeper measured ~21,940 total input tokens for a
        ~1,900-token prompt, and returned empty content on the larger
        watchlists while the same model answered a raw call perfectly.

    Returns the same shape as `run_agent`'s result dict for the keys callers
    actually read: `response`, `tokens_used`, `loops_used`, `model_used`,
    `provider`, `execution_ms`.
    """
    import json as _json
    import time as _time
    import httpx
    from app.config import settings
    from app.agents.base_agent import min_p_for

    url = f"{settings.PRISM_URL.rstrip('/')}/chat"
    payload = {
        "model": model,
        "provider": provider,
        "project": settings.PROJECT_NAME,
        "systemPrompt": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
        "maxTokens": max_tokens,
        "thinkingEnabled": False,
    }
    # minP is sent EXPLICITLY, not left to prism's default, even though this
    # endpoint does not currently inject one.
    #
    # Prism applies `getAgentDefaults()` — which carries `minP: 0.05`, the
    # value a speculative-decoding vLLM box answers with an empty stream after
    # HTTP 200 — inside `if (agent)` in ChatRoutes.prepareGenerationContext.
    # The trigger is the `agent` FIELD in the payload, not the endpoint: /chat
    # and /agent share that code path, and /chat is safe here only because this
    # payload happens to omit `agent`. Since 5f42260 routed every tool-less
    # role through here, that accident guards most of the desk's LLM calls, and
    # adding `agent` for persona attribution would silently re-open
    # GATEKEEPER_DEGRADED on every local box. Sending the field makes the
    # protection a property of this request instead of an omission.
    _min_p = min_p_for(provider, model)
    if _min_p is not None:
        payload["minP"] = _min_p
    text_parts: list[str] = []
    done: dict = {}
    _t0 = _time.monotonic()
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    evt = _json.loads(line[6:])
                except Exception:
                    continue
                kind = evt.get("type")
                if kind == "chunk":
                    text_parts.append(evt.get("content") or "")
                elif kind == "done":
                    done = evt
                elif kind == "error":
                    raise RuntimeError(str(evt)[:300])
    usage = done.get("usage") or {}
    return {
        "response": "".join(text_parts),
        # Total, not output-only: the leaderboard's token columns are totals
        # everywhere else and a shadow that reported only output would look
        # artificially cheap next to the primary.
        "tokens_used": (usage.get("inputTokens") or 0) + (usage.get("outputTokens") or 0),
        "loops_used": 1,
        "model_used": done.get("model"),
        "provider": done.get("provider"),
        # run_agent's result dict carries this and callers read it. Without it
        # the gatekeeper's shadow rows recorded primary_elapsed_ms=0 on every
        # row — the primary reading as instant next to the shadow it is being
        # compared against.
        "execution_ms": int((_time.monotonic() - _t0) * 1000),
    }


async def resolve_default_model_for_agent(
    agent_name: str,
    force_refresh: bool = False,
    endpoint_override: str | None = None,
) -> tuple[str, str]:
    """Resolve default model based on agent role to balance load.
    Jetson handles lightweight janitorial, consensus, and curation tasks.
    Gold Spark handles heavy quant research, debates, and final decisions.

    `endpoint_override` names a box directly ("jetson" / "dgx_spark") and skips
    the name-keyword rule entirely. It exists so a caller can vary the MODEL
    without varying the agent NAME — the keyword rule can only move work to
    Jetson by renaming the agent, which relabels the role and makes the two
    arms of a per-role model comparison look like two different jobs.

    An unknown override RAISES rather than falling back to the default box: a
    silent fallback would run both arms of an A/B on the same box while the
    telemetry still claimed a split, which is worse than no comparison at all.
    """
    from app.services.prism_agent_caller import llm
    from app.config.config import settings as _settings

    solo_jetson = bool(getattr(_settings, "SOLO_JETSON_MODE", False))
    routing_mode = getattr(_settings, "ROUTING_MODE", "auto")

    if endpoint_override:
        if endpoint_override not in ENDPOINT_PROVIDERS:
            raise ValueError(
                f"Unknown endpoint_override {endpoint_override!r} — "
                f"expected one of {sorted(ENDPOINT_PROVIDERS)}"
            )
        endpoint_key = endpoint_override
        provider = ENDPOINT_PROVIDERS[endpoint_key]
        candidates = [(endpoint_key, provider)]
    elif solo_jetson or routing_mode == "force_jetson":
        candidates = [("jetson", ENDPOINT_PROVIDERS["jetson"])]
    elif routing_mode == "force_dgx":
        candidates = [("dgx_spark", ENDPOINT_PROVIDERS["dgx_spark"])]
    else:
        # Dynamic smart routing:
        # Harder tasks (research, debate, decisions) prefer dgx_spark;
        # lightweight / collector tasks prefer jetson.
        name_lower = (agent_name or "").lower()
        collector_keywords = (
            "janitor", "curator", "summarizer", "scout", "purge",
            "maintenance", "consensus", "ticker_validator"
        )
        is_collector = any(kw in name_lower for kw in collector_keywords)
        if is_collector:
            candidates = [
                ("jetson", ENDPOINT_PROVIDERS["jetson"]),
                ("dgx_spark", ENDPOINT_PROVIDERS["dgx_spark"]),
            ]
        else:
            candidates = [
                ("dgx_spark", ENDPOINT_PROVIDERS["dgx_spark"]),
                ("jetson", ENDPOINT_PROVIDERS["jetson"]),
            ]

    discovered_model = None
    chosen_endpoint = None
    chosen_provider = None
    last_error = None

    for ep_key, prov in candidates:
        ep = llm._endpoints.get(ep_key)
        if not ep or not ep.enabled or not ep.url:
            if not last_error:
                last_error = RuntimeError(f"VLLM endpoint '{ep_key}' is not configured or disabled.")
            continue
        try:
            discovered_model = await get_live_model_from_vllm(ep.url, force_refresh=force_refresh)
        except (ModelUnavailableError, RuntimeError, Exception) as exc:
            logger.warning(
                "[SmartRouting] Endpoint '%s' unavailable (%s) — attempting fallback if available.",
                ep_key, exc
            )
            last_error = exc
            continue

        # Decision agents run on dgx_spark or jetson fallback.
        # Normal Jetson collector/janitor roles are model-agnostic by design.
        name_lower = (agent_name or "").lower()
        collector_keywords = (
            "janitor", "curator", "summarizer", "scout", "purge",
            "maintenance", "consensus", "ticker_validator"
        )
        is_collector = any(kw in name_lower for kw in collector_keywords)
        must_check_contract = (not is_collector) or (ep_key == "jetson" and solo_jetson)

        if must_check_contract:
            pattern = (getattr(_settings, "DECISION_MODEL_PATTERN", "") or "").strip().lower()
            if pattern:
                patterns = [p.strip() for p in pattern.split("|") if p.strip()]
                model_lower = str(discovered_model).lower()
                if not any(p in model_lower for p in patterns):
                    contract_err = ModelContractError(
                        f"{ep_key} is serving {discovered_model!r}, which does not match "
                        f"DECISION_MODEL_PATTERN={pattern!r}. Refusing to run {agent_name} "
                        f"against a model the decision agents were not built for."
                    )
                    logger.warning(
                        "[SmartRouting] Endpoint '%s' model contract violation: %s — attempting fallback if available.",
                        ep_key, contract_err
                    )
                    last_error = contract_err
                    continue

        chosen_endpoint = ep_key
        chosen_provider = prov
        break

    if not chosen_endpoint or not discovered_model:
        if isinstance(last_error, (ModelUnavailableError, ModelContractError)):
            raise last_error
        raise ModelUnavailableError(f"No vLLM endpoints available: {last_error}")

    return discovered_model, chosen_provider


async def call_prism_agent(
    agent_id: str,
    user_message: str,
    fallback_system_prompt: str,
    fallback_agent_name: str,
    priority: Any = None,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    ticker: str = "",
    cycle_id: str = "",
    bot_id: str = "",
    agentic_mode: bool = False,
    actor_label: str | None = None,
    parent_conversation_id: str | None = None,
    parent_agent_session_id: str | None = None,
    model_override: str | None = None,
    project: str | None = None,
    endpoint_override: str | None = None,
) -> tuple[str, int, int]:
    """Route an LLM call through Prism SDK."""
    start = time.monotonic()

    if max_tokens is None:
        max_tokens = 8192

    # Prism's ContextExhaustionGuard rejects ANY request whose output budget
    # is under MINIMUM_VIABLE_OUTPUT_TOKENS (4096) — there are no exemptions,
    # so every call must be floored at 4096. Small budgets are expressed via
    # the conciseness directive instead.
    instruction = ""
    if max_tokens < 4096:
        if max_tokens <= 128:
            sentences = "1 or 2 sentences max"
        elif max_tokens <= 256:
            sentences = "under 4 sentences"
        elif max_tokens <= 512:
            sentences = "under 8 sentences"
        elif max_tokens <= 1024:
            sentences = "under 15 sentences"
        else:
            sentences = "concise"
            
        instruction = f"\n\n[SYSTEM DIRECTIVE: Keep your response concise, {sentences}.]"
        fallback_system_prompt = (fallback_system_prompt or "") + instruction
        max_tokens = 8192

    agent_id = resolve_agent_id(agent_id or fallback_agent_name)

    try:
        publish_event(TelemetryEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            cycle_id=cycle_id,
            ticker=ticker,
            kind="llm",
            source="prism",
            status="ok",
            step="prism_agent_start",
            detail=f"Starting call to {agent_id}"
        ))
    except Exception as tel_err:
        logger.warning("Failed to publish telemetry start event: %s", tel_err)
    
    try:
        # Prepend system prompt directly to messages list for OpenAI/vLLM compatibility.
        # We interleave an assistant acknowledgment turn between system prompt and user message
        # to ensure strict role alternation: system -> assistant -> user.
        messages = [
            {"role": "system", "content": FIRM_CONTEXT + (fallback_system_prompt or "")},
            {"role": "assistant", "content": "Acknowledged. I am ready to process the quantitative data."},
            {"role": "user", "content": user_message}
        ]
        from app.v3.guardrails import get_budget_for_role
        max_iter = get_budget_for_role(agent_id).max_turns

        default_model, default_provider = await resolve_default_model_for_agent(
            fallback_agent_name or agent_id, endpoint_override=endpoint_override
        )
        model = model_override or default_model
        
        if model_override:
            name_lower = model_override.lower()
            if "gpt-" in name_lower:
                provider = "openai"
            elif "claude-" in name_lower:
                provider = "anthropic"
            elif "gemini-" in name_lower:
                provider = "google"
            else:
                provider = default_provider
        else:
            provider = default_provider
        
        # vLLM will reject requests if (prompt_tokens + max_tokens > max_model_len).
        # We subtract the estimated prompt tokens from max_tokens to give the largest possible budget without crashing.
        all_text = (
            FIRM_CONTEXT + (fallback_system_prompt or "") +
            "Acknowledged. I am ready to process the quantitative data." +
            user_message
        )
        est_input_tokens = len(all_text) // 4 + 100
        if max_tokens >= 4096:
            # Never drop below 4096: Prism's ContextExhaustionGuard rejects
            # smaller output budgets outright (and Prism does its own output
            # clamping against the real context window, so the subtraction is
            # just a vLLM-overflow courtesy, not a correctness requirement).
            max_tokens = max(4096, max_tokens - est_input_tokens)
        
        bench_task = f"{fallback_agent_name or agent_id}:{ticker}" if ticker else (fallback_agent_name or agent_id)
        try:
            resp = await prism_client.call_agent(
                model=model,
                messages=messages,
                system_prompt=FIRM_CONTEXT + (fallback_system_prompt or ""),
                agent_name=agent_id,
                max_tokens=max_tokens,
                temperature=temperature,
                project=project or settings.PROJECT_NAME,
                max_iterations=max_iter,
                provider=provider,
                thinking_enabled=False,
                bench_task=bench_task,
                **min_p_kwargs(provider, model),
            )
        except Exception as e:
            if "404" in str(e) or "not exist" in str(e).lower() or "not found" in str(e).lower():
                logger.warning(f"[PrismAgentCaller] 404 Model Not Found. Forcing refresh and retrying...")
                # Fetch fresh model and try exactly one more time
                fresh_model, _ = await resolve_default_model_for_agent(fallback_agent_name or agent_id, force_refresh=True)
                resp = await prism_client.call_agent(
                    model=fresh_model,
                    messages=messages,
                    system_prompt=FIRM_CONTEXT + (fallback_system_prompt or ""),
                    agent_name=agent_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    project=project or settings.PROJECT_NAME,
                    max_iterations=max_iter,
                    provider=provider,
                    thinking_enabled=False,
                    bench_task=bench_task,
                    **min_p_kwargs(provider, fresh_model),
                )
            else:
                raise e
        
        try:
            # Prism sets text to null (not missing) on textless turns —
            # .get("text", "") returns None there, and falling back to
            # resp.text would hand the caller the raw JSON envelope as if it
            # were the agent's answer.
            response_text = (resp.json().get("text") or "").strip()
        except Exception as parse_err:
            logger.error("[PrismAgentCaller] %s: response body was not JSON (%s) — returning empty text", agent_id, parse_err)
            response_text = ""
        response_text, leaked = strip_reasoning_leak(response_text, agent_id)
        usage_resp = resp
        if not response_text:
            # A fast empty response indicates a chat-template or reasoning token drop.
            # Retry once with a clean, single-turn [system, user] structure.
            logger.warning(
                "[PrismAgentCaller] %s: empty response text (%dms) — attempting clean single-turn retry",
                agent_id, int((time.monotonic() - start) * 1000),
            )
            try:
                clean_messages = [
                    {"role": "system", "content": FIRM_CONTEXT + (fallback_system_prompt or "")},
                    {"role": "user", "content": user_message},
                ]
                retry_resp = await prism_client.call_agent(
                    model=model,
                    messages=clean_messages,
                    system_prompt=FIRM_CONTEXT + (fallback_system_prompt or ""),
                    agent_name=agent_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    project=project or settings.PROJECT_NAME,
                    max_iterations=max_iter,
                    provider=provider,
                    thinking_enabled=False,
                    **min_p_kwargs(provider, model),
                )
                retry_text = (retry_resp.json().get("text") or "").strip()
                retry_text, _ = strip_reasoning_leak(retry_text, agent_id)
                if retry_text:
                    response_text = retry_text
                    # Bill the response that actually produced the text. Reading
                    # usage off the FIRST (empty) response reported 0 tokens for
                    # a call that really did spend them.
                    usage_resp = retry_resp
                    logger.info("[PrismAgentCaller] %s: single-turn retry SUCCEEDED (%d chars)", agent_id, len(response_text))
            except Exception as retry_err:
                logger.debug("[PrismAgentCaller] %s: single-turn retry failed: %s", agent_id, retry_err)

        if not response_text:
            logger.warning(
                "[THINK-LEAK] %s: empty response text after retry — output budget may have been consumed",
                agent_id,
            )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        tokens = _extract_token_usage(usage_resp, response_text)
        
        try:
            publish_event(TelemetryEvent(
                ts=datetime.now(timezone.utc).isoformat(),
                cycle_id=cycle_id,
                ticker=ticker,
                kind="llm",
                source="prism",
                status="ok",
                step="prism_agent_success",
                detail=f"Completed {agent_id} in {elapsed_ms}ms"
            ))
        except Exception as tel_err:
            logger.warning("Failed to publish telemetry success event: %s", tel_err)
            
        return response_text, tokens, elapsed_ms
        
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error("[PrismAgentCaller] Call failed: %s", e, exc_info=True)
        
        try:
            publish_event(TelemetryEvent(
                ts=datetime.now(timezone.utc).isoformat(),
                cycle_id=cycle_id,
                ticker=ticker,
                kind="llm",
                source="prism",
                status="error",
                step="prism_agent_error",
                detail=str(e)
            ))
        except Exception as tel_err:
            logger.warning("Failed to publish telemetry error event: %s", tel_err)
            
        raise e

from enum import IntEnum
from dataclasses import dataclass

class Priority(IntEnum):
    HIGH = 0
    NORMAL = 1
    LOW = 2

@dataclass
class VLLMEndpoint:
    name: str
    url: str
    max_concurrent: int
    enabled: bool = True
    model: str | None = None
    cache_usage: float = 0.0
    requests_running: int = 0
    requests_waiting: int = 0
    last_model_sync: float = 0.0

#: Metric NAME → (VLLMEndpoint attribute, converter). Names are matched
#: EXACTLY against the token before '{' or whitespace — never by prefix.
#:
#: WHY EXACT. The old loop used line.startswith(name), and vLLM ships metric
#: FAMILIES that share a prefix: `vllm:num_requests_waiting` is followed by
#: `vllm:num_requests_waiting_by_reason{reason="capacity"}` and
#: `{reason="deferred"}`. All three matched, and the LAST line parsed —
#: deferred, which is 0 in steady state — overwrote the true queue depth on
#: every 5s poll. Measured live 2026-08-09: box waiting=17, controller read 0.
#: With waiting pinned at 0 the AdaptiveConcurrencyController's backpressure
#: clamp could never fire: the service sat at its MAX ceiling instead of
#: dropping to MIN while it — plus prism's own memory ops and scheduled
#: agents — piled 22 in flight against Gold Spark's 6 slots. Queued calls
#: waited 5+ minutes at zero bytes, prism's 300s idle watchdog killed them
#: ("Provider stream stalled"), and at 02:14 the same pile-up preceded the
#: box refusing TCP entirely for 80 minutes.
_VLLM_METRIC_MAP = {
    "vllm:gpu_cache_usage_perc": ("cache_usage", float),
    "vllm:kv_cache_usage_perc": ("cache_usage", float),
    "vllm_gpu_cache_usage_perc": ("cache_usage", float),
    "vllm:num_requests_running": ("requests_running", lambda v: int(float(v))),
    "vllm_num_requests_running": ("requests_running", lambda v: int(float(v))),
    "vllm:num_requests_waiting": ("requests_waiting", lambda v: int(float(v))),
    "vllm_num_requests_waiting": ("requests_waiting", lambda v: int(float(v))),
}


def parse_vllm_metrics(text: str) -> dict:
    """Parse a vLLM Prometheus /metrics payload into endpoint attributes.

    Returns only the attributes present in the payload, converted; a line
    whose value fails conversion is skipped rather than poisoning the rest.
    Line order must not matter — see _VLLM_METRIC_MAP for the incident where
    it did.
    """
    out: dict = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        # The metric NAME is everything before the label block or the value.
        name = line.split("{", 1)[0].split(None, 1)[0]
        entry = _VLLM_METRIC_MAP.get(name)
        if entry is None:
            continue
        attr, conv = entry
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[attr] = conv(parts[-1])
            except Exception:
                pass
    return out


class PrismLLMShim:
    """Shim class that mimics the old VLLM client interface."""
    def __init__(self):
        self._killed = False
        self.prism_client = prism_client
        self.model = None
        
        self._endpoints: dict[str, VLLMEndpoint] = {}
        
        # Load from config settings
        from app.config import settings
        if settings.PROVIDER_VLLM_1_URL:
            self._endpoints["jetson"] = VLLMEndpoint(
                name="jetson",
                url=settings.PROVIDER_VLLM_1_URL,
                max_concurrent=getattr(settings, "PROVIDER_VLLM_1_CONCURRENCY", 8),
                model=None
            )
        if settings.PROVIDER_VLLM_2_URL:
            self._endpoints["dgx_spark"] = VLLMEndpoint(
                name="dgx_spark",
                url=settings.PROVIDER_VLLM_2_URL,
                max_concurrent=getattr(settings, "PROVIDER_VLLM_2_CONCURRENCY", 16),
                model=None
            )
            
        self._metrics_task = None
        
    async def _sync_endpoint_model(self, ep: VLLMEndpoint, force: bool = False) -> str | None:
        import httpx
        import time
        if not ep or not getattr(ep, "url", None):
            return None
        now_time = time.monotonic()
        last_sync = getattr(ep, "last_model_sync", 0.0)
        if force or now_time - last_sync > 5.0:
            setattr(ep, "last_model_sync", now_time)
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    r = await client.get(f"{ep.url}/v1/models")
                    if r.status_code == 200:
                        data = r.json()
                        models = data.get("data", [])
                        if models:
                            new_model = models[0]["id"]
                            ep.model = new_model
            except Exception as e:
                logger.debug("[PrismLLMShim] Failed to sync model for %s: %s", ep.name, e)
        return getattr(ep, "model", None)

    def reset_kill_switch(self):
        self._killed = False
        
    async def abort_active_requests(self):
        self._killed = True
        
    async def chat(
        self,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 8192,
        enable_thinking: bool = False,
        priority: Priority = Priority.NORMAL,
        agent_name: str = "unknown",
        ticker: str = "",
        cycle_id: str = "",
        bot_id: str = "",
        model_override: str | None = None,
        endpoint_override: str | None = None,
        history: list[dict] | None = None,
        tools: list[dict] | None = None,
        actor_label: str | None = None,
        stream_callback: Any = None,
    ) -> tuple[str, int, int]:
        # No `images` parameter. One was declared here and never forwarded to
        # call_prism_agent, so a caller that passed images got a text-only
        # answer that read as a considered one. Removed 2026-08-10: a
        # parameter this method cannot honour is worse than its absence.
        import asyncio
        self.start_metrics_polling()
        if self._killed:
            raise asyncio.CancelledError("vLLM kill switch is armed — call reset_kill_switch() first")

        from app.services.adaptive_concurrency import concurrency_controller

        # Calculate estimated tokens
        est_tokens = (len(system or "") + len(user or "")) // 4
        for msg in (history or []):
            est_tokens += len(msg.get("content", "") or "") // 4

        priority_val = priority.value if hasattr(priority, "value") else int(priority)

        async with concurrency_controller.track(label=agent_name, tokens=est_tokens, priority=priority_val):
            return await call_prism_agent(
                agent_id="",
                user_message=user,
                fallback_system_prompt=system,
                fallback_agent_name=agent_name,
                priority=priority,
                temperature=temperature,
                max_tokens=max_tokens,
                ticker=ticker,
                cycle_id=cycle_id,
                bot_id=bot_id,
                actor_label=actor_label,
                model_override=model_override,
                endpoint_override=endpoint_override,
            )

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 8192,
        enable_thinking: bool = False,
        priority: Priority = Priority.NORMAL,
        agent_name: str = "unknown",
        ticker: str = "",
        cycle_id: str = "",
        bot_id: str = "",
        model_override: str | None = None,
        endpoint_override: str | None = None,
        stream_callback: Any = None,
    ) -> dict:
        import asyncio
        import time
        from app.services.adaptive_concurrency import concurrency_controller
        from app.config import settings

        if self._killed:
            raise asyncio.CancelledError("vLLM kill switch is armed — call reset_kill_switch() first")

        self.start_metrics_polling()

        # Estimate tokens of history/messages
        est_tokens = 0
        for msg in messages:
            est_tokens += len(msg.get("content", "") or "") // 4
            if "tool_calls" in msg and msg["tool_calls"]:
                est_tokens += len(str(msg["tool_calls"])) // 4

        start = time.monotonic()
        priority_val = priority.value if hasattr(priority, "value") else int(priority)

        async with concurrency_controller.track(label=agent_name, tokens=est_tokens, priority=priority_val):
            default_model, default_provider = await resolve_default_model_for_agent(
                agent_name, endpoint_override=endpoint_override
            )
            model = model_override or default_model
            
            if model_override:
                name_lower = model_override.lower()
                if "gpt-" in name_lower:
                    provider = "openai"
                elif "claude-" in name_lower:
                    provider = "anthropic"
                elif "gemini-" in name_lower:
                    provider = "google"
                else:
                    provider = default_provider
            else:
                provider = default_provider

            from app.v3.guardrails import get_budget_for_role
            max_iter = get_budget_for_role(agent_name).max_turns

            final_max_tokens = max_tokens or 8192
            if final_max_tokens >= 4096:
                # Floor at 4096: Prism's ContextExhaustionGuard rejects any
                # smaller output budget outright (no provider call is made).
                final_max_tokens = max(4096, final_max_tokens - est_tokens - 100)

            bench_task = f"{agent_name}:{ticker}" if ticker else agent_name
            resp = await self.prism_client.call_agent(
                model=model,
                messages=messages,
                system_prompt="",
                agent_name=agent_name,
                tools=tools,
                max_tokens=final_max_tokens,
                temperature=temperature,
                project=settings.PROJECT_NAME,
                max_iterations=max_iter,
                provider=provider,
                thinking_enabled=False,
                bench_task=bench_task,
                **min_p_kwargs(provider, model),
            )

            try:
                payload_json = resp.json()
                # text is null (not missing) on textless turns — don't let the
                # raw envelope leak through as the response.
                response_text = (payload_json.get("text") or "").strip()
                response_text, _leaked = strip_reasoning_leak(response_text, agent_name)
                # Prism emits camelCase toolCalls with {id, name, args} items;
                # normalize to the OpenAI {function:{name, arguments}} shape
                # the client-side tool loop (registry.execute_tool_call) expects.
                raw_tool_calls = payload_json.get("toolCalls") or payload_json.get("tool_calls") or []
                tool_calls = []
                for tc in raw_tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    if "function" in tc:
                        tool_calls.append(tc)
                    else:
                        tool_calls.append({
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": json.dumps(tc.get("args") or {}),
                            },
                        })
            except Exception as parse_err:
                logger.error("[PrismAgentCaller] chat_with_tools: response body was not JSON (%s) — returning empty text", parse_err)
                response_text = ""
                tool_calls = []
                payload_json = {}

            elapsed_ms = int((time.monotonic() - start) * 1000)
            total_tokens = _extract_token_usage(resp, response_text)

            return {
                "text": response_text,
                "total_tokens": total_tokens,
                "elapsed_ms": elapsed_ms,
                "tool_calls": tool_calls,
                # Prism's server-side resolved model, not an echo of the
                # request — the value a per-model scorecard must attribute to.
                "model_used": payload_json.get("model"),
                "provider": payload_json.get("provider"),
            }
        
    async def stream_prism_agent(self, payload: dict):
        """Pass-through streaming for UI OmniChat."""
        import asyncio
        self.start_metrics_polling()
        if self._killed:
            raise asyncio.CancelledError("vLLM kill switch is armed")

        client = await self.prism_client._get_client()
        url = f"{self.prism_client.url}/agent"
        headers = {
            "Content-Type": "application/json",
            "x-project": payload.get("project", "vllm-trading-bot"),
            "x-username": payload.get("username", "omni_chat"),
        }
        try:
            async with client.stream("POST", url, json=payload, headers=headers, timeout=180.0) as response:
                if response.status_code != 200:
                    err = await response.aread()
                    raise RuntimeError(f"Prism HTTP {response.status_code}: {err.decode('utf-8')}")
                async for line in response.aiter_lines():
                    if line:
                        yield line + "\n"
        except Exception as e:
            logger.error("[PRISM] stream_prism_agent error: %s", e)
            yield f"data: {{\"type\": \"error\", \"message\": \"{str(e)}\"}}\n\n"

    def start_metrics_polling(self):
        if self._metrics_task is None or self._metrics_task.done():
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                self._metrics_task = loop.create_task(self._poll_all_metrics())
                logger.info("[PrismLLMShim] Started background metrics polling for vLLM endpoints.")
            except RuntimeError:
                pass

    async def _poll_all_metrics(self):
        import httpx
        import asyncio
        while True:
            for ep in self._endpoints.values():
                if not ep.enabled or not ep.url:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=3.0) as client:
                        r = await client.get(f"{ep.url}/metrics")
                        if r.status_code == 200:
                            # Reset values before parsing new ones
                            ep.requests_running = 0
                            ep.requests_waiting = 0
                            for attr, value in parse_vllm_metrics(r.text).items():
                                setattr(ep, attr, value)
                except Exception as e:
                    logger.debug("[PrismLLMShim] Failed to poll metrics from %s: %s", ep.name, e)
            await asyncio.sleep(5.0)

    async def close(self):
        if self._metrics_task and not self._metrics_task.done():
            self._metrics_task.cancel()

llm = PrismLLMShim()
