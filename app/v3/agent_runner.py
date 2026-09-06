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
import functools
import json
import logging
import time
from typing import Any

from app.v3.shared_desk import SharedDesk, PhaseOutcome, DecisionProvenance
# NOTE: the live turn budget is get_agent_budget_turns (tool_whitelists), not
# guardrails.get_budget_for_role — that one only serves the non-V3 prism path.
from app.v3.guardrails import (
    enter_v3_session,
    exit_v3_session,
)
from app.utils.text_utils import sanitize_ascii
from app.v3.artifacts import (
    ARTIFACT_SCHEMAS,
    normalize_signal_weights,
    validate_artifact,
)
from app.v3.output_rules import (
    CANCELLED as REASON_CANCELLED,
    NON_LATIN_PROSE_THRESHOLD,
    prose_script_share,
    FAILURE_REASONS,
    RUNNER_EXCEPTION,
    RETRY_BUDGET_EXHAUSTED,
    SCHEMA_INVALID,
    TIMEOUT as REASON_TIMEOUT,
    UNCLASSIFIED,
    classify_output,
    record_rule_firing,
)
from app.v3.telemetry import sanitize_error_message
from app.v3.quality_scorer import score_artifact

logger = logging.getLogger(__name__)

# Tool-playbook tips cache: (agent_name -> (tips, fetched_monotonic)).
_PLAYBOOK_CACHE: dict[str, tuple[str, float]] = {}
_PLAYBOOK_TTL_SEC = 3600.0
_PLAYBOOK_MAX_CHARS = 2000


_REQUESTED_MAX_TOKENS = 8192

# The content-bearing fields of each research artifact — what a desk actually
# reads. An artifact that kept NONE of these has collapsed and is a failed run
# regardless of which required keys survived (2026-07-26 audit). Deliberately
# EXCLUDES routing/enum fields like desk_note.triage_recommendation, which is
# schema-required but absent in 65% of healthy production desk_notes.
_SUBSTANTIVE_FIELDS: dict[str, tuple[str, ...]] = {
    "desk_note": ("summary", "key_findings"),
    "fundamental_report": ("summary", "pillars"),
    "quant_report": ("summary", "risk_metrics"),
    "delta_report": ("summary",),
}


#: Keys that belong to the `emit_structured_output` envelope rather than to any
#: artifact — the tool's own request shape (`schema`/`data`/`label`), the
#: `/compute/synthetic-output` response shape (`acknowledged`/`_synthetic`/
#: `validationWarnings`), and the double-encoded `arguments` wrapper the
#: provider adapter sometimes leaves in place.
_ENVELOPE_KEYS = frozenset({
    "schema", "data", "label", "arguments",
    "acknowledged", "_synthetic", "validationWarnings",
})


def _unwrap_structured_output(
    parsed: dict, artifact_type: str, agent_name: str
) -> dict:
    """Return the artifact from inside an `emit_structured_output` envelope.

    `emit_structured_output` is NOT on any v3 whitelist, but Prism offers it to
    the personas anyway (the ToolCanary logs an OFF-WHITELIST warning every
    time), and the models reach for it as the natural way to emit a typed
    artifact. Its request shape wraps the real payload:

        {"schema": {...}, "label": "desk_note", "data": {<the artifact>}}

    That parses as clean JSON, so the unparseable-output repair pass never
    fires — but every required field is now one level down, so
    `validate_artifact` reports them all missing and `_artifact_collapsed`
    calls it a total collapse. Measured on cycle-v3-1785792600 (PLTR,
    2026-08-03): the junior and quant analysts each lost a complete, correct
    artifact this way and were retried from scratch, ~185s of the cycle spent
    re-deriving research that had already been produced.

    FAIL-CLOSED. Unwrapping only happens when the top level carries `data` and
    *nothing that is not envelope furniture*. A real artifact that merely has a
    `data` field alongside its own keys is left exactly as it was, so this can
    only ever recover a run that would otherwise have been thrown away.
    """
    for _ in range(2):  # `arguments` may wrap the envelope one extra level
        if not isinstance(parsed, dict) or not parsed:
            return parsed
        extra = set(parsed) - _ENVELOPE_KEYS
        if extra:
            return parsed

        # `{"arguments": "{\"data\": ...}"}` — the provider adapter passed the
        # OpenAI-style function-call envelope through without decoding it.
        inner = parsed.get("arguments")
        if isinstance(inner, str) and "data" not in parsed:
            try:
                decoded = json.loads(inner)
            except (ValueError, TypeError):
                return parsed
            if not isinstance(decoded, dict):
                return parsed
            parsed = decoded
            continue

        payload = parsed.get("data")
        # The model stringified its own payload — the same defect that makes
        # the tool itself reject the call with "'data' ... must be an object".
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                return parsed
        if not isinstance(payload, dict) or not payload:
            return parsed

        logger.warning(
            "[V3Runner] %s wrapped its %s in an emit_structured_output "
            "envelope (keys=%s) — unwrapped `data` (%d fields). The artifact "
            "was intact; only the envelope was wrong.",
            agent_name, artifact_type, sorted(parsed), len(payload),
        )
        return payload

    return parsed


def _is_wrong_shape(artifact_type: str, artifact: dict) -> bool:
    """True when a parsed dict carries NOTHING the artifact is made of.

    This is the fragment detector, and it is deliberately independent of the
    SDK: `lazycat-sdk` is BIND-MOUNTED into this container, so a trading-service
    deploy never ships a fix made over there. The guard has to hold on its own.

    A JSON extractor that walks into a malformed outer object hands back one of
    its nested blocks — the `metrics` dict off a fundamental_report, a single
    `overlays` entry off a quant_report. Those parse cleanly, so the tool-less
    repair pass never fires; the run instead reaches schema validation, is
    correctly called a collapse, and burns a full ~100s tool-enabled re-run to
    recover what was a PARSE failure all along.

    Deliberately the weakest possible test — not one required or substantive
    field present. Anything that kept even one is a real, if degraded, artifact
    and belongs to `_artifact_collapsed` and the branches below, which already
    grade it. Unknown artifact types are never narrowed.
    """
    if not isinstance(artifact, dict) or not artifact:
        return False
    schema = ARTIFACT_SCHEMAS.get(artifact_type)
    if not schema:
        return False
    known = set(schema.get("required", ())) | set(
        _SUBSTANTIVE_FIELDS.get(artifact_type, ())
    )
    if not known:
        return False
    return not (known & set(artifact))


def _artifact_collapsed(artifact_type: str, artifact: dict) -> bool:
    """True when a research artifact kept NONE of its content-bearing fields.

    Empty string, empty dict/list, and None all count as absent — a summary of
    "" is no more use to the desk than a missing key. Unknown artifact types
    are never treated as collapsed, so this can only ever narrow behaviour.

    Replayed over 2263 stored production artifacts: fires on 3-7% per type
    (all genuine wrong-schema emissions), versus the 65% that would have
    tripped a naive any-missing-required-field gate.
    """
    substantive = _SUBSTANTIVE_FIELDS.get(artifact_type, ())
    if not substantive or not isinstance(artifact, dict):
        return False
    for field in substantive:
        value = artifact.get(field)
        if isinstance(value, (dict, list)):
            if value:
                return False
        elif str(value or "").strip():
            return False
    return True


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


def apply_signal_weights_policy(
    artifact: dict,
    *,
    artifact_type: str,
    agent_name: str = "",
    ticker: str = "",
) -> bool:
    """Coerce a decision's `signal_weights` to the canonical vector, in place.

    Returns True when the artifact now carries a valid vector, False when it was
    deliberately left alone — which the caller turns into a schema error.

    Before this existed the runner only acted when `signal_weights` was ABSENT,
    substituting the equalised default. A vector that was PRESENT but malformed
    went straight through: an executed ZS BUY (cycle-v3-1788646388) persisted
    `{'board':.45,'quant':.25,'specific':0,'fundamental':.15,'debate':0,
    'board_dup':0}` — two invented keys summing to 0.85 — because nothing on the
    path looked at the shape. See `normalize_signal_weights` for the measured
    background.

    The "incomplete decision is not salvaged" rule is PRESERVED exactly: a
    decision missing action/confidence/reasoning is a failed run, and inventing
    weights for it would let it past the missing-required-fields branch that
    exists to engage the circuit breaker's retry.
    """
    if artifact_type != "trade_decision" or not isinstance(artifact, dict):
        return False

    raw = artifact.get("signal_weights")
    supplied = isinstance(raw, dict) and bool(raw)

    if not supplied:
        complete = (
            artifact.get("action")
            and artifact.get("confidence") is not None
            and str(artifact.get("reasoning") or "").strip()
        )
        if not complete:
            return False

    weights, source = normalize_signal_weights(raw)
    artifact["signal_weights"] = weights
    artifact["signal_weights_source"] = source

    if source == "default_equalized":
        logger.warning(
            "[V3Runner] %s: trade_decision for %s carried no usable "
            "signal_weights (%r) — equalized default, stamped "
            "signal_weights_source=default_equalized",
            agent_name, ticker, raw,
        )
    elif source == "model_normalized":
        logger.warning(
            "[V3Runner] %s: trade_decision for %s emitted malformed "
            "signal_weights %r (sum=%.4f) — renormalized to %r",
            agent_name, ticker, raw,
            sum(v for v in raw.values() if isinstance(v, (int, float))
                and not isinstance(v, bool)),
            weights,
        )
    return True


def guard_unshortable_sell(artifact: dict, *, desk: Any, bot_id: str = "") -> dict:
    """Coerce a SELL the bot cannot place, on ANY path that decides.

    Extracted from run_v3_agent so the delta (energy-saver) tier can reuse it.
    The delta tier wrote `final_decision` directly and returned before Layer 6,
    so this guard — and every policy gate — was skipped on the highest-volume
    route in the pipeline (2026-07-25 audit).

    A MISSING `held` key is NOT treated as "not held": that would coerce real
    exits on any cycle whose portfolio lookup failed. Only an affirmative
    `held is False`, re-confirmed against the live book, suppresses a SELL.
    """
    if not isinstance(artifact, dict):
        return artifact
    if str(artifact.get("action") or "").upper() != "SELL":
        return artifact
    if desk.cycle_metadata.get("held") is not False:
        return artifact

    # Re-check against the live book before suppressing an exit. `held` is
    # computed once at desk build; when bot_id resolution was broken it read
    # False for EVERY ticker, including ones the desk genuinely owned
    # (2026-07-24). Coercing on that stale flag alone would have converted real
    # exits into HOLDs — the one failure mode of this guard that costs money.
    really_held = None
    try:
        from app.tools.portfolio_tools import get_position_context
        really_held = bool(get_position_context(desk.ticker, bot_id).get("held"))
    except Exception as pos_err:  # noqa: BLE001
        logger.warning(
            "[V3Runner] %s: live position re-check failed (%s) — leaving the "
            "SELL intact rather than risk suppressing a real exit",
            desk.ticker, pos_err,
        )
    if really_held is False:
        from app.v3.artifact_validators import coerce_unshortable_sell
        return coerce_unshortable_sell(
            artifact, held=False, ticker=desk.ticker, cycle_id=desk.cycle_id,
        )
    if really_held:
        logger.error(
            "[V3Runner] %s: desk metadata said held=False but the bot DOES "
            "hold it — SELL preserved; bot_id resolution is wrong", desk.ticker,
        )
    return artifact


