"""
SkillOpt — post-cycle skill mutation for the V3 agent fleet.

After each autoresearch reflection, propose one bounded edit per target agent's
persistent "skill doc" (a short markdown block that skill_loader prepends to
that agent's system prompt), validate it against a heuristic score gate, and
persist accepted versions to agent_skills. Rejected candidates are logged to
rejected_skill_edits. Modeled on microsoft/SkillOpt's propose→validate→commit
loop, adapted to this repo:

- Baseline signal = confidence-weighted outcome score over the most recent
  resolved directional decision_outcomes rows (WIN=1, FLAT=0.5, LOSS=0).
- Validation is HEURISTIC (content-quality checks + score gate), not a true
  replay — re-running the V3 pipeline on history is far too expensive. The
  acceptance bar (+0.5%) is deliberately low while the heuristic matures.
- MUST NOT invoke the V3 orchestrator: guardrails' _active_v3_sessions
  recursion guard would trip (and a nested pipeline inside autoresearch would
  be a disaster anyway). This module only calls llm.chat() for the edit
  proposal and touches decision_outcomes / agent_skills / rejected_skill_edits.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# Keys MUST match each module's AGENT_NAME (the same strings used by
# agent_tool_telemetry and prism_agent_caller logs) — note the v3_ prefix.
# A bare "junior_analyst" key would silently never match at load time.
TARGET_AGENTS: dict[str, str] = {
    "v3_junior_analyst": "First-pass screener: triages the ticker and frames the questions the desk should answer.",
    "v3_fundamental_analyst": "Fundamentals: valuation, earnings quality, balance sheet, filings and guidance.",
    "v3_quant_analyst": "Quant/technicals: price action, indicators, volatility, statistical signals.",
    "v3_bull_agent": "Bull advocate: builds the strongest evidence-based long case in the debate.",
    "v3_bear_agent": "Bear advocate: builds the strongest evidence-based short/avoid case in the debate.",
    "v3_regime_engine": "Regime classifier: maps macro conditions to a market regime and its playbook.",
    "v3_board_of_directors": "Board: final risk-weighted vote on the trade after the debate.",
}

# Acceptance gate: simulated score must beat baseline by this much.
MIN_SCORE_DELTA = 0.005
# Cold-start guard: need at least this many resolved directional outcomes.
MIN_RESOLVED_ROWS = 5
BASELINE_WINDOW_ROWS = 10
# Skill docs ride in every system prompt — keep them small.
MAX_SKILL_CHARS = 4000
# Per-agent LLM proposal timeout and an overall wall-clock budget so a slow
# LLM can't push autoresearch toward its 30-minute stale threshold.
PER_AGENT_TIMEOUT_SEC = 120.0
TOTAL_BUDGET_SEC = 420.0

# Meta-instruction injection patterns a skill doc must never contain — it is
# prepended to a system prompt, so this is a prompt-injection surface.
_FORBIDDEN_PATTERNS = re.compile(
    r"ignore (all |any )?(previous|prior|above)|disregard (the |your )?(system|instructions)"
    r"|you are now|new persona|override.{0,20}instructions",
    re.IGNORECASE,
)

_IMPERATIVE_HINTS = (
    "use ", "prefer ", "avoid ", "check ", "weight", "cap ", "require",
    "always ", "never ", "flag ", "verify ", "cite ", "quantify", "compare ",
    "cross-check", "size ", "discount ",
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _sanitize_skill(text: str) -> str:
    """Strip delimiter/fence artifacts the model copies out of the prompt.

    The optimizer prompt shows the current doc wrapped in `---` rules, and
    models reliably mirror those markers back into `updated_skill`. Left in,
    they get prepended verbatim to a live system prompt (and a leading `---`
    reads as YAML front-matter to some renderers).
    """
    lines = [ln.rstrip() for ln in (text or "").strip().splitlines()]
    while lines and (lines[0].strip() in ("---", "***", "___") or lines[0].strip().startswith("```")):
        lines.pop(0)
    while lines and (lines[-1].strip() in ("---", "***", "___") or lines[-1].strip().startswith("```")):
        lines.pop()
    return "\n".join(lines).strip()


# ── Public entry point ────────────────────────────────────────────────────────

async def propose_and_validate_skill_edits(
    reflection: dict, cycle_id: str, tickers: list | None = None
) -> dict:
    """Run one SkillOpt pass over TARGET_AGENTS. Returns a summary dict.

    Called from autoresearch core inside its own try/except — may raise, but
    prefers to degrade to a summary with a 'skipped' reason.
    """
    from app.config import settings as _settings

    if not bool(getattr(_settings, "SKILLOPT_ENABLED", True)):
        return {"skipped": "disabled"}

    # A rule-based fallback reflection has no LLM-grade recommendations, and an
    # anomalous cycle (degenerate 0.0 sub-scores) is a broken measurement —
    # mutating long-lived skills from either would encode noise.
    if reflection.get("fallback"):
        return {"skipped": "rule_based_reflection"}
    if reflection.get("anomaly"):
        return {"skipped": "anomalous_cycle"}

    baseline = _compute_baseline_score()
    if baseline is None:
        return {"skipped": "cold_start", "min_rows": MIN_RESOLVED_ROWS}

    summary: dict = {"baseline": round(baseline, 4), "updated": [], "rejected": 0, "skipped": 0}
    t0 = time.monotonic()

    for agent_name, role in TARGET_AGENTS.items():
        if (time.monotonic() - t0) > TOTAL_BUDGET_SEC:
            logger.warning(
                "[SkillOpt] wall-clock budget (%ds) exhausted — skipping remaining agents",
                int(TOTAL_BUDGET_SEC),
            )
            summary["skipped"] += 1
            continue
        try:
            outcome = await _optimize_one_agent(
                agent_name, role, reflection, cycle_id, baseline
            )
            if outcome == "updated":
                summary["updated"].append(agent_name)
            elif outcome == "rejected":
                summary["rejected"] += 1
            else:
                summary["skipped"] += 1
        except Exception as e:  # noqa: BLE001 — one agent's failure must not stop the rest
            logger.warning("[SkillOpt] %s failed (non-fatal): %s", agent_name, e)
            summary["skipped"] += 1

    if summary["updated"]:
        try:
            from app.autoresearch.skill_loader import invalidate_skill_cache
            invalidate_skill_cache()
        except Exception as e:  # noqa: BLE001
            logger.debug("[SkillOpt] cache invalidation failed: %s", e)

    return summary


# ── Baseline + heuristic validation ──────────────────────────────────────────

def _compute_baseline_score() -> float | None:
    """Confidence-weighted outcome score over the last resolved directional
    decisions (WIN=1, FLAT=0.5, LOSS=0). None when there are too few rows to
    say anything (cold start)."""
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT outcome, confidence FROM decision_outcomes "
                "WHERE resolved_at IS NOT NULL AND action IN ('BUY', 'SELL') "
                "AND outcome IN ('WIN', 'LOSS', 'FLAT') "
                "ORDER BY resolved_at DESC LIMIT %s",
                [BASELINE_WINDOW_ROWS],
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("[SkillOpt] baseline query failed: %s", e)
        return None

    if not rows or len(rows) < MIN_RESOLVED_ROWS:
        return None

    weights = {"WIN": 1.0, "FLAT": 0.5, "LOSS": 0.0}
    num = 0.0
    den = 0.0
    for outcome, confidence in rows:
        conf = float(confidence or 0)
        if conf <= 0:
            conf = 50.0  # unknown conviction — neutral weight, not zero
        num += conf * weights.get(outcome, 0.0)
        den += conf
    return (num / den) if den > 0 else None


def _simulate_score_with_skill(
    candidate: str, current: str, baseline: float, reflection: dict
) -> float:
    """Heuristic simulated score: baseline plus content-quality adjustments.

    NOT a replay — a true replay would re-run the V3 pipeline on historical
    data. These checks reward specific, actionable, reflection-grounded edits
    and penalize vague or near-noop ones; MIN_SCORE_DELTA is the matching low
    acceptance bar.
    """
    delta = 0.0
    lowered = candidate.lower()

    # Specificity: concrete numbers/thresholds beat platitudes.
    if re.search(r"\d", candidate):
        delta += 0.004
    # Actionability: imperative guidance the agent can actually follow.
    if any(h in lowered for h in _IMPERATIVE_HINTS):
        delta += 0.004
    # Grounding: overlaps this cycle's reflection recommendations.
    recs = " ".join(str(r) for r in (reflection.get("recommendations") or [])).lower()
    rec_terms = {w for w in re.findall(r"[a-z]{5,}", recs)}
    cand_terms = {w for w in re.findall(r"[a-z]{5,}", lowered)}
    if rec_terms and len(rec_terms & cand_terms) >= 3:
        delta += 0.004
    # Substantive but bounded.
    if 150 <= len(candidate) <= 2500:
        delta += 0.003
    # Near-noop: barely differs from the current doc. The penalty must outweigh
    # every bonus combined (max +0.015) so a cosmetic edit can't clear the gate.
    if current:
        import difflib
        if difflib.SequenceMatcher(None, candidate, current).ratio() > 0.95:
            delta -= 0.02
    # Vague filler with no imperative content.
    if not any(h in lowered for h in _IMPERATIVE_HINTS):
        delta -= 0.01

    return baseline + delta


# ── Per-agent optimization ───────────────────────────────────────────────────

async def _optimize_one_agent(
    agent_name: str,
    role: str,
    reflection: dict,
    cycle_id: str,
    baseline: float,
) -> str:
    """Returns 'updated' | 'rejected' | 'skipped'."""
    current_text, current_version = _load_skill(agent_name)

    prompt = _build_optimizer_prompt(agent_name, role, current_text, reflection)
    proposal = await _call_optimizer_llm(agent_name, prompt)
    if proposal is None:
        return "skipped"

    action = str(proposal.get("action", "SKIP")).upper()
    rationale = str(proposal.get("rationale", ""))[:500]
    candidate = _sanitize_skill(str(proposal.get("updated_skill") or ""))

    if action == "SKIP" or not candidate:
        return "skipped"

    cand_hash = _hash(candidate)

    # ── Poison / injection / size gate ──
    reject_reason = None
    try:
        from app.utils.poison_guard import is_poisoned_response
        if is_poisoned_response(candidate):
            reject_reason = "poison_guard"
    except Exception:  # noqa: BLE001 — guard unavailable ≠ candidate bad
        pass
    if reject_reason is None and _FORBIDDEN_PATTERNS.search(candidate):
        reject_reason = "meta_instruction_injection"
    if reject_reason is None and len(candidate) > MAX_SKILL_CHARS:
        reject_reason = f"too_long ({len(candidate)} > {MAX_SKILL_CHARS})"
    if reject_reason is None and cand_hash == _hash(current_text):
        return "skipped"  # byte-identical no-op

    if reject_reason:
        _log_rejection(agent_name, cand_hash, cycle_id, reject_reason, None, rationale)
        logger.info("[SkillOpt] %s rejected: %s", agent_name, reject_reason)
        return "rejected"

    # ── Heuristic score gate ──
    simulated = _simulate_score_with_skill(candidate, current_text, baseline, reflection)
    score_delta = simulated - baseline
    if score_delta <= MIN_SCORE_DELTA:
        _log_rejection(
            agent_name, cand_hash, cycle_id,
            f"score_gate (delta {score_delta:+.4f} <= {MIN_SCORE_DELTA})",
            score_delta, rationale,
        )
        logger.info(
            "[SkillOpt] %s rejected by score gate (delta %+.4f)", agent_name, score_delta
        )
        return "rejected"

    _save_skill(
        agent_name=agent_name,
        skill_text=candidate,
        skill_hash=cand_hash,
        cycle_id=cycle_id,
        score=simulated,
        action=action,
        rationale=rationale,
        new_version=current_version + 1,
    )
    logger.info(
        "[SkillOpt] %s updated to v%d (%s, delta %+.4f): %.80s…",
        agent_name, current_version + 1, action, score_delta, rationale,
    )
    return "updated"


def _build_optimizer_prompt(
    agent_name: str, role: str, current_skill: str, reflection: dict
) -> str:
    recs = reflection.get("recommendations") or []
    health = reflection.get("system_health", "unknown")
    summary = str(reflection.get("summary", ""))[:600]
    recs_block = "\n".join(f"- {str(r)[:300]}" for r in recs[:5]) or "- (none)"
    current_block = current_skill.strip() or "(no skill doc yet — this would be version 1)"

    return (
        f"You maintain the persistent SKILL DOC for one trading agent. The doc is a short "
        f"markdown block prepended to that agent's system prompt every cycle, so it must be "
        f"durable guidance, not commentary on a single cycle.\n\n"
        f"AGENT: {agent_name}\n"
        f"ROLE: {role}\n\n"
        f"CURRENT SKILL DOC:\n---\n{current_block}\n---\n\n"
        f"THIS CYCLE'S AUDIT REFLECTION\n"
        f"System health: {health}\n"
        f"Summary: {summary}\n"
        f"Recommendations:\n{recs_block}\n\n"
        f"TASK: Propose at most ONE edit to the skill doc that would plausibly improve this "
        f"agent's future decisions. Rules:\n"
        f"- Keep the doc under 1500 characters: 3-8 imperative bullet points, specific and "
        f"checkable (thresholds, data sources, failure modes), no restating the agent's role.\n"
        f"- Only encode durable lessons; drop bullets that no longer earn their space.\n"
        f"- If nothing clearly improves the doc, choose SKIP. SKIP is the correct default.\n\n"
        f"Output ONLY a JSON object:\n"
        f'{{"action": "ADD" | "DELETE" | "REPLACE" | "SKIP", '
        f'"rationale": "<one sentence>", '
        f'"updated_skill": "<the COMPLETE new skill doc text, or empty string on SKIP>"}}'
    )


async def _call_optimizer_llm(agent_name: str, prompt: str) -> dict | None:
    """One LLM call at low priority. None on SKIP-shaped failure of any kind."""
    try:
        from app.services.prism_agent_caller import llm, Priority
        response, _tokens, _elapsed = await asyncio.wait_for(
            llm.chat(
                system=(
                    "You are a skill-library optimizer for a multi-agent trading system. "
                    "Output valid JSON only."
                ),
                user=prompt,
                temperature=0.2,
                max_tokens=2048,
                agent_name="skillopt_optimizer",
                ticker="_system",
                priority=Priority.LOW,
            ),
            timeout=PER_AGENT_TIMEOUT_SEC,
        )
        from app.utils.text_utils import parse_json_response
        parsed = parse_json_response(response)
        if not isinstance(parsed, dict) or "action" not in parsed:
            logger.debug("[SkillOpt] %s: unparseable optimizer output", agent_name)
            return None
        return parsed
    except Exception as e:  # noqa: BLE001 — a failed proposal is just a SKIP
        logger.warning("[SkillOpt] optimizer LLM call failed for %s: %s", agent_name, e)
        return None


# ── Persistence ──────────────────────────────────────────────────────────────

def _load_skill(agent_name: str) -> tuple[str, int]:
    """Active skill text + version for an agent; ("", 0) when none exists."""
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT skill_text, version FROM agent_skills "
                "WHERE agent_name = %s AND status = 'active' "
                "ORDER BY version DESC LIMIT 1",
                [agent_name],
            ).fetchone()
        if row:
            return (row[0] or "", int(row[1] or 0))
    except Exception as e:  # noqa: BLE001
        logger.debug("[SkillOpt] _load_skill failed for %s: %s", agent_name, e)
    return ("", 0)


def _save_skill(
    *,
    agent_name: str,
    skill_text: str,
    skill_hash: str,
    cycle_id: str,
    score: float,
    action: str,
    rationale: str,
    new_version: int,
) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE agent_skills SET status = 'archived' "
            "WHERE agent_name = %s AND status = 'active'",
            [agent_name],
        )
        db.execute(
            "INSERT INTO agent_skills "
            "(agent_name, version, skill_text, skill_hash, cycle_id, score, action, rationale, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active')",
            [agent_name, new_version, skill_text, skill_hash, cycle_id,
             round(float(score), 4), action, rationale],
        )


def _log_rejection(
    agent_name: str,
    skill_hash: str,
    cycle_id: str,
    reason: str,
    score_delta: float | None,
    rationale: str,
) -> None:
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO rejected_skill_edits "
                "(agent_name, skill_hash, cycle_id, reason, score_delta, rationale) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [agent_name, skill_hash, cycle_id, reason,
                 round(float(score_delta), 4) if score_delta is not None else None,
                 rationale],
            )
    except Exception as e:  # noqa: BLE001 — audit log, never fatal
        logger.debug("[SkillOpt] rejection log failed for %s: %s", agent_name, e)
