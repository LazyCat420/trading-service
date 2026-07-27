"""
SkillOpt — post-cycle skill mutation for the V3 agent fleet.

After each autoresearch reflection, propose one bounded edit per target agent's
persistent "skill doc" (a short markdown block that skill_loader prepends to
that agent's system prompt) and persist accepted versions to agent_skills.
Rejected candidates are logged to rejected_skill_edits. Modeled on
microsoft/SkillOpt's propose→validate→commit loop, adapted to this repo.

WHAT DECIDES WHETHER A VERSION SURVIVES
---------------------------------------
The version already serving, judged on the decisions it actually governed —
see `scorecard.py`. Before any proposal is paid for, the current version is
scored against its predecessor over resolved `decision_outcomes` rows stamped
with it, and the pass either proceeds, holds, or **reverts**.

It used to be the prose heuristics below, which could not have worked:
`_simulate_score_with_skill` returns `baseline + delta` and the gate compared
`simulated - baseline`, so the realized-outcome term cancelled exactly and every
accept/reject came down to "contains a digit"-class checks. Those checks survive
as a pre-filter on obvious junk, which is what they are good at.

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
from app.autoresearch.scorecard import (
    VERDICT_CONTAMINATED, VERDICT_HEALTHY, VERDICT_REGRESSED, regression_verdict,
)

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
#
# 2026-07-25 audit: this was 4000 while the PROMPT told the model 1500, so the
# stated limit was never enforced and the board doc grew 1146 -> 1812 chars
# over 20 versions, every one of them an accepted REPLACE. A limit the code
# does not enforce is a suggestion, and the model treated it as one.
MAX_SKILL_CHARS = 1800
# What the prompt asks for. Kept below MAX_SKILL_CHARS so there is a little
# slack between "what we request" and "what we reject" — a doc landing at
# 1520 is not worth throwing away.
TARGET_SKILL_CHARS = 1500

# Superseded by scorecard.MATURITY_N (100). Kept only as the documented reason
# the maturity idea exists at all.
#
# The 2026-07-25 audit found the board agent taking 20 versions in ~5 days while
# outcomes need 7 days to resolve, so every version was replaced before a single
# one of its trades matured, and it set this to 25 to stop the churn. That was
# right about the disease and wrong about the dose: bootstrapping 1500 real
# resolved decisions showed two IDENTICAL versions differ by ±0.207 at n=25, so
# a gate there fires at random. n=100 (±0.104) is the first threshold at which
# the comparison means anything.
_SUPERSEDED_MIN_DECISIONS_BEFORE_REEDIT = 25

# A candidate must differ from the current doc by more than cosmetics. The old
# near-noop check compared WHOLE-DOC similarity at a 0.95 threshold; real edits
# landed at 0.84-0.94 and sailed through, including a version whose only change
# was renaming "Conviction-Winrate Scaling" to "Dynamic Conviction Scaling"
# with a byte-identical body. Bullet-level comparison catches that; whole-doc
# similarity structurally cannot.
MIN_NEW_BULLET_CHARS = 40
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

    summary: dict = {
        "baseline": round(baseline, 4), "updated": [], "rejected": 0,
        "skipped": 0, "immature": 0, "rolled_back": [], "contaminated": 0,
    }
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
            elif outcome == "immature":
                # Counted separately from "skipped": a held version is the
                # system working as designed, not a proposal that failed. Rolled
                # into `skipped` it would look like the loop had stalled.
                summary["immature"] += 1
            elif outcome == "rolled_back":
                # The loop's only destructive action, and the one worth seeing
                # at a glance: a version measured worse than what it replaced.
                summary["rolled_back"].append(agent_name)
            elif outcome == "contaminated":
                summary["contaminated"] += 1
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


def _decisions_governed(agent_name: str, version: int) -> int | None:
    """How many RESOLVED decisions ran under this agent's current version.

    Returns None when the question cannot be answered — no version yet, or the
    `skill_versions` column is absent (a deployment older than the 2026-07-25
    migration). None means "unknown" and lets the edit proceed; returning 0
    would freeze every agent forever on any deployment where the stamp is
    missing, which is a far worse failure than one extra edit.

    Rows written before the column existed carry NULL and are deliberately not
    counted: they were governed by *some* version we did not record, and
    assuming it was this one would manufacture a sample that never existed.
    """
    if not version:
        return None
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT count(*) FROM decision_outcomes "
                "WHERE resolved_at IS NOT NULL "
                "AND skill_versions IS NOT NULL "
                "AND (skill_versions ->> %s)::int = %s",
                [agent_name, int(version)],
            ).fetchone()
            governed = int(row[0]) if row else 0
            if governed:
                return governed

            # Zero stamped rows is ambiguous, and getting this wrong freezes the
            # whole fleet. It means EITHER "this version is brand new" (hold it,
            # correct) OR "this version predates the stamp" (its sample is
            # unknowable, and holding on that basis would block every agent for
            # weeks after deploy — every live version on 2026-07-25 predated the
            # column). Distinguish by asking whether the stamp is flowing AT ALL.
            stamped = db.execute(
                "SELECT 1 FROM decision_outcomes "
                "WHERE skill_versions IS NOT NULL LIMIT 1"
            ).fetchone()
        if not stamped:
            return None  # attribution has not started — unknown, not zero
        return 0
    except Exception as e:  # noqa: BLE001
        # Most likely the column does not exist yet. Unknown, not zero.
        logger.debug("[SkillOpt] governed-count unavailable for %s: %s", agent_name, e)
        return None


def _bullets(text: str) -> list[str]:
    """The doc's bullet lines, normalized for comparison.

    Normalization deliberately strips the LABEL (`- **Name**:`) and markdown
    emphasis, so two bullets with the same body under different names compare
    EQUAL. That is the whole point: renaming a rule is not learning one.
    """
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith(("-", "*", "•")):
            continue
        line = line.lstrip("-*• \t")
        # Drop a leading "**Label**:" / "Label:" prefix.
        line = re.sub(r"^\*{0,2}[^:*]{1,60}\*{0,2}\s*:\s*", "", line)
        line = re.sub(r"[*_`#]", "", line).lower()
        line = re.sub(r"[^a-z0-9%.<>= ]+", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            out.append(line)
    return out


def _substantive_change(candidate: str, current: str) -> tuple[bool, str]:
    """Does the candidate add or remove real GUIDANCE, not just wording?

    Returns (is_substantive, reason_when_not).

    Compares normalized bullet BODIES rather than whole-doc text. The old
    whole-doc similarity check at 0.95 never fired on a real edit: measured
    across the board agent's 8 most recent versions, consecutive similarity ran
    0.84-0.94 — every one accepted, including a pure rename. Bullet-level
    comparison is the only form that catches relabeling.
    """
    if not current:
        return True, ""

    cur_set = set(_bullets(current))
    cand_list = _bullets(candidate)
    cand_set = set(cand_list)

    added = [b for b in cand_list if b not in cur_set]
    removed = [b for b in cur_set if b not in cand_set]

    # A deletion is a legitimate edit on its own — dropping a bullet that no
    # longer earns its space is exactly what the prompt asks for.
    if removed and not added:
        return True, ""

    meaningful = [b for b in added if len(b) >= MIN_NEW_BULLET_CHARS]

    # A bullet counts as NEW only if it is not a near-copy of one already
    # present. Appending a word to an existing bullet produces a technically
    # "added" bullet whose content is inherited — the near-noop shape. Compare
    # each candidate bullet against its closest current match.
    import difflib
    genuinely_new = []
    for b in meaningful:
        closest = difflib.get_close_matches(b, list(cur_set), n=1, cutoff=0.0)
        if closest and difflib.SequenceMatcher(None, b, closest[0]).ratio() > 0.85:
            continue  # a reworded copy of an existing rule, not a new one
        genuinely_new.append(b)

    if not genuinely_new:
        if added:
            return False, f"cosmetic_only ({len(added)} reworded/trivial bullet(s))"
        return False, "no_bullet_changed (rename or reformat only)"
    return True, ""


def _simulate_score_with_skill(
    candidate: str, current: str, baseline: float, reflection: dict
) -> float:
    """Heuristic content score: baseline plus quality adjustments.

    ⚠ NOT a measure of trading accuracy, and it never was. A true replay would
    re-run the V3 pipeline on historical data; `decision_outcomes` carries no
    `agent_name`, so per-agent realized accuracy is not attributable either.
    This only asks "is this doc better WRITTEN" — specific, actionable, and
    grounded in the cycle's own reflection.

    2026-07-25 audit: the previous version awarded every bonus to nearly every
    candidate. Measured over the board agent's real stored versions, 6 of 7
    accepted edits scored `+0.0150` — the exact maximum — and all 66 recorded
    rejections scored exactly `-0.0050`. It emitted one of two values, so it
    ranked nothing. Worse, fed a genuine edit and deliberate keyword soup, it
    scored the SOUP higher, because "contains a digit" and "contains an
    imperative verb" are satisfied by any rewrite.

    The fix is proportional credit rather than flat bonuses, plus penalties for
    the specific degenerate shapes that were passing. The structural check
    (`_substantive_change`) runs SEPARATELY and is not scored — a rename must
    be rejected outright, not out-pointed.
    """
    delta = 0.0
    lowered = candidate.lower()
    cand_bullets = _bullets(candidate)

    # Specificity: proportional to how many bullets carry a real threshold,
    # not a flat bonus for one digit anywhere in the document.
    if cand_bullets:
        with_numbers = sum(1 for b in cand_bullets if re.search(r"\d", b))
        delta += 0.004 * min(1.0, with_numbers / len(cand_bullets))

    # Actionability: same treatment — share of bullets that are imperative.
    if cand_bullets:
        imperative = sum(
            1 for b in cand_bullets if any(h in b for h in _IMPERATIVE_HINTS)
        )
        delta += 0.004 * min(1.0, imperative / len(cand_bullets))

    # Grounding: the NEW material must overlap this cycle's reflection.
    # Scoring overlap across the whole doc rewarded a candidate for text it
    # inherited, and let keyword-stuffing fake it — the reason soup outscored
    # a real edit in the 2026-07-25 measurement. Only newly-added bullets count.
    recs = " ".join(str(r) for r in (reflection.get("recommendations") or [])).lower()
    rec_terms = {w for w in re.findall(r"[a-z]{5,}", recs)}
    if rec_terms:
        cur_set = set(_bullets(current))
        new_text = " ".join(b for b in cand_bullets if b not in cur_set)
        new_terms = {w for w in re.findall(r"[a-z]{5,}", new_text)}
        if len(rec_terms & new_terms) >= 3:
            delta += 0.004

    # Shape: the prompt asks for 3-8 bullets under TARGET_SKILL_CHARS. Reward
    # docs that actually comply instead of any doc over 150 chars.
    if 3 <= len(cand_bullets) <= 8 and len(candidate) <= TARGET_SKILL_CHARS:
        delta += 0.003

    # ── Penalties for the shapes that were slipping through ──

    # Bloat: the doc rides in every system prompt every cycle.
    if len(candidate) > TARGET_SKILL_CHARS:
        overshoot = (len(candidate) - TARGET_SKILL_CHARS) / TARGET_SKILL_CHARS
        delta -= min(0.02, 0.01 * overshoot * 4)

    # Accretion: more bullets than the prompt allows means nothing was dropped.
    if len(cand_bullets) > 8:
        delta -= 0.004 * (len(cand_bullets) - 8)

    # Repetition — the keyword-soup tell. A bullet that says "mandate" nine
    # times is gaming the imperative check, not giving nine instructions.
    # Measured on the WHOLE doc AND on the newly-added text separately: a long
    # healthy doc dilutes one stuffed bullet's ratio below any useful
    # threshold, so whole-doc measurement alone cannot see it.
    cur_bullets_set = set(_bullets(current))
    added_text = " ".join(b for b in cand_bullets if b not in cur_bullets_set)
    for scope, min_words in ((lowered, 20), (added_text, 12)):
        words = re.findall(r"[a-z]{4,}", scope)
        if len(words) < min_words:
            continue
        top = max((words.count(w) for w in set(words)), default=0)
        if top / len(words) > 0.15:
            delta -= 0.012
            break

    # Vague filler with no imperative content at all.
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
    """Returns 'updated' | 'rejected' | 'skipped' | 'immature'."""
    current_text, current_version = _load_skill(agent_name)

    # ── Measured gate: did the CURRENT version help, hurt, or is it unknown? ──
    # This replaces a raw sample count. The count could only ever say "enough
    # rows exist"; the scorecard says whether those rows mean anything and what
    # they mean. Its verdicts are terminal in both directions:
    #
    #   CONTAMINATED  the window's tools were broken, so its decisions say
    #                 nothing about the doc — neither promote nor revert on it
    #   IMMATURE      too few resolved decisions for the ±0.10 noise band
    #   REGRESSED     measurably worse than its predecessor → revert
    #   HEALTHY       proceed to propose the next edit
    #
    # `_decisions_governed` is still consulted first for the one thing the
    # scorecard cannot express: whether version stamping is running at all. On a
    # deployment predating the stamp it returns None, and freezing the whole
    # fleet on missing telemetry is a worse failure than one extra edit.
    if _decisions_governed(agent_name, current_version) is None:
        logger.info(
            "[SkillOpt] %s: version stamping unavailable — proceeding unmeasured",
            agent_name,
        )
    else:
        card = regression_verdict(agent_name, current_version)
        logger.info("[SkillOpt] %s", card.summary())

        if card.verdict == VERDICT_REGRESSED:
            if _rollback_skill(agent_name, current_version, cycle_id, card.detail):
                return "rolled_back"
            return "immature"  # predecessor unavailable; hold rather than churn

        if card.verdict == VERDICT_CONTAMINATED:
            logger.info("[SkillOpt] %s held: %s", agent_name, card.detail)
            return "contaminated"

        if card.verdict != VERDICT_HEALTHY:
            logger.info("[SkillOpt] %s held: %s", agent_name, card.detail)
            return "immature"

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
    # Size gate, with an escape hatch for docs that are ALREADY over budget.
    # Tightening MAX_SKILL_CHARS from 4000 to 1800 left 5 of 7 live docs above
    # the target and one (the board's, 1811) above the ceiling itself. Without
    # this, an over-budget doc is frozen forever: every candidate at or near
    # its size is rejected, so it can never shrink back. An edit that makes an
    # over-budget doc SMALLER is always allowed through this gate.
    if reject_reason is None and len(candidate) > MAX_SKILL_CHARS:
        shrinking = len(current_text) > MAX_SKILL_CHARS and len(candidate) < len(current_text)
        if not shrinking:
            reject_reason = f"too_long ({len(candidate)} > {MAX_SKILL_CHARS})"
        else:
            logger.info(
                "[SkillOpt] %s: over-budget doc shrinking %d -> %d, allowed",
                agent_name, len(current_text), len(candidate),
            )
    if reject_reason is None and cand_hash == _hash(current_text):
        return "skipped"  # byte-identical no-op

    # Structural gate, deliberately NOT part of the score: a rename must be
    # rejected outright rather than out-pointed by content bonuses. The old
    # near-noop check lived in the scorer and never fired on a real edit.
    if reject_reason is None:
        substantive, why = _substantive_change(candidate, current_text)
        if not substantive:
            reject_reason = why

    if reject_reason:
        _log_rejection(agent_name, cand_hash, cycle_id, reject_reason, None, rationale)
        logger.info("[SkillOpt] %s rejected: %s", agent_name, reject_reason)
        return "rejected"

    # ── Prose pre-filter (NOT a quality gate) ──
    # This was the promotion gate. It could not be: `_simulate_score_with_skill`
    # returns `baseline + delta`, and the gate compared `simulated - baseline`,
    # so the realized-outcome baseline **cancelled exactly** and 100% of every
    # accept/reject came from "contains a digit"-class heuristics. The realized
    # question is now asked above, by the scorecard, against decisions the
    # version actually governed.
    #
    # The heuristics are kept because they are genuinely good at what they are:
    # catching a rewrite that is vaguer or flabbier than what it replaces. That
    # is a pre-filter on obvious junk, and it is labelled as one.
    prose_delta = _simulate_score_with_skill(
        candidate, current_text, baseline, reflection
    ) - baseline
    if prose_delta <= MIN_SCORE_DELTA:
        _log_rejection(
            agent_name, cand_hash, cycle_id,
            f"prose_prefilter (delta {prose_delta:+.4f} <= {MIN_SCORE_DELTA})",
            prose_delta, rationale,
        )
        logger.info(
            "[SkillOpt] %s rejected by prose pre-filter (delta %+.4f)",
            agent_name, prose_delta,
        )
        return "rejected"

    _save_skill(
        agent_name=agent_name,
        skill_text=candidate,
        skill_hash=cand_hash,
        cycle_id=cycle_id,
        score=prose_delta,
        action=action,
        rationale=rationale,
        new_version=current_version + 1,
    )
    logger.info(
        "[SkillOpt] %s updated to v%d (%s, prose %+.4f): %.80s…",
        agent_name, current_version + 1, action, prose_delta, rationale,
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

    # An already-over-budget doc must be told to shrink, or the size gate
    # freezes it: every candidate near its size is rejected, so it can never
    # come back down. See the shrink escape hatch in _optimize_one_agent.
    over_budget_rule = ""
    if len(current_skill) > TARGET_SKILL_CHARS:
        over_budget_rule = (
            f"- THIS DOC IS CURRENTLY OVER BUDGET ({len(current_skill)} chars vs "
            f"{TARGET_SKILL_CHARS}). Your edit MUST make it SHORTER: merge overlapping "
            f"bullets or drop the weakest one. An edit that does not reduce the "
            f"length will be rejected.\n"
        )

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
        f"- Keep the doc under {TARGET_SKILL_CHARS} characters: 3-8 imperative bullet points, specific and "
        f"checkable (thresholds, data sources, failure modes), no restating the agent's role.\n"
        f"- Only encode durable lessons; drop bullets that no longer earn their space.\n"
        f"- Renaming or rewording an existing bullet is NOT an improvement and will be "
        f"rejected. An edit must add genuinely new guidance or remove a bullet.\n"
        f"- The doc is already at its size budget. If you add a bullet, say which one "
        f"you dropped to make room.\n"
        f"{over_budget_rule}"
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


def _rollback_skill(agent_name: str, from_version: int, cycle_id: str,
                    reason: str) -> bool:
    """Revert to the predecessor by APPENDING it as a new version.

    Append-only on purpose. Reactivating the old row would make
    `decision_outcomes.skill_versions` ambiguous — two disjoint periods stamped
    with the same version number, silently pooled into one sample by every
    query in scorecard.py. A fresh version number keeps each period its own
    population, which is the whole basis for comparing them.

    The reverted edit is also recorded as a dead end, so the optimizer is not
    handed the same idea again next cycle.
    """
    with get_db() as db:
        prev = db.execute(
            "SELECT skill_text, skill_hash FROM agent_skills "
            "WHERE agent_name = %s AND version = %s",
            [agent_name, int(from_version) - 1],
        ).fetchone()
        if not prev or not prev[0]:
            logger.warning(
                "[SkillOpt] %s v%d regressed but v%d is unavailable — cannot roll back",
                agent_name, from_version, from_version - 1,
            )
            return False
        bad = db.execute(
            "SELECT skill_hash, rationale FROM agent_skills "
            "WHERE agent_name = %s AND version = %s",
            [agent_name, int(from_version)],
        ).fetchone()

    _save_skill(
        agent_name=agent_name,
        skill_text=prev[0],
        skill_hash=prev[1],
        cycle_id=cycle_id,
        score=0.0,
        action="ROLLBACK",
        rationale=f"reverted v{from_version} to v{from_version - 1}: {reason}"[:500],
        new_version=int(from_version) + 1,
    )
    if bad:
        _log_rejection(
            agent_name, bad[0] or "", cycle_id,
            f"rolled_back_v{from_version}", None,
            f"measured regression: {reason}"[:500],
        )
    try:
        from app.autoresearch.skill_loader import invalidate_skill_cache
        invalidate_skill_cache(agent_name)
    except Exception as e:  # noqa: BLE001 — TTL backstops a missed invalidation
        logger.debug("[SkillOpt] cache invalidation after rollback failed: %s", e)

    logger.warning(
        "[SkillOpt] %s ROLLED BACK v%d -> v%d (served as v%d): %s",
        agent_name, from_version, from_version - 1, from_version + 1, reason,
    )
    return True


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