def _get_tool_playbook_tips(agent_name: str, limit: int = 3) -> str:
    """Compact per-agent tool guidance from the eval layer's tool_playbook."""
    cached = _PLAYBOOK_CACHE.get(agent_name)
    if cached and (time.monotonic() - cached[1]) < _PLAYBOOK_TTL_SEC:
        return cached[0]
    tips = ""
    try:
        from app.db import mongo_store
        docs = mongo_store.find_docs(
            "tool_playbook",
            {"agent_role": agent_name},
            sort=[("last_validated_at", -1), ("created_at", -1)],
            limit=limit,
        )
        seen_seq = set()
        unique_tips = []
        for d in docs:
            seq = d.get("recommended_tool_sequence")
            if seq and seq not in seen_seq:
                seen_seq.add(seq)
                unique_tips.append(f"- {seq}")
        tips = "\n".join(unique_tips)
    except Exception as e:  # noqa: BLE001 — advisory context, never blocks the agent
        logger.debug("[V3Runner] tool_playbook fetch failed: %s", e)
    # Belt-and-braces cap. This is advisory context appended to every agent
    # prompt; on 2026-08-05 an unbounded version of this injection reached
    # 131k chars and prism rejected the request outright ("0 output tokens of
    # a 0 token window"), taking down every discovery cycle. Advisory text
    # must never be able to cost a cycle, however the table misbehaves.
    if len(tips) > _PLAYBOOK_MAX_CHARS:
        logger.warning(
            "[V3Runner] tool_playbook tips for %s were %d chars — truncated to %d",
            agent_name, len(tips), _PLAYBOOK_MAX_CHARS,
        )
        tips = tips[:_PLAYBOOK_MAX_CHARS].rsplit("\n", 1)[0]
    _PLAYBOOK_CACHE[agent_name] = (tips, time.monotonic())
    return tips


def _fallback_overlays_from_metrics(artifact: dict) -> list:
    """Best-effort overlays when the agent's JSON omitted them.

    Uses the fields the quant report always carries — a stop-loss level and
    thesis direction — so the chart still shows at least one meaningful line
    rather than falling back to a bare candlestick with no analysis.
    """
    overlays: list = []
    stop = artifact.get("stop_loss_suggestion")
    try:
        stop = float(stop)
    except (TypeError, ValueError):
        stop = None
    if stop:
        direction = str(artifact.get("thesis_direction", "")).upper()
        # A stop below entry (long thesis) is support; above (short) is resistance.
        ov_type = "resistance" if direction == "BEARISH" else "support"
        overlays.append({
            "type": ov_type,
            "y0": stop,
            "y1": stop,
            "reasoning": "Suggested stop-loss level",
        })
    return overlays


async def _persist_quant_chart(ticker: str, artifact: dict) -> None:
    """Write the quant analyst's overlays to the AI Analysis Overlays chart.

    Called after the artifact is parsed so chart generation no longer depends
    on the model remembering to tool-call save_trading_chart mid-loop.
    """
    overlays = artifact.get("overlays")
    if not isinstance(overlays, list) or not overlays:
        overlays = _fallback_overlays_from_metrics(artifact)
    if not overlays:
        logger.info("[V3Runner] %s: no overlays to chart (skipping)", ticker)
        return

    from app.tools.charting_tools import save_trading_chart

    confidence = artifact.get("confidence")
    result = await asyncio.wait_for(
        save_trading_chart(
            ticker=ticker,
            overlays=overlays,
            period="1y",
            analysis=str(artifact.get("summary", "")),
            strategy_name="Quant/Risk Technical Analysis",
            confidence=str(confidence) if confidence is not None else "",
            reasoning=str(artifact.get("position_sizing_note", "")),
        ),
        timeout=45,
    )
    logger.info("[V3Runner] %s: persisted %d chart overlays (%s)", ticker, len(overlays), result[:60] if isinstance(result, str) else result)


