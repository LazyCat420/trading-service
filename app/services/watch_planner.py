"""watch_planner — Stage 2. A small, bounded agent that recommends; never decides.

SHIPS DISABLED (`WATCH_PLANNER_ENABLED=0`) ON PURPOSE
-----------------------------------------------------
The delivery order this implements is: enrichment and deterministic triage run
in shadow FIRST, and the planner is enabled only once the shadow data shows the
triage inputs are reliable. Turning a model loose on inputs that have not been
validated would produce judgements about `catalyst_urgency` and
`resolution_overlap` values nobody has checked, and the judgements would look
reasonable either way — which is how a green check lies.

So this module exists, is tested, and returns nothing until switched on.

WHY IT IS TOOL-LESS AND TINY
----------------------------
`chat_toolless` is used rather than the agent endpoint because the agent
endpoint attaches the full MCP catalog server-side — measured at ~83 tools
≈ 21k tokens before any prompt. A triage planner whose whole job is to be
cheaper than the cycle it gates must not carry a 21k-token catalog to decide
"not now".

THE OUTPUT IS ADVICE
--------------------
Nothing here enforces a bound. `watch_policy.apply_policy` clamps every time,
window, cooldown and budget, for the planner path and the deterministic path
alike. Enforcing bounds in both places would put one rule on two seams, and the
two would drift — the failure this codebase has already paid for elsewhere.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.services import watch_schema

logger = logging.getLogger(__name__)

# Small model, short leash. The planner reads a page and answers with one object.
DEFAULT_MAX_TOKENS = 400
DEFAULT_TIMEOUT_S = 45.0

SYSTEM_PROMPT = """You are a research allocator for an equity desk. You do NOT analyse the stock.

You decide only whether a full, expensive research cycle is worth running RIGHT NOW for one ticker, given what changed. A full cycle costs real money and there are only a handful available per day across the whole desk, so the bar is "would this change a decision", not "is this interesting".

Answer with ONE JSON object and nothing else:

  {"action": "ANALYZE_NOW",          "rationale": "...", "expected_information": "..."}
  {"action": "SCHEDULE_AT",          "at": "<ISO-8601 UTC>", "rationale": "...", "expected_information": "..."}
  {"action": "WATCH_FOR_CONDITION",  "condition": {"type": "price_below", "level": 145.5}, "until": "<ISO-8601 UTC>", "rationale": "..."}
  {"action": "CLOSE_WATCH",          "close_reason": "...", "rationale": "..."}

Guidance:
- ANALYZE_NOW only when the new evidence plausibly resolves, or overturns, the stated open question. New information that merely mentions the company is not enough.
- SCHEDULE_AT when the resolving fact has a known arrival time (an earnings call, a filing date). Prefer just AFTER the fact lands, never just before — analysing hours before a known release spends a cycle on a thesis the release is about to rewrite.
- WATCH_FOR_CONDITION when the question is real but nothing has happened yet and a code-checkable level would answer it. This is the cheap answer; prefer it when in doubt.
- CLOSE_WATCH when the open question has already been settled, or the thesis no longer applies.
- "Do nothing" is a good answer. Most trips deserve WATCH_FOR_CONDITION.

condition.type must be one of: price_above, price_below, pct_change, rsi, volume_spike, news, staleness."""


def build_prompt(*, ticker: str, watch: dict, verdict, calendar: dict,
                 now: datetime | None = None) -> str:
    """The user turn. Everything the planner may consider, and nothing else.

    Deliberately includes the score COMPONENTS rather than only the total: the
    planner's useful contribution is disagreeing with a specific term ("the
    catalyst urgency is low because the calendar is stale, not because there is
    no catalyst"), and it cannot disagree with a number it cannot see.
    """
    now = now or datetime.now(timezone.utc)
    rc = watch_schema.get_resolution_condition(watch) or {}
    dc = (watch or {}).get("decision_context") or {}
    ev = (verdict.detail or {}).get("evidence", {}) if verdict is not None else {}

    cal_lines = []
    for e in (calendar or {}).get("events", [])[:5]:
        cal_lines.append(f"  - {e.get('kind')}: {e.get('label')} at {e.get('at')} "
                         f"(confidence: {e.get('confidence')})")
    cal_block = "\n".join(cal_lines) or "  (no scheduled events found)"

    return f"""TICKER: {ticker}