async def _persist_quant_signals(desk: Any, cycle_id: str, artifact: dict) -> None:
    """Post the quant's `signals` section to the whiteboard from its artifact.

    The prompt called this write MANDATORY ("a run with zero whiteboard writes
    is incomplete") and it happened in 9 of 56 runs — because the agent emits
    its final JSON on turn 1 in 84% of runs and never reaches the step. The
    debate and Board are supposed to argue over these levels, so the desk was
    usually missing the only numbers it has.

    Same fix as the chart overlays: derive it from the artifact, which the
    model reliably fills, instead of depending on a mid-loop tool call.
    """
    metrics = artifact.get("risk_metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}

    content = {
        "thesis_direction": artifact.get("thesis_direction"),
        "confidence": artifact.get("confidence"),
        "rsi": metrics.get("rsi"),
        "atr": metrics.get("atr"),
        "volatility_regime": metrics.get("volatility_regime"),
        "vol_signal": metrics.get("vol_signal"),
        "sma_200_status": metrics.get("sma_200_status"),
        "bollinger_position": metrics.get("bollinger_position"),
        "stop_loss_suggestion": artifact.get("stop_loss_suggestion"),
        "hrp_weight_suggestion": artifact.get("hrp_weight_suggestion"),
        "position_sizing_note": artifact.get("position_sizing_note"),
        "open_questions": artifact.get("sub_analyses_requested") or [],
    }
    content = {k: v for k, v in content.items() if v not in (None, "", [])}
    if not content:
        return

    # `confidence` and `thesis_direction` alone are not signals — they are the
    # agent's opinion of itself with none of the evidence behind it. Measured
    # 2026-07-29: 53 of 326 quant whiteboard writes (16%) were under 60 chars,
    # and one was literally `{'confidence': 65}`. The quant is the ONLY agent
    # that does this; every other author is at 0/326.
    #
    # That stub is worse than no post: `signals` is the section teammates are
    # told to annotate ("read a teammate's section — desk_note or signals"), so
    # an 18-character entry occupies the slot and looks like data while giving
    # the fundamental analyst nothing to agree or disagree with. The
    # collaboration silently loses its substrate.
    #
    # Skipping leaves the section absent, which is legible: a missing section
    # reads as "the quant had nothing", an empty one reads as "the quant said
    # almost nothing", and only the first is true.
    _SELF_REPORT = {"confidence", "thesis_direction"}
    if not (set(content) - _SELF_REPORT):
        logger.warning(
            "[V3Runner] %s: quant signals carried no evidence fields "
            "(only %s) — not posting a stub to the whiteboard",
            desk.ticker, sorted(content),
        )
        return

    from app.agents.whiteboard import whiteboard

    await whiteboard.write_section(
        ticker=desk.ticker,
        cycle_id=cycle_id,
        section="signals",
        content=content,
        author_agent="v3_quant_analyst",
    )


async def _persist_junior_market_context(desk: Any, cycle_id: str, artifact: dict) -> None:
    """Post the junior's `market_context` section from its artifact, if absent.

    Same failure shape as the quant's `signals` (see _persist_quant_signals):
    the prompt calls the whiteboard write MANDATORY, but a model that emits its
    final JSON without taking the tool step simply never writes it. Measured
    2026-08-26: 0/48 desks before 08-19, 54/62 (87%) since — a residual ~1-in-8
    miss on the one section every downstream desk is told to read.

    Unlike the quant hook this one is a FALLBACK, not the primary writer: when
    the agent did make its write, posting again would supersede the agent's own
    words with a mechanical digest (write_section versions rather than
    duplicates), so an existing section wins and this returns without writing.

    The artifact reliably carries the promised content — `key_findings` is
    "your 2-3 load-bearing findings" almost verbatim — so the derivation is a
    selection, not a summary.
    """
    from app.agents.whiteboard import whiteboard

    existing = await whiteboard.get_section(desk.ticker, cycle_id, "market_context")
    if existing:
        return

    findings = [
        str(f).strip()
        for f in (artifact.get("key_findings") or [])
        if isinstance(f, (str, int, float)) and str(f).strip()
    ][:3]

    catalyst = artifact.get("catalyst_call") or {}
    catalyst_line = ""
    if isinstance(catalyst, dict) and catalyst.get("catalyst"):
        catalyst_line = (
            f"Catalyst call: {catalyst.get('direction') or 'UNSTATED'} "
            f"on {catalyst['catalyst']}"
        )
        if catalyst.get("already_priced_in"):
            catalyst_line += " (already priced in)"
        catalyst_line += "."

    # No stub posts — an absent section reads as "the junior had nothing",
    # an empty one reads as data. Same doctrine as the quant's signals guard.
    if not findings and not catalyst_line:
        logger.warning(
            "[V3Runner] %s: junior artifact carried no key_findings or "
            "catalyst_call — not posting a market_context stub",
            desk.ticker,
        )
        return

    text = " ".join(findings)
    if catalyst_line:
        text = f"{text} {catalyst_line}".strip()

    await whiteboard.write_section(
        ticker=desk.ticker,
        cycle_id=cycle_id,
        section="market_context",
        content={"text": text, "derived_from_artifact": True},
        author_agent="v3_junior_analyst",
    )
    logger.info(
        "[V3Runner] %s: market_context derived from desk_note artifact "
        "(agent skipped its mandatory whiteboard write)",
        desk.ticker,
    )


def _scoped_to_the_agent(fn):
    """Open a (cycle, phase, ticker) scope around an agent run, and close it.

    Everything an agent does — its tool calls, its warnings, the errors raised
    beneath it — is attributed to the triple opened here. The scope CLOSES on
    the way out, so a nested run cannot outlive itself and blank the caller's
    context; that is the whole reason `tool_context()` exists alongside
    `set_tool_context()`.

    `phase` names the stage for telemetry and defaults to the agent's own
    name, which is right for every caller except the debate, where one agent
    argues in several stages — `bull_argument` and `bull_defense` are the same
    agent — so `_run_agent_with_circuit_breaker` passes the stage explicitly.

    A DECORATOR, not a wrapper function, deliberately: several tests read
    `inspect.getsource(run_v3_agent)` to pin invariants about the body (the
    quant persistence guard, for one), and `getsource` follows the
    `__wrapped__` that `functools.wraps` sets. Splitting the body into a
    differently-named inner function instead would have left those guards
    silently inspecting an 18-line wrapper and passing on nothing.
    """
    @functools.wraps(fn)
    async def _wrapper(desk, agent_module, *, phase: str = "", **kwargs):
        from app.tools.tool_context import tool_context

        agent_name = getattr(agent_module, "AGENT_NAME", None)
        with tool_context(
            agent_name=agent_name,
            cycle_id=kwargs.get("cycle_id") or "",
            ticker=getattr(desk, "ticker", None),
            phase=phase or agent_name,
        ):
            return await fn(desk, agent_module, **kwargs)

    return _wrapper


@_scoped_to_the_agent
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
    is_retry: bool = False,
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
        is_retry: True when the circuit breaker is re-running this phase after
            a failure. A malformed ANALYST artifact returns AGENT_ERROR on the
            first attempt (to earn the retry) but degrades to DATA_GAP on the
            retry, so a model that reliably emits the wrong shape does not
            trip the breaker and abort the whole desk.

    Returns:
        PhaseOutcome indicating success or failure type.
    """
    # In-process tool execution (whiteboard, peer requests) is already scoped
    # by `run_v3_agent` above — agent, cycle, ticker AND phase, with teardown.
    # This used to call set_tool_context() here with agent + cycle only, which
    # is why `tool_usage_stats.ticker` was NULL on the in-process path and
    # every execution_errors row read phase='unknown'.
    # The HTTP bridge path sets the same context from request headers.
    from app.utils.pipeline_utils import noop as _noop
    if emit is None:
        emit = _noop

    agent_name = agent_module.AGENT_NAME
    artifact_type = agent_module.ARTIFACT_TYPE

    # The only attempt identity this function can honestly report. The retry
    # itself lives in the orchestrator's circuit breaker
    # (`_run_agent_with_circuit_breaker`), which re-calls this function with
    # is_retry=True; it retries at most once, so the domain really is {1, 2}.
    # Derived here rather than passed in, so the caller cannot disagree with
    # the flag that already drives the AGENT_ERROR→DATA_GAP degrade below.
    attempt_no = 2 if is_retry else 1

    # Declared before the try below so every failure handler can read it even
    # when the failure lands before the run_agent call is reached.
    _cost_sink: dict = {"tokens": 0, "loops": 0, "tool_calls": 0}

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

        # Precomputed quant math — scoped to the two agents that size trades.
        # Injected because telemetry shows they rarely make the tool calls the
        # prompt asks for (quant avg 1.6 loops, board 1.0) — the math must
        # already be on their desk, not behind a tool call.
        # 2026-07-28: the synthesizer joined this list. It issues the FINAL
        # action — it downgraded 21 of 41 Board BUYs to HOLD — and it was the
        # least-informed agent on the desk, deciding from summarised prose
        # while every block the reconcile passes enforce went to other agents.
        if agent_name in ("v3_quant_analyst", "v3_board_of_directors",
                          "v3_decision_synthesizer"):
            quant_math = desk.cycle_metadata.get("quant_math_context", "")
            if quant_math:
                dynamic_sections.append((_KEEP, quant_math))

        # Verified indicator values — never shed: these are the numbers the
        # agent would otherwise invent (measured 2026-07-24), and they are
        # what the reconciliation pass will enforce on its artifact anyway.
        #
        # 2026-08-23: the four debate agents joined this list. The decision
        # audit (ch.90) replayed 592 desks and found the debaters and the
        # judge received NO verified numeric block at all — the judge was
        # instructed to "check claims against the facts" with no facts — and
        # 1 in 7 checkable numbers in stored debate prose matched nothing on
        # the desk. FACT blocks widen to the debate; the composite score
        # (a precomputed verdict) deliberately does not — see below.
        if agent_name in (
            "v3_quant_analyst", "v3_board_of_directors",
            "v3_decision_synthesizer",
            "v3_bull_agent", "v3_bear_agent",
            "v3_bull_defense", "v3_debate_judge",
        ):
            tech_baseline = desk.cycle_metadata.get("technical_baseline_context", "")
            if tech_baseline:
                dynamic_sections.append((_KEEP, tech_baseline))

        # Book/portfolio risk context: the deciders, plus the two debate seats
        # that weigh the book side — the bear (exit/avoid theses need the
        # position and concentration facts) and the judge (grades those
        # claims). NOT the bull/defense: their job is the case for one name,
        # not portfolio construction.
        if agent_name in (
            "v3_quant_analyst", "v3_board_of_directors",
            "v3_decision_synthesizer",
            "v3_bear_agent", "v3_debate_judge",
        ):
            book_brief = desk.cycle_metadata.get("book_brief_context", "")
            if book_brief:
                dynamic_sections.append((_KEEP, book_brief))

        # Precomputed fundamental ratios — scoped to the desk that judges the
        # business, and to the two agents that decide. Never shed, same reason
        # as the technical baseline.
        #
        # 2026-07-28: the fundamental analyst had NO numeric fields to
        # reconcile, and fundamentals reached the deciders as prose while
        # technicals reached them as numbers. Measured over 41 Board BUYs, the
        # synthesizer's overrides cited oscillators more (stochastic +27.1pp)
        # and fundamentals less (eps -21.2pp, margin -16.9pp) — it was weighing
        # three decimals against an adjective.
        if agent_name in (
            "v3_fundamental_analyst",
            "v3_board_of_directors",
            "v3_decision_synthesizer",
            "v3_bull_agent", "v3_bear_agent",
            "v3_bull_defense", "v3_debate_judge",
        ):
            fundamental = desk.cycle_metadata.get("fundamental_context", "")
            if fundamental:
                dynamic_sections.append((_KEEP, fundamental))

        # Deterministic baseline score (2026-08-05) — the two agents that issue
        # an action, plus the quant desk that owns the risk numbers. Never
        # shed: it is the only place on the desk where a composite, a computed
        # risk/reward and the structural gates appear at all, and the failure
        # it addresses (every decision landing below the confidence floor) is
        # precisely a failure of the deciding step.
        #
        # NOT given to the analysts, and NOT to the arguing debate seats
        # (bull/bear/defense). They are meant to reach an independent read;
        # handing every desk the same precomputed verdict would collapse the
        # disagreement the board is supposed to weigh, and the block's own
        # confidence term rewards fundamental/technical agreement — which would
        # then be measuring an echo. The JUDGE is the exception (2026-08-23):
        # it grades claims rather than making a case, and the composite's
        # structural gates and risk/reward are grading criteria, not a side.
        if agent_name in (
            "v3_quant_analyst",
            "v3_board_of_directors",
            "v3_decision_synthesizer",
            "v3_debate_judge",
        ):
            score_block = desk.cycle_metadata.get("decision_score_context", "")
            if score_block:
                dynamic_sections.append((_KEEP, score_block))

        # THE OTHER NAMES THIS CYCLE (app/v3/cycle_candidates.py).
        #
        # Given to the BEAR and to the deciding agents, and to nobody else.
        #
        # The bear, because on a long-only one-position book a bear thesis has
        # no executable form: "do not own this" and "own this" are the only two
        # readings, so every bear case lands as HOLD however well argued. 273 of
        # 333 HOLDs in 30 days were the agent's own verdict before any gate ran.
        # Giving the bear the alternatives turns an absolute question it answers
        # cheaply ("is this a buy?" — no) into a relative one it cannot ("would
        # you rather own one of these?").
        #
        # The deciders, because a preference is only actionable if whoever
        # writes the action can see what it is a preference FOR.
        #
        # NOT the bull, and not the analysts. Same reasoning as the score block
        # above: they are meant to reach an independent read of THIS name, and
        # a bull handed a list of rivals is being invited to argue relatively
        # when its job is to make the strongest case for one thing.
        if agent_name in (
            "v3_bear_agent",
            "v3_board_of_directors",
            "v3_decision_synthesizer",
        ):
            candidate_block = desk.cycle_metadata.get("cycle_candidates_context", "")
            if candidate_block:
                dynamic_sections.append((_KEEP, candidate_block))

        # THE BEAR'S ANSWER to that list (app/v3/substitute.py), to the two
        # deciding agents only. NEVER SHED: a preference is only actionable if
        # whoever writes the action can see what it is a preference FOR, and
        # the bear's own artifact reaches the board as a prose summary in which
        # a single ticker is easy to lose. It renders "" until the bear has
        # run, and "" for a declension — see `substitute_block` for why an
        # honest "none is better" must NOT be reported as corroboration.
        if agent_name in ("v3_board_of_directors", "v3_decision_synthesizer"):
            from app.v3.substitute import substitute_context

            sub_block = substitute_context(desk)
            if sub_block:
                dynamic_sections.append((_KEEP, sub_block))

        # The framed propositions for THIS desk (app/v3/debate_frame.py), to
        # every participant in the debate INCLUDING the judge — a judge ruling
        # on different questions than the debaters argued is worse than no
        # framing at all. Never shed: without it these agents fall back to the
        # generic "is this a buy" debate this module exists to replace.
        #
        # 2026-08-23 (ch.90): the two DECIDERS joined. They followed the
        # debate's verdict (the only input the action measurably tracked)
        # while never seeing the propositions it was framed around — 0 of 245
        # desks delivered the frame to the board in the replay audit.
        if agent_name in (
            "v3_bull_agent",
            "v3_bear_agent",
            "v3_bull_defense",
            "v3_debate_judge",
            "v3_board_of_directors",
            "v3_decision_synthesizer",
        ):
            frame_block = desk.cycle_metadata.get("debate_frame_context", "")
            if frame_block:
                dynamic_sections.append((_KEEP, frame_block))

        # Cross-desk dissent — ONLY the two agents that issue an action, and
        # never shed. It is the one block whose absence changes what the policy
        # layer does: HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT holds any BUY/SELL
        # that does not answer it, so shedding this would hold a trade for a
        # disagreement the agent was never shown.
        #
        # Replaces a post-hoc cap that rewrote `confidence` to 60 after the
        # desk had already decided (see contradiction_shadow.build_dissent_block).
        if agent_name in ("v3_board_of_directors", "v3_decision_synthesizer"):
            dissent = desk.cycle_metadata.get("dissent_context", "")
            if dissent:
                dynamic_sections.append((_KEEP, dissent))

        # Precomputed valuation math — scoped to the desk that judges price and
        # the board that sizes the trade. Never shed, for the same reason as the
        # technical baseline: these are the numbers the reconciliation pass will
        # enforce on the artifact anyway, so dropping them from the prompt only
        # guarantees a disagreement to correct afterwards.
        if agent_name in ("v3_valuation_analyst", "v3_board_of_directors",
                          "v3_decision_synthesizer",
                          "v3_bull_agent", "v3_bear_agent",
                          "v3_bull_defense", "v3_debate_judge"):
            valuation = desk.cycle_metadata.get("valuation_context", "")
            if valuation:
                dynamic_sections.append((_KEEP, valuation))

        # Opinion cards go ONLY to the valuation desk, never to the Board.
        # The Board sizes and authorises the trade; handing it a named
        # investor's opinion invites deference to a personality instead of to
        # the desk's own evidence, and the Board makes ~1.0 tool calls so it
        # cannot check anything it is told. The valuation analyst is the one
        # agent holding the computed multiples to weigh it against.
        #
        # Shed-eligible (not _KEEP), unlike the valuation block: if the prompt
        # must be trimmed, an opinion is exactly what should go first.
        if agent_name == "v3_valuation_analyst":
            opinion = desk.cycle_metadata.get("opinion_context", "")
            if opinion:
                dynamic_sections.append((_KEEP + 40, opinion))

        # Alternative data — insider cluster buys, congressional disclosures,
        # social chatter.
        #
        # 2026-07-28: this went to two agents, and `get_congress_trades` was
        # never called in 30 days by anyone. The data is not missing — 30,483
        # congress rows across 508 tickers active in the last 90 days, and the
        # block renders for roughly half the tickers sampled. It simply never
        # reached the desks that size and authorise the trade.
        #
        # Widened rather than left to the tool, because six of ten agents
        # average loops_used = 1.00 — they emit their JSON on the first pass and
        # never take a tool turn, so a whitelist entry cannot reach them. A
        # block always can. Same reasoning that turned fundamentals from zero
        # numeric fields into 23 reconciled ones.
        if agent_name in (
            "v3_junior_analyst",
            "v3_fundamental_analyst",
            "v3_quant_analyst",
            "v3_valuation_analyst",
            "v3_board_of_directors",
            "v3_decision_synthesizer",
        ):
            alt_data = desk.cycle_metadata.get("alt_data_context", "")
            if alt_data:
                dynamic_sections.append((_KEEP, alt_data))

        # Market data briefing first — it's the shared factual base (plan 4.2)
        data_report = desk.cycle_metadata.get("data_report", "")
        if data_report:
            if len(data_report) > 5000:
                data_report = data_report[:5000] + "\n...[TRUNCATED FOR LENGTH]..."
            dynamic_sections.append((
                _KEEP,
                f"## MARKET DATA BRIEFING FOR THIS CYCLE\n{data_report}",
            ))

        # NEVER SHED (2026-07-24 audit). This section carries "NO OPEN POSITION
        # in X — the bot cannot SELL what it does not hold (no shorting)", which
        # is not context, it is a hard constraint on which actions are even
        # legal. At shed_order 2 it was among the FIRST things dropped when a
        # prompt overflowed Prism's 2048-token embedder — and 167 of 176 SELL
        # decisions (95%) were on tickers the bot did not hold, every one of
        # them policy-blocked after the desk had already spent ~1,243s on it.
        # Dropping a position constraint to save tokens is never the right
        # trade; shed memory or the whiteboard summary instead.
        portfolio_ctx = desk.cycle_metadata.get("portfolio_context", "")
        if portfolio_ctx:
            dynamic_sections.append((_KEEP, f"## Portfolio Context\n{portfolio_ctx}"))

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
            # for_agent_prompt=True drops the sections the SharedDesk already
            # delivers in its own _KEEP block above. Those duplicates were 87%
            # of this block and pushed the whiteboard's unique payload —
            # market_context, signals, and the annotations — off the end of the
            # 8,000-char cap on 93% of boards.
            wb_summary = await whiteboard.summarize(
                ticker=desk.ticker, cycle_id=cycle_id, for_agent_prompt=True
            )
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
        #
        # DIVISOR 3 IS KNOWN TO BE WRONG, AND IS KEPT DELIBERATELY.
        #
        # Measured 2026-08-09 by binary search against the live embedder, the
        # desk's dense JSON runs **1.88 chars/token**, not the ~2.5-3 this
        # comment used to assume — so the true budget is ~2,966 chars, not
        # 4,944, and blocks that pass this gate can still overflow the
        # embedder. `EmbeddingService.CHARS_PER_TOKEN` carries the measurement.
        #
        # Tightening it here was tried and REVERTED, because this gate does not
        # do what its name suggests. Overflow does not reject anything; it
        # routes the whole dynamic block into the SYSTEM prompt (see the
        # relocation branch below), which prism does not embed — and that
        # *skips KV-cache reuse*. Production prefix-cache hit rate is ~84% on a
        # box whose measured failure mode is prefill thrash, so making
        # relocation more frequent costs more than the overflow does.
        #
        # And the overflow now costs much less than when this guard was
        # written: the vllm-shim clamps and token-feedback-rescales oversized
        # embeddings (`lazy-agent-service@39f62f6`), so an overflowing embed is
        # truncated rather than rejected. The failure this defended against —
        # "prism stores nothing" — has been fixed at the seam that can actually
        # measure the overflow.
        #
        # So: an honest number here would make things worse. Revisit only with
        # a measurement of relocation frequency against cache hit rate.
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
            # only place left that Prism does not embed — and since it is not
            # embedded, the shed sections cost nothing here: restore them.
            # Before this, every decision agent (KEEP core ~21k chars) shed the
            # whiteboard summary — the carrier of final_decision — and then
            # routed to the system prompt anyway, so the Board/synthesizer ran
            # without the whiteboard on 100% of 08-04's decision builds while
            # the shed bought no embed relief at all.
            if prompt_split and not _fits_embedder and shed:
                dynamic_block = "\n\n".join(t for _, t in dynamic_sections)
            system_prompt += "\n\n" + dynamic_block
            if prompt_split and not _fits_embedder:
                logger.warning(
                    "[V3Runner] %s: non-sheddable context (~%d tok) exceeds "
                    "Prism's %d-token memory embedder — routing FULL dynamic "
                    "block to system prompt (%d shed section(s) restored, "
                    "KV-cache reuse skipped).",
                    agent_name, (_fixed_chars + len(dynamic_block)) // 3,
                    _EMBED_TOKEN_LIMIT, len(shed),
                )

        if tool_whitelist:
            # State the turn budget as a NUMBER the agent can count against.
            #
            # The failure this addresses is narration, not slowness: an agent
            # spends its last turn writing "I'll now complete the analysis and
            # emit the desk_note JSON" instead of emitting it. The harness then
            # returns that announcement as `final_text`, parsing fails, and the
            # whole run's research is spent — the salvage pass at the bottom of
            # this function exists entirely to claw those back.
            #
            # Scope note: this is cheap insurance, not a fix for a large loss.
            # Measured over all 2,412 recorded analyst runs, non-SUCCESS is
            # 4.3% (2.3-7.6% per agent) — NOT the 22-36% that motivated the
            # original plan item. Do not expect a visible move in the artifact
            # rate from this; judge it on the salvage-pass invocation count.
            #
            # BATCHING (added 2026-08-11). The block above tells the model what
            # spends a turn but never told it that SEVERAL tool calls can share
            # one. Measured over 30 days: tool calls per loop is ~1.0 for every
            # agent (junior 5.91 calls / 5.95 loops = 0.99; quant 0.92; bull
            # 0.73), i.e. each lookup buys its own LLM round-trip.
            #
            # That matters because tool execution is NOT the cost. Splitting
            # agent wall-clock against summed tool elapsed_ms over the same 30
            # days: tools are 0.0-11.5% of it (bear 1.5s of 359.2s; quant 0.9s
            # of 272.7s; junior 18.5s of 160.6s). 88-100% is LLM time, so the
            # only way to make an agent faster is to make it take fewer turns.
            #
            # The harness DOES honour batching — this is an existence proof,
            # not a hope: 436 of 3,163 runs (13.8%) completed more tool calls
            # than they used loops, up to 13 more. Per-agent it ranges from
            # 27.7% (fundamental) to 0.9% (bull), which is the spread of a
            # prompt habit, not a platform limit. If a future measurement shows
            # calls/loop still pinned at 1.0 across every agent, this paragraph
            # is inert and should be deleted rather than left as decoration.
            from app.agents.tool_whitelists import get_agent_budget_turns
            _budget = get_agent_budget_turns(agent_name, True)
            user_prompt += (
                "You have access to a specific subset of tools for your domain. "
                "Use them only if you need deeper research beyond the pre-collected data. "
                "Do not redundantly fetch data already provided.\n"
                f"\n### TURN BUDGET: {_budget}\n"
                f"You get at most {_budget} turns for this task, and a turn is "
                "spent whether you call a tool or write prose.\n"
                "- ISSUE INDEPENDENT TOOL CALLS TOGETHER IN ONE TURN. Several "
                "calls in a single turn cost ONE turn, not one each. If you "
                "know you need two or three lookups and none of them depends "
                "on another's result, request them all at once — asking one at "
                "a time is the most common way this budget is wasted. Only "
                "wait for a result when your next choice genuinely depends on "
                "it.\n"
                "- Budget your research so the LAST turn is the JSON artifact.\n"
                "- NEVER spend a turn announcing what you are about to do. "
                "Text like \"I'll now complete the analysis and emit the JSON\" "
                "is a wasted turn, and if it is your last one the entire run is "
                "discarded — write the JSON instead of describing it.\n"
                "- If the budget runs short, emit the artifact from what you "
                "already have and record the gap in your findings. A partial "
                "report is worth far more than a complete one you never sent.\n\n"
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

        # ── The operator's channel ────────────────────────────────────────
        # A human watching the cycle can address an agent mid-run from the
        # Trading Agent Chat widget. Placed AFTER the output directive so it
        # cannot be read as a licence to stop emitting the artifact, and
        # BEFORE "Begin your analysis now" so it is the last instruction the
        # agent reads. Consumed exactly once — see app/v3/agent_chat.py for
        # why a directive must not become standing policy.
        try:
            from app.v3.agent_chat import (
                directive_block, mark_directives_consumed, pending_directives,
            )

            _directives = pending_directives(
                cycle_id=cycle_id, ticker=desk.ticker, agent_name=agent_name,
            )
            _block = directive_block(_directives)
            if _block:
                user_prompt += _block
                mark_directives_consumed(
                    [d["id"] for d in _directives], consumed_by=agent_name,
                )
                logger.info(
                    "[V3Runner] %s: %d operator directive(s) injected for %s",
                    agent_name, len(_directives), desk.ticker,
                )
                emit(
                    "analyzing", f"v3_directive_{desk.ticker}",
                    f"📨 {desk.ticker}: {agent_name} received "
                    f"{len(_directives)} operator directive(s)",
                    data={"kind": "agent_message", "ticker": desk.ticker,
                          "speaker": agent_name, "role": "system",
                          "message": "Operator directive received: "
                                     + " | ".join(
                                         (d.get("directive") or "")[:160]
                                         for d in _directives
                                     )},
                )
        except Exception as _dir_err:  # noqa: BLE001 — never block a run
            logger.debug("[V3Runner] directive injection skipped: %s", _dir_err)

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

        # What the run has SPENT so far, owned HERE so every failure handler
        # below — timeout, cancel, crash — reads the same numbers. The crash
        # path used to read `e.partial_cost` off the exception; the timeout
        # path could not, because wait_for raises its own TimeoutError after
        # cancelling the run (ABT fundamental analyst, cycle-v3-1788660665:
        # 1,800,068 ms, 18 tool calls, row said tokens=0 loops=0).
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
                cost_sink=_cost_sink,
                soft_deadline_s=timeout_seconds * 0.5,
                deadline_monotonic=t_start + timeout_seconds,
            ),
            timeout=timeout_seconds,
        )

        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        final_text = result.get("response", "")
        loops_used = result.get("loops_used", 1)
        token_usage = result.get("tokens_used", 0)
        cached_tokens = result.get("cached_tokens", 0)
        prompt_tokens = result.get("prompt_tokens", 0)
        stop_reason = result.get("stop_reason", "completed")
        model_used = result.get("model_used")
        provider_used = result.get("provider")

        # Budget exhaustion — the harness hit max_iterations without a final
        # answer, so the "response" is a sentinel, not an artifact. (The old
        # max_tokens/length check here was dead: run_agent never set those.)
        if stop_reason == "max_iterations":
            logger.warning(
                "[V3Runner] %s hit its turn wall for %s (%d chars of %s) — "
                "artifact parsing will fail.",
                agent_name, desk.ticker, len(final_text or ""),
                classify_output(final_text).name,
            )

        # Parse the artifact from the agent's output
        artifact = _parse_artifact(final_text, artifact_type, agent_name)

        # A fragment is a PARSE failure wearing an artifact's clothes: valid
        # JSON, so the repair pass below never fired, and the run instead spent
        # a full tool-enabled re-run (~100s, 37.7k tokens on TSM 2026-08-04)
        # rediscovering research it had already produced. Route it to repair —
        # but HOLD ON TO IT. If repair does not land, the fragment is restored
        # so the collapse branches below grade it exactly as they did before:
        # AGENT_ERROR first, DATA_GAP on the retry. Failing straight to
        # AGENT_ERROR here would return it twice and let should_abort() take
        # the whole ticker down, which the 2026-07-26 audit deliberately
        # designed against.
        fragment: dict | None = None
        wrong_shape = False
        # A parsed artifact written in another language. It is SHAPED correctly,
        # so nothing downstream objects — but every English prose heuristic that
        # reads it (extract_dynamic_trigger_from_text, disposition tokens, the
        # contradiction shadow) silently finds nothing, and the pipeline
        # proceeds as though the reasoning said nothing at all.
        #
        # MEASURED 2026-09-06: ZS's Board on cycle-v3-1788646388 returned
        # BUY @ 71% with quality 87 and a `final_decision` 33.5% CJK — the only
        # such artifact in 1,421 whiteboard entries over 16 days, but 1 of 4 GLM
        # boards. Routed through the SAME repair path as a fragment, because it
        # is one cheap tool-less turn: the analysis exists, it is in the wrong
        # language. If the repair does not land the ORIGINAL is restored below
        # — a Chinese decision is still a decision, and dropping it would turn a
        # legibility problem into a dead desk.
        if artifact is not None and prose_script_share(artifact) > NON_LATIN_PROSE_THRESHOLD:
            wrong_shape = True
            logger.warning(
                "[V3Runner] %s: %s for %s is %.0f%% non-Latin script — routing "
                "to the tool-less repair for an English rewrite",
                agent_name, artifact_type, desk.ticker,
                prose_script_share(artifact) * 100,
            )
            fragment, artifact = artifact, None
        elif artifact is not None and _is_wrong_shape(artifact_type, artifact):
            wrong_shape = True
            logger.warning(
                "[V3Runner] %s: parsed output is not a %s — it carries none of "
                "its fields (keys=%s). Treating as unparseable so the repair "
                "pass can run.",
                agent_name, artifact_type, sorted(artifact)[:20],
            )
            fragment, artifact = artifact, None

        # Salvage pass. A tool-enabled agent that reaches its iteration ceiling
        # is told by the harness to "summarize", and models frequently answer
        # with one more *pseudo* tool call in plain text (e.g.
        # `call:mcp__lazy-tool-service__get_sec_filings{ticker:WFC}`) instead of
        # the JSON artifact. Nothing executes it, so the literal string becomes
        # the final answer and parsing fails — burning the whole run's research.
        # One tool-less retry that shows the model its own output and asks only
        # for the JSON recovers it, so re-running every agent from scratch (or
        # tripping the breaker) is not the first resort.
        # Name the failure class BEFORE deciding whether repair can run, so the
        # counter sees the tool-less agents' failures too. A rate computed only
        # over the repairable population would be the "number computed
        # correctly over the wrong set" defect: it would read as a class rate
        # while excluding every agent that has no tool whitelist.
        rule = None
        repaired: bool | None = None
        if artifact is None:
            rule = classify_output(final_text, wrong_shape=wrong_shape)

        # A TRANSPORT fault is not repairable by re-asking. The tool-less
        # repair exists to recover an artifact from research the model has
        # ALREADY done; when the inference server handed the model's tool call
        # back as text, no tool ever ran, so the "repair" writes a report out
        # of the briefing in the prompt and the run is booked SUCCESS with a
        # quality score in the eighties. That is what happened to all 12
        # tickers of cycle-v3-1788565070. Let it fail where it can be seen.
        if artifact is None and rule is not None and rule.transport_failure:
            logger.error(
                "[V3Runner] %s: %s for %s (%d chars) — the inference server "
                "returned the model's tool call as TEXT. Refusing the tool-less "
                "repair: there is no research to repair from. Check the box's "
                "tool-call parser (see app/v3/output_rules.py).",
                agent_name, rule.name, desk.ticker, len(final_text or ""),
            )
        elif artifact is None and final_text and bool(tool_whitelist):
            logger.warning(
                "[V3Runner] %s: %s for %s (%d chars) — attempting tool-less "
                "artifact repair",
                agent_name, rule.name, desk.ticker, len(final_text),
            )
            try:
                # Hand back the agent's OWN findings, not just its last
                # sentence (2026-08-05). The common failure is an agent that
                # narrates its next step and runs out of turns — "I'll complete
                # the analysis and emit the desk_note JSON" — so `final_text`
                # is an announcement containing no analysis. Repairing from
                # that alone asks the model to write a report out of nothing,
                # and it failed again about as often as it succeeded. The
                # research is in the tool results; give it those.
                _transcript = result.get("tool_transcript") or []
                _findings = ""
                if _transcript:
                    _lines = [
                        "## WHAT YOU ALREADY FOUND (your own tool results this run)",
                        "Use these. They are the research behind the report you "
                        "were about to write.",
                        "",
                    ]
                    for _entry in _transcript:
                        _lines.append(
                            f"### {_entry.get('tool')} {_entry.get('args', '')}\n"
                            f"{_entry.get('result', '')}"
                        )
                    _findings = "\n".join(_lines) + "\n\n"

                # The rule's directive is the injected correction — the analog
                # of a stream rule that sits dormant until the model goes
                # off-script and then says the ONE thing that fits what it
                # actually did. The generic "could not be parsed" that used to
                # sit here was shown to every class equally: it told a model
                # that had returned nothing to fix its previous reply, and a
                # model whose JSON was truncated to start writing JSON.
                _previous = ""
                if rule.quote_previous:
                    # TRUNCATED_JSON is quoted from the TAIL: its head is
                    # perfectly good JSON and the cut is at the end, so a
                    # head-only excerpt shows the model none of the damage.
                    _excerpt = (
                        f"...{final_text[-2000:]}"
                        if rule.name == "TRUNCATED_JSON" and len(final_text) > 2000
                        else final_text[:2000]
                    )
                    _previous = (
                        f"## PREVIOUS ATTEMPT ({rule.name})\n{_excerpt}\n\n"
                    )

                repair_prompt = (
                    f"{user_prompt}\n\n"
                    f"{_findings}"
                    f"{_previous}"
                    f"{rule.directive}\n\n"
                    f"Do NOT call any tools — you have none available "
                    f"now. Using the analysis you already performed, "
                    f"reply with ONLY the '{artifact_type}' JSON "
                    f"object. Start with '{{' and end with '}}'. "
                    f"No markdown fences, no commentary.\n"
                )
                logger.info(
                    "[V3Runner] %s: repairing %s with %d recovered tool "
                    "result(s) (%d chars of findings)",
                    agent_name, desk.ticker, len(_transcript), len(_findings),
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
                        cost_sink=_cost_sink,  # the repair's spend joins the run's
                        soft_deadline_s=timeout_seconds * 0.5,
                        deadline_monotonic=t_start + timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
                repair_text = repair_result.get("response", "")
                artifact = _parse_artifact(repair_text, artifact_type, agent_name)
                repaired = artifact is not None
                if artifact is not None:
                    logger.info(
                        "[V3Runner] %s: artifact repair succeeded for %s "
                        "(rule %s)",
                        agent_name, desk.ticker, rule.name,
                    )
                token_usage += repair_result.get("tokens_used", 0)
                # Recompute on BOTH repair outcomes: recomputing only on
                # success meant an AGENT_ERROR row's elapsed_ms excluded the
                # failed repair pass, understating retry cost (~647s of board
                # retries undercounted on 08-04).
                elapsed_ms = int((time.monotonic() - t_start) * 1000)
            except Exception as e:
                repaired = False
                logger.warning(
                    "[V3Runner] %s: artifact repair failed for %s: %s: %s",
                    agent_name, desk.ticker, type(e).__name__, e,
                )
                elapsed_ms = int((time.monotonic() - t_start) * 1000)

        # One row per classified failure, whatever happened next. `repaired`
        # stays None when the repair pass never ran (a tool-less agent has
        # nothing to re-ask without tools), which keeps "we could not try" from
        # reading as "we tried and failed" — the same distinction the bear's
        # NOT_ASKED draws against DECLINED.
        if rule is not None:
            record_rule_firing(
                rule,
                agent_name=agent_name,
                ticker=desk.ticker,
                cycle_id=cycle_id,
                chars=len(final_text or ""),
                repaired=repaired,
            )
            emit(
                "analyzing",
                f"v3_output_rule_{desk.ticker}",
                f"📐 {desk.ticker}: {agent_name} → {rule.name}"
                + (
                    " (repaired)" if repaired
                    else " (repair failed)" if repaired is False
                    else " (no repair path)"
                ),
            )

        # Repair did not land (or could not run — it needs a tool-enabled
        # agent). Put the fragment back and let the collapse branches grade it.
        if artifact is None and fragment is not None:
            logger.warning(
                "[V3Runner] %s: repair did not recover a %s for %s — falling "
                "back to the fragment so it is graded, not discarded",
                agent_name, artifact_type, desk.ticker,
            )
            artifact = fragment

        if artifact is None:
            # Same retry contract as the collapse branch below, for the same
            # reason: should_abort() kills the whole ticker on a second
            # AGENT_ERROR, so returning it twice turns a degraded desk into no
            # desk at all. This branch used to ignore is_retry, and the
            # fragment restore above was what kept it off the retry path —
            # until the parser stopped handing back the nested block. It now
            # declines to invent fields the model never emitted (correctly),
            # and returns {} instead, which _parse_artifact reports as None.
            # So `fragment` is None too, and a truncated artifact reached this
            # branch on BOTH attempts and took the ticker down. The degrade has
            # to live here, not only in the restore path that feeds it.
            outcome = (
                PhaseOutcome.DATA_GAP if is_retry else PhaseOutcome.AGENT_ERROR
            )
            logger.error(
                "[V3Runner] %s produced no parseable artifact for %s — "
                "returning %s (retry=%s)",
                agent_name, desk.ticker, outcome.value, is_retry,
            )
            emit(
                "analyzing",
                f"v3_{agent_name}_fail_{desk.ticker}",
                f"❌ {desk.ticker}: V3 {agent_name} — no valid artifact produced",
                status="error",
            )
            # The class is whatever `classify_output` already decided at the
            # top of this branch — reusing `rule.name` is what makes this row
            # joinable to its `output_rule:` firing instead of a second opinion
            # about the same buffer.
            _record_telemetry(desk, agent_name, elapsed_ms, loops_used, token_usage,
                              outcome.value,
                              sys_prompt_chars=sys_prompt_chars, user_prompt_chars=user_prompt_chars,
                              cached_tokens=cached_tokens, prompt_tokens=prompt_tokens,
                              model_used=model_used, provider=provider_used,
                              attempt_no=attempt_no,
                              failure_reason=rule.name if rule else UNCLASSIFIED.name,
                              error_message=(
                                  f"no parseable {artifact_type} from {len(final_text or '')} "
                                  f"chars of output"
                                  + (f" (repair failed, rule {rule.name})" if repaired is False and rule
                                     else "")
                              ))
            return outcome

        # Decision artifacts: empty VALUES are as fatal as missing keys. The
        # schema check is presence-only, so an LLM emitting reasoning="" or
        # omitting signal_weights passed validation and produced hollow
        # trade_results rows ('{}' weights / blank reasoning — 45/525 measured
        # in the 2026-07-21 data audit). Strip empty reasoning so the
        # missing-required branch below catches it, and require a non-empty
        # signal_weights on the synthesizer's trade_decision.
        if artifact_type in ("final_decision", "trade_decision") and isinstance(artifact, dict):
            if not str(artifact.get("reasoning") or "").strip():
                artifact.pop("reasoning", None)

        # Flattened-inner-object salvage (2026-07-26 audit). NOTE 2026-08-04:
        # this was a per-field patch for one instance of the extractor bug now
        # fixed at source (`_is_wrong_shape` above + the SDK's depth-0 scan), so
        # a bare {direction, matters_this_week, why} is intercepted upstream and
        # repaired rather than re-nested here. Kept as a second line of defence:
        # the SDK is bind-mounted and may lag this deploy. Original note —
        # the fundamental
        # analyst emitted ONLY its nested `near_term_read` body at the top
        # level — {direction, matters_this_week, why} — which parses as clean
        # JSON, so the unparseable-repair pass above never fired. It then
        # scored 36/dead_end (JPM, cycle-v3-1785038939) and was appended as
        # SUCCESS anyway, and the Board went on to a confident HOLD citing
        # data_quality 95 over an empty fundamental desk. Re-nest it so the
        # research is kept; the envelope is still missing its required fields,
        # so the branch below decides the outcome — this only stops us from
        # throwing away the one field the debate actually reads.
        if (
            artifact_type == "fundamental_report"
            and isinstance(artifact, dict)
            and "near_term_read" not in artifact
            and "direction" in artifact
            and not any(k in artifact for k in ("summary", "pillars", "thesis_direction"))
        ):
            inner = {
                k: artifact.pop(k)
                for k in ("direction", "matters_this_week", "why")
                if k in artifact
            }
            artifact["near_term_read"] = inner
            logger.warning(
                "[V3Runner] %s: re-nested a flattened near_term_read for %s "
                "(keys=%s) — envelope still incomplete",
                agent_name, desk.ticker, sorted(inner),
            )

        # Validate the artifact
        errors = validate_artifact(artifact_type, artifact)
        if artifact_type == "trade_decision" and isinstance(artifact, dict):
            if not apply_signal_weights_policy(
                artifact,
                artifact_type=artifact_type,
                agent_name=agent_name,
                ticker=desk.ticker,
            ):
                errors = list(errors) + ["Missing required field: signal_weights (empty)"]
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
                                  sys_prompt_chars=sys_prompt_chars, user_prompt_chars=user_prompt_chars,
                                  cached_tokens=cached_tokens, prompt_tokens=prompt_tokens,
                                  model_used=model_used, provider=provider_used,
                                  attempt_no=attempt_no,
                                  failure_reason=SCHEMA_INVALID,
                                  error_message=(
                                      f"{artifact_type} missing required fields: "
                                      f"{', '.join(missing_required)}"
                                  ))
                return PhaseOutcome.AGENT_ERROR

            # ANALYST artifacts (2026-07-26 audit): the branch above was scoped
            # to decision artifacts only, so a fundamental_report missing ALL
            # FOUR required fields fell through here, got its warnings stapled
            # on, and was appended as SUCCESS. Nothing retried, and the desk
            # read an empty fundamental input as a real one — the Board issued
            # a confident HOLD at data_quality 95 over it.
            #
            # The gate is TOTAL COLLAPSE, not any missing required field. Those
            # are very different populations in production: `summary` is absent
            # in 23-55 of ~740 artifacts per type (3-7%), while desk_note's
            # `triage_recommendation` is absent in 530 of 810 (65%) — routine,
            # survivable, and routed around. Hard-failing on any single missing
            # field would have converted the majority of junior-analyst runs
            # into retries and desk aborts: a far worse regression than the bug.
            # An artifact that kept NO substantive field is the JPM signature.
            #
            # And NOT at the cost of the desk: should_abort() kills the whole
            # ticker once retries are exhausted, so returning AGENT_ERROR twice
            # would turn a degraded desk into no desk at all — strictly worse
            # than the bug. The retry therefore degrades to DATA_GAP, which is
            # non-fatal, still flows to the debate, and is already the signal
            # downstream desks use to discount an input.
            if missing_required and _artifact_collapsed(artifact_type, artifact):
                outcome = (
                    PhaseOutcome.DATA_GAP if is_retry else PhaseOutcome.AGENT_ERROR
                )
                # Log the keys the artifact DID keep. Without them a collapse
                # is undiagnosable after the fact: the raw model text lives
                # only in the container's stdout, which dies with the
                # container (cycle-v3-1785792600's junior-analyst collapse was
                # unreconstructable an hour later for exactly this reason),
                # and `agent_traces.tool_args` is truncated at 2000 chars.
                # The key list alone identifies the wrong-schema shape —
                # e.g. ['data', 'label', 'schema'] is the emit_structured_output
                # envelope handled in _unwrap_structured_output().
                logger.error(
                    "[V3Runner] %s analyst artifact for %s is missing required "
                    "fields %s — returning %s (retry=%s). Keys actually "
                    "present: %s",
                    agent_name, desk.ticker, missing_required, outcome.value, is_retry,
                    sorted(artifact)[:20] if isinstance(artifact, dict) else type(artifact).__name__,
                )
                emit(
                    "analyzing",
                    f"v3_{agent_name}_fail_{desk.ticker}",
                    f"❌ {desk.ticker}: V3 {agent_name} — analyst artifact "
                    f"missing required fields {missing_required}",
                    status="error",
                )
                _record_telemetry(desk, agent_name, elapsed_ms, loops_used, token_usage,
                                  outcome.value,
                                  sys_prompt_chars=sys_prompt_chars, user_prompt_chars=user_prompt_chars,
                                  cached_tokens=cached_tokens, prompt_tokens=prompt_tokens,
                                  model_used=model_used, provider=provider_used,
                                  attempt_no=attempt_no,
                                  failure_reason=SCHEMA_INVALID,
                                  error_message=(
                                      f"{artifact_type} collapsed — missing required fields: "
                                      f"{', '.join(missing_required)}; kept keys: "
                                      f"{', '.join(sorted(artifact)[:10]) if isinstance(artifact, dict) else '?'}"
                                  ))
                # On the retry we keep going so the salvaged research still
                # reaches the desk — but tagged, never as a clean SUCCESS.
                if outcome is PhaseOutcome.AGENT_ERROR:
                    return outcome
                artifact["_validation_warnings"] = errors
                artifact["_degraded"] = True
            else:
                logger.warning(
                    "[V3Runner] %s artifact validation warnings for %s: %s",
                    agent_name, desk.ticker, errors,
                )
                # Non-fatal for analyst artifacts — we still append, but log the issues
                artifact["_validation_warnings"] = errors

        # Type-specific coercion (2026-07-21 audit): regime enum literals,
        # out-of-range factors, and null dynamic_trigger values all reached
        # the DB unvalidated — a null trigger value means the watch can NEVER
        # fire (order_triggers gates on `value is not None`).
        from app.v3.artifact_validators import validate_artifact as _coerce_artifact
        artifact = _coerce_artifact(artifact_type, artifact, desk=desk)

        # A SELL the bot cannot place is not a verdict. Applied to the decision
        # artifacts only, and only when the desk knows the position is not held
        # (a MISSING `held` key is not treated as "not held" — that would coerce
        # real exits on any cycle whose portfolio lookup failed).
        if artifact_type in ("final_decision", "trade_decision"):
            artifact = guard_unshortable_sell(artifact, desk=desk, bot_id=bot_id)

        # THE BEAR'S SUBSTITUTE (app/v3/substitute.py). Normalised HERE, at the
        # one point that has both the artifact and the desk that carries the
        # pool the bear was shown — the membership check cannot be done in
        # `artifact_validators` because that dispatch takes no desk.
        #
        # Runs before `append_artifact`, so the deciders read the normalised
        # field and never the model's raw spelling of it. Never raises: a
        # substitute is a label, and a label must not cost a bear case.
        if artifact_type == "bear_rebuttal":
            from app.v3.substitute import apply_substitute

            artifact = apply_substitute(artifact, desk=desk)

        # SHADOW (2026-07-25): mark the one board override measured to lose
        # money — board turning bearish over a fundamental desk that reported
        # NO near-term view (-2.81%/decision, n=68; -2.38% executable-only at
        # permutation p=0.0015). Flags only, never rewrites: n=35 executable
        # overrides is enough to detect the effect, not to rewire the board,
        # and the standing rule is that board changes ship shadow-first.
        if artifact_type in ("final_decision", "trade_decision"):
            try:
                from app.v3.artifact_validators import (
                    flag_bearish_override_of_fundamental,
                )

                artifact = flag_bearish_override_of_fundamental(
                    artifact,
                    fundamental_report=getattr(desk, "fundamental_report", None),
                    ticker=desk.ticker, cycle_id=desk.cycle_id,
                )
            except Exception as _shadow_err:  # noqa: BLE001
                # A shadow flag must never be able to affect a decision.
                logger.warning(
                    "[V3Runner] %s: override shadow flag failed (non-fatal): %s",
                    desk.ticker, _shadow_err,
                )

        # 2026-07-25: provenance is ASSERTED here, at the one call site that
        # knows an agent actually ran and produced this artifact. It used to be
        # inferred by append_artifact's default, which credited every unstamped
        # decision — including two hardcoded triage HOLDs — as board-reasoned.
        # Moving the claim to where the evidence is means the default can be
        # honest (unattributed) without demoting real board output. Only fills
        # a blank: the validators above (unshortable-SELL coercion) already set
        # the field, and their marker must win.
        if artifact_type in ("final_decision", "trade_decision") and not artifact.get(
            "decision_provenance"
        ):
            artifact["decision_provenance"] = DecisionProvenance.BOARD_REASONED.value

        # Append to SharedDesk
        desk.append_artifact(artifact_type, artifact)

        # Persist the quant analyst's technical overlays to the AI Analysis
        # Overlays chart deterministically. The agent USED to tool-call
        # save_trading_chart mid-loop (step 5), but after the 2026-07-18 prompt
        # compression it began emitting its final JSON and skipping that call —
        # so no cycle produced chart overlays. Overlays now travel in the
        # artifact's `overlays` field (which the model reliably fills) and we
        # write the chart here, independent of whether the tool was called.
        if agent_name == "v3_quant_analyst":
            # Fabrication guard (2026-07-24 audit): 171 of 305 quant reports
            # carried an RSI that matched NO number anywhere on the desk, and
            # 148 of those came from runs with zero tool calls. risk_metrics
            # drives volatility_regime and stop placement, so a made-up RSI is
            # not a cosmetic problem. Verifiable fields are replaced with
            # values computed from stored data; the model's originals are kept
            # on the artifact so the rate stays measurable.
            try:
                from app.quant.technical_baseline import reconcile_risk_metrics

                report = reconcile_risk_metrics(
                    artifact, desk.ticker, model_used_tools=loops_used > 1
                )
                if report.get("corrected"):
                    logger.warning(
                        "[V3Runner] %s: quant risk_metrics disagreed with stored "
                        "data (%sapplied, baseline %s): %s",
                        desk.ticker,
                        "" if report.get("applied") else "NOT ",
                        report.get("as_of", "?"),
                        report["corrected"],
                    )
            except Exception as e:
                logger.warning(
                    "[V3Runner] risk_metrics reconciliation failed for %s: %s: %s",
                    desk.ticker, type(e).__name__, e,
                )

        if agent_name == "v3_fundamental_analyst":
            # The third of the same guard. Until 2026-07-28 this desk emitted no
            # numeric fields at all, so there was nothing to reconcile and the
            # ratios quoted in its prose were never checked against the row they
            # came from: 4 of 7 stated P/Es were wrong in one cycle, CARS by 83%
            # (it quoted the FORWARD P/E as the trailing one — the failure is
            # mislabelling as often as invention, and both look identical
            # downstream).
            try:
                from app.quant.fundamental_block import (
                    reconcile_fundamental_metrics,
                )

                report = reconcile_fundamental_metrics(
                    artifact, desk.ticker, model_used_tools=loops_used > 1
                )
                if report.get("corrected"):
                    logger.warning(
                        "[V3Runner] %s: fundamental metrics disagreed with "
                        "stored data (%sapplied, snapshot %s): %s",
                        desk.ticker,
                        "" if report.get("applied") else "NOT ",
                        report.get("as_of", "?"),
                        report["corrected"],
                    )
            except Exception as e:
                logger.warning(
                    "[V3Runner] fundamental reconciliation failed for %s: %s: %s",
                    desk.ticker, type(e).__name__, e,
                )

            # Positioning counts. The alt-data block was widened to six agents
            # on 2026-07-28 and then measured: ZERO of the newly added agents
            # cited it. Injection alone loses to the compressed desk view, so
            # this pairs the block with a required field and a reconcile — the
            # same three-part shape that took this desk from 0 numeric fields
            # to 23 reconciled ones.
            try:
                from app.v3.alt_data_block import reconcile_positioning_read

                rep = reconcile_positioning_read(artifact, desk.ticker)
                if rep.get("corrected"):
                    logger.warning(
                        "[V3Runner] %s: positioning counts disagreed with "
                        "stored data: %s", desk.ticker, rep["corrected"],
                    )
            except Exception as e:
                logger.warning(
                    "[V3Runner] positioning reconciliation failed for %s: %s: %s",
                    desk.ticker, type(e).__name__, e,
                )

        if agent_name == "v3_valuation_analyst":
            # Same guard as the quant's, one layer down. The multiples drive the
            # verdict, the verdict reaches the Board, and nothing else in the
            # pipeline can tell a computed EV/EBIT from a plausible one — so the
            # verifiable fields are replaced with values computed from stored
            # filings and the model's originals kept, which is what makes the
            # fabrication RATE measurable rather than merely suppressed.
            try:
                from app.quant.valuation_block import reconcile_valuation_metrics

                report = reconcile_valuation_metrics(
                    artifact, desk.ticker, model_used_tools=loops_used > 1
                )
                if report.get("corrected"):
                    logger.warning(
                        "[V3Runner] %s: valuation_metrics disagreed with computed "
                        "values (%sapplied, snapshot %s): %s",
                        desk.ticker,
                        "" if report.get("applied") else "NOT ",
                        report.get("as_of", "?"),
                        report["corrected"],
                    )
            except Exception as e:
                logger.warning(
                    "[V3Runner] valuation reconciliation failed for %s: %s: %s",
                    desk.ticker, type(e).__name__, e,
                )
        # QUANT-ONLY persistence. These two read `risk_metrics` and `overlays`,
        # which only the quant artifact has — but they sat OUTSIDE any
        # agent_name guard and therefore ran after EVERY agent.
        #
        # Measured 2026-07-29 from the container logs: the "carried no evidence
        # fields (only ['confidence'])" warning fires on the VALUATION analyst,
        # not the quant, and lands two lines after "Appended valuation_report".
        # The valuation artifact has no risk_metrics, so the extraction
        # correctly collapsed to `confidence` — and before the stub guard
        # existed, that is what wrote `{'confidence': 65}` into the quant's
        # `signals` section under the quant's name. 53 of 326 such writes.
        #
        # So the stub was never the quant being lazy: it was another agent's
        # artifact posted to the quant's section. The guard stopped the bad
        # write; this stops the wrong CALLER.
        if agent_name == "v3_quant_analyst":
            try:
                await _persist_quant_chart(desk.ticker, artifact)
            except Exception as e:
                logger.warning(
                    "[V3Runner] chart overlay persist failed for %s: %s: %s",
                    desk.ticker, type(e).__name__, e,
                )
            try:
                await _persist_quant_signals(desk, cycle_id, artifact)
            except Exception as e:
                logger.warning(
                    "[V3Runner] quant signals whiteboard write failed for %s: %s: %s",
                    desk.ticker, type(e).__name__, e,
                )

        # Same agent-name pin as the quant block above, for the same reason:
        # this hook must never fire on another agent's artifact.
        if agent_name == "v3_junior_analyst":
            try:
                await _persist_junior_market_context(desk, cycle_id, artifact)
            except Exception as e:
                logger.warning(
                    "[V3Runner] junior market_context fallback failed for %s: %s: %s",
                    desk.ticker, type(e).__name__, e,
                )

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
        # A degraded artifact already recorded its own AGENT_ERROR/DATA_GAP row
        # above; recording SUCCESS here too would put both in v3_agent_telemetry
        # and make the failure invisible to the exact query that found this bug.
        degraded = bool(artifact.get("_degraded"))
        if not degraded:
            # attempt_no on the SUCCESS row too, not just the failures: the
            # case this column exists for is a run that FAILED and then worked
            # (ASIC/v3_junior_analyst, cycle-v3-1786455000, quality -1 then
            # 88). Stamping only the failure would leave the row that actually
            # produced the artifact claiming to be a first attempt.
            _record_telemetry(desk, agent_name, elapsed_ms, loops_used, token_usage, "SUCCESS", quality_score,
                              sys_prompt_chars=sys_prompt_chars, user_prompt_chars=user_prompt_chars,
                              artifact_size_bytes=artifact_size_bytes,
                              cached_tokens=cached_tokens, prompt_tokens=prompt_tokens,
                              model_used=model_used, provider=provider_used,
                              attempt_no=attempt_no)

            # Benchmark a second box on this exact prompt, off the critical
            # path. Dispatched only on a NON-degraded success: shadowing a run
            # whose primary already failed would compare the boxes on an input
            # the pipeline itself could not handle. Nothing awaits this and no
            # result of it reaches the desk.
            try:
                from app.v3.model_shadow import dispatch_shadow, shadow_endpoint_for
                shadow_ep = shadow_endpoint_for(agent_name)
                if shadow_ep:
                    dispatch_shadow(
                        endpoint=shadow_ep,
                        agent_name=agent_name,
                        ticker=desk.ticker,
                        cycle_id=cycle_id,
                        bot_id=bot_id,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        max_tokens=safe_max_tokens,
                        timeout_seconds=timeout_seconds,
                        primary={
                            "model_used": model_used,
                            "provider": provider_used,
                            "elapsed_ms": elapsed_ms,
                            "tokens_used": token_usage,
                            "loops_used": loops_used,
                            "response": final_text,
                        },
                    )
            except Exception as shadow_err:
                logger.warning("[V3AgentRunner] shadow dispatch skipped for %s: %s",
                               agent_name, shadow_err)

        # Classify outcome
        data_gaps = artifact.get("data_gaps", [])
        if degraded or (data_gaps and len(data_gaps) > 2):
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
        _spent_loops, _spent_tokens = _spent(_cost_sink)
        _record_telemetry(desk, agent_name, elapsed_ms, _spent_loops, _spent_tokens, "TIMED_OUT",
                          sys_prompt_chars=sys_prompt_chars, user_prompt_chars=user_prompt_chars,
                          prompt_tokens=_spent_tokens,
                          attempt_no=attempt_no,
                          failure_reason=REASON_TIMEOUT,
                          cost_partial=True,
                          error_message=f"exceeded the {timeout_seconds:.0f}s agent timeout")
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
        _spent_loops, _spent_tokens = _spent(_cost_sink)
        _record_telemetry(desk, agent_name, elapsed_ms, _spent_loops, _spent_tokens, "CANCELLED",
                          prompt_tokens=_spent_tokens,
                          attempt_no=attempt_no,
                          failure_reason=REASON_CANCELLED,
                          cost_partial=True,
                          error_message="cancelled — stop requested")
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
        # `e` used to end here: the row said AGENT_ERROR and the only copy of
        # WHY was a log line in a container that gets replaced on every deploy.
        #
        # The tokens and loops used to be hardcoded 0/0, which made a crash FREE
        # in every ledger that sums prompt_tokens — the 20k invariant, the
        # per-model comparison, the cycle's token total. GOOG's dead bull agent
        # (2026-09-05, cycle-v3-1788646388) recorded tok=0 loops=0 while
        # `agent_tool_telemetry` held 7 tool calls for it and vLLM had run two
        # full 24k-token prefills. `partial_cost` is attached by base_agent to
        # whatever escapes — the ResilientCallError, not the RuntimeError
        # underneath — and accumulates across the retry attempts.
        # The sink is the source; the attribute on the exception is the
        # fallback for a run_agent that predates the sink (partial deploy).
        _spent_loops, _spent_tokens = _spent(_cost_sink, getattr(e, "partial_cost", None))
        # A refused retry is its own class: the run was healthy enough to be
        # retried but the wall clock could not fit it. Name it so the ledger
        # can count "we gave up on purpose" apart from "the provider crashed".
        _reason = _crash_reason(e)
        _record_telemetry(desk, agent_name, elapsed_ms,
                          _spent_loops, _spent_tokens, "AGENT_ERROR",
                          # `prompt_tokens` is the field every audit probe and
                          # the 20k invariant actually read; leaving it 0 while
                          # token_usage was populated would keep the crash free
                          # in exactly the ledgers that matter.
                          prompt_tokens=_spent_tokens,
                          attempt_no=attempt_no,
                          failure_reason=_reason,
                          cost_partial=True,
                          error_message=f"{type(e).__name__}: {e}")
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
    _why = ""
    try:
        from app.utils.text_utils import parse_json_response
        parsed = parse_json_response(text)
        if isinstance(parsed, dict) and parsed:
            return _unwrap_structured_output(parsed, artifact_type, agent_name)
        _why = "every strategy returned empty"
    except Exception as e:  # noqa: BLE001 — the reason is the payload here
        _why = f"{type(e).__name__}: {e}"

    # Log WHAT came back, not just how much (2026-08-05). The char count alone
    # made this undiagnosable: a 49-char failure and an 11,248-char failure are
    # completely different bugs (a spent turn budget vs a model that reasoned
    # instead of emitting), and the count cannot tell them apart. Same lesson as
    # the gatekeeper's empty-response sentinel earlier today. Head AND tail,
    # because a truncated artifact looks fine at the front and dies at the end.
    _preview = (text or "").strip()

    # WHY it failed, not just what came back. Every decode error on this path
    # was being discarded — six `except json.JSONDecodeError: continue` sites
    # in lazycat.llm_json plus a bare `except Exception: pass` here — so a
    # complete, well-formed-LOOKING artifact that python refused could not be
    # explained after the fact. Diagnosing v3_quant_analyst's TSM failure on
    # 2026-08-11 dead-ended exactly here: 3,682 chars opening `{"summary":` and
    # closing `"tags":[...]}`, no truncation, and nothing anywhere recorded
    # which character python choked on. The raw text is not stored either
    # (llm_audit_logs only keeps v3_decision), so the evidence was gone.
    #
    # Re-parsing here costs one json.loads on an already-failed buffer and
    # turns "unparseable" into a position and a reason.
    _detail = _why
    try:
        import json as _json
        _json.loads(_preview)
    except ValueError as decode_err:
        pos = getattr(decode_err, "pos", None)
        _detail = f"{_why} | json.loads: {decode_err}"
        if isinstance(pos, int):
            _detail += " | at: " + sanitize_ascii(repr(_preview[max(0, pos - 80):pos + 80]))
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        pass

    logger.warning(
        "[V3Runner] Failed to parse artifact from %s output (%d chars) — %s\n"
        "  HEAD: %s\n  TAIL: %s",
        agent_name, len(text), _detail or "reason unknown",
        sanitize_ascii(_preview[:600]),
        sanitize_ascii(_preview[-300:]) if len(_preview) > 900 else "(shown above)",
    )
    return None


def _retry_was_refused(exc: BaseException, refused_type: type) -> bool:
    """True when a RetryBudgetExhausted is anywhere in what escaped: the
    exception itself, its __cause__ chain, or the per-attempt records
    aresilient_call attaches to its ResilientCallError.

    The record carries the class NAME, not the exception. `AttemptRecord`
    (lazycat/resilience.py:116) is a dataclass of `attempt`, `exception_type:
    str`, `exception_msg`, `failure_type`, `elapsed_ms`, `timestamp` — there is
    no exception object on it, `ResilientCallError` sets no `__cause__`, and
    `str(exc)` names only the failure TYPE ("last_type=fatal"). The first
    version looked for an `exception`/`error`/`exc` attribute, found nothing on
    any real record, and so returned False for every refusal that ever
    happened: each one was filed as RUNNER_EXCEPTION, and the reason this fix
    exists never appeared in a single row.
    """
    name = refused_type.__name__
    seen = []
    cur = exc
    while cur is not None and cur not in seen:
        seen.append(cur)
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    if any(isinstance(x, refused_type) or type(x).__name__ == name for x in seen):
        return True
    for rec in (getattr(exc, "attempts", None) or []):
        recorded = rec.get("exception_type") if isinstance(rec, dict) else getattr(rec, "exception_type", None)
        if recorded == name:
            return True
        # Tolerate a record that does carry the exception itself.
        for attr in ("exception", "error", "exc"):
            v = rec.get(attr) if isinstance(rec, dict) else getattr(rec, attr, None)
            if v is not None and (isinstance(v, refused_type) or type(v).__name__ == name):
                return True
    return name in str(exc)


def _crash_reason(exc: BaseException) -> str:
    """RETRY_BUDGET_EXHAUSTED when the run refused a retry it could not finish,
    RUNNER_EXCEPTION otherwise.

    Its own function because the `except` block it serves sits 1,600 lines
    inside `run_v3_agent`, where nothing can reach it: replacing the reason
    with a bare RUNNER_EXCEPTION there left every test green.
    """
    from app.agents.base_agent import RetryBudgetExhausted

    return RETRY_BUDGET_EXHAUSTED if _retry_was_refused(exc, RetryBudgetExhausted) else RUNNER_EXCEPTION


def _spent(sink: dict | None, fallback: dict | None = None) -> tuple[int, int]:
    """(loops, tokens) a run had spent when it died — from the caller-owned
    sink, or from the attribute an older run_agent attached to its exception."""
    src = sink if (sink and any(sink.get(k) for k in ("tokens", "loops"))) else (fallback or sink or {})
    return int(src.get("loops") or 0), int(src.get("tokens") or 0)


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
    cached_tokens: int = 0,
    prompt_tokens: int = 0,
    model_used: str | None = None,
    provider: str | None = None,
    attempt_no: int = 1,
    error_message: str = "",
    failure_reason: str | None = None,
    cost_partial: bool = False,
) -> None:
    """Record telemetry for a V3 agent run.

    `failure_reason` names the failure class. It is NOT a free-form string and
    NOT a second taxonomy: it must come from `app/v3/output_rules.py` — either
    an `OutputRule.name` (so the row joins the matching `output_rule:` firing
    in `v3_guardrail_firings`) or one of the runner reasons for failures where
    no buffer was ever classified. See the namespace note in that module.
    """
    if failure_reason and failure_reason not in FAILURE_REASONS:
        # Loud but non-fatal: telemetry never aborts a cycle, but an unknown
        # class silently entering the column is exactly how a second taxonomy
        # starts, so it does not get to pass quietly.
        logger.error(
            "[V3Runner] failure_reason %r is not in the output_rules namespace "
            "— recording it would fork the taxonomy. Storing UNCLASSIFIED.",
            failure_reason,
        )
        failure_reason = UNCLASSIFIED.name

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
        "sys_prompt_chars": sys_prompt_chars,
        "user_prompt_chars": user_prompt_chars,
        "attempt_no": attempt_no,
        "error_message": sanitize_error_message(error_message),
        "failure_reason": failure_reason,
        # KV-cache probe: last-request snapshot from the harness (see
        # base_agent run_agent return keys). cached_tokens == 0 on a
        # multi-iteration run means prefix caching did nothing for this agent.
        "cached_tokens": cached_tokens,
        "prompt_tokens": prompt_tokens,
        "model_used": model_used,
        "provider": provider,
        # True when the run DIED and these numbers are what it had already
        # spent rather than what it completed. A consumer summing tokens wants
        # both; one measuring throughput wants only the completed runs.
        "cost_partial": bool(cost_partial),
    }
    desk.record_agent_telemetry(entry)

    # FLUSH NOW, not at the next desk save.
    #
    # MEASURED 2026-09-05 on the GLM cycle cycle-v3-1788642086: after the
    # junior, fundamental and quant analysts had all COMPLETED on three
    # tickers — 35 tool calls spent — `v3_agent_telemetry` held 0 rows and
    # `shared_desk` held 0 rows. Every entry lived only on the in-memory desk
    # until the first `save_desk`, which lands after the whole analyst chain.
    #
    # `flush_agent_telemetry` already existed for exactly this reason and its
    # docstring says the desk "flushes as it progresses" — but its only
    # trigger was `save_desk`, so the granularity was the PHASE, not the
    # agent. The 71 desks with no cost record it was written to fix (up to
    # ~47M tokens, ~14.5% of true spend) are still reachable in that window.
    #
    # Confirmed against 16 days of finished cycles: 7 of 132 carry ZERO
    # telemetry rows, and 5 of those 7 are `stopped` — killed mid-flight
    # before any desk saved. `cycle-v3-1788630137`, stopped by the operator
    # on 2026-09-05 with 9 tickers in flight, lost the cost of all of them.
    #
    # Idempotent (`flush_agent_telemetry` marks what it wrote and skips it
    # next time), so `save_desk` and the end-of-pipeline `persist_telemetry`
    # keep working unchanged and simply find nothing pending. Never raises:
    # cost accounting must not be able to fail an agent that succeeded.
    try:
        from app.v3.telemetry import flush_agent_telemetry

        flush_agent_telemetry(desk)
    except Exception as exc:  # noqa: BLE001 — an observer must not break a run
        logger.warning(
            "[V3Runner] telemetry flush after %s failed (row stays pending "
            "for the next save): %s", agent_name, exc,
        )