NOW: {now.isoformat()}

PRIOR DECISION
  action: {dc.get('action') or 'unknown'}
  confidence: {dc.get('confidence')}
  thesis: {(dc.get('thesis_summary') or '(none recorded)')[:800]}

THE OPEN QUESTION THIS WATCH EXISTS TO RESOLVE
  question: {rc.get('open_question') or '(NONE RECORDED — this watch predates the contract)'}
  resolved by: {rc.get('resolving_fact') or '(none recorded)'}
  expected: {rc.get('resolves_by') or 'unknown'}
  invalidated if: {json.dumps(rc.get('invalidates_if')) if rc.get('invalidates_if') else '(no level recorded)'}

WHAT JUST HAPPENED
  kind: {ev.get('kind')} ({ev.get('trigger_type')}), source: {ev.get('source')}
  observed at: {ev.get('observed_at')}
  evidence: {(ev.get('text') or '(no text)')[:500]}
  overlap with the open question: {(verdict.detail or {}).get('resolution_overlap') if verdict is not None else None} matching terms

CALENDAR
{cal_block}

DETERMINISTIC SCORE: {getattr(verdict, 'score', None)}
COMPONENTS: {json.dumps(getattr(verdict, 'components', {}), default=str)}

Answer with one JSON object."""


def _extract_json(text: str) -> dict | None:
    """Pull the object out of a model response.

    A model that wraps its answer in prose or a fence has still answered; a
    parser that only accepts a bare object would score that as a refusal and
    the failure would look like "the planner never recommends anything".
    """
    if not text:
        return None
    s = text.strip()
    if "```" in s:
        parts = s.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                s = p
                break
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return None


async def plan(
    *, ticker: str, watch: dict, verdict, calendar: dict,
    now: datetime | None = None,
    provider: str | None = None, model: str | None = None,
    call_model=None,
) -> tuple[dict | None, str | None]:
    """Ask the planner. Returns (normalised_result, error).

    `call_model` is injectable so the whole module is testable without a live
    model. A None result is ALWAYS safe: `apply_policy` with no planner result
    falls back to the deterministic path, so a planner outage degrades to the
    behaviour that shipped, never to no behaviour.
    """
    from app.services.parameter_store import get_param

    try:
        if int(get_param("WATCH_PLANNER_ENABLED")) == 0:
            return None, "planner disabled (WATCH_PLANNER_ENABLED=0)"
    except Exception as e:  # noqa: BLE001
        # An unreadable switch must not enable a disabled feature.
        return None, f"planner switch unreadable ({e}) — treating as disabled"

    prompt = build_prompt(ticker=ticker, watch=watch, verdict=verdict,
                          calendar=calendar, now=now)

    if call_model is None:
        from app.services.prism_agent_caller import chat_toolless as _ct

        async def call_model(**kw):  # noqa: ANN001
            return await _ct(**kw)

    try:
        resp = await call_model(
            provider=provider or "prism", model=model or "",
            system_prompt=SYSTEM_PROMPT, user_prompt=prompt,
            max_tokens=DEFAULT_MAX_TOKENS, timeout_seconds=DEFAULT_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[WatchPlanner] call failed for %s: %s", ticker, e)
        return None, f"planner call failed: {e}"

    raw = (resp or {}).get("response") if isinstance(resp, dict) else resp
    obj = _extract_json(raw if isinstance(raw, str) else "")
    if obj is None:
        return None, "planner returned no parsable JSON object"

    result, err = watch_schema.normalize_planner_result(obj, now=now)
    if err:
        logger.info("[WatchPlanner] %s: rejected planner result — %s", ticker, err)
        return None, err
    result["tokens_used"] = (resp or {}).get("tokens_used") if isinstance(resp, dict) else None
    return result, None
