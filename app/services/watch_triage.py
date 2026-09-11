"""watch_triage — Stage 1. Cheap, deterministic, explainable. No LLM.

THE FINDING THIS MODULE EXISTS FOR
----------------------------------
Measured 2026-09-06 over `watch_events`: the daily wake budget of 6 was spent
**exactly 6 of 6 on every one of the last 21 days**. The desk is not idle with
occasional trips — it is saturated, permanently. And `_spend_wake_budget` ranks
only the candidates that happen to be present in ONE 15-minute sweep, then
fires at most one, so the day's six wakes go to the first six sweeps that
produce any candidate at all. The 2026-09-06 firing times — 06:06, 08:15,
09:31, 10:03, 11:48, 12:59 — are monotonically early, not ranked.

**The scarce resource is the wake, and nothing allocated it.** Better triggers
cannot fix that; they only change which six arrive first. So the fix here is not
a bigger budget or a sharper trigger, it is a SCORE FLOOR that rises as the
day's budget depletes: with all six wakes left a mid-score trip may fire; with
one left, only a top trip may. That makes allocation depend on score rather than
clock position without needing to see the future.

WHAT IT SCREENS
---------------
Every rejection is a named, reachable reason, and every score component is
recorded separately, because an unexplainable ranking cannot be audited later —
which is the whole reason the current one could saturate for 21 days unnoticed.

Pure functions. Every input is passed in; nothing here reads a clock or a
database, so the entire layer is testable against frozen fixtures.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.services import watch_schema
from app.services.watch_catalysts import catalyst_urgency

logger = logging.getLogger(__name__)

# ── Rejection reasons. Every one is exercised by a test; a reason that cannot
#    be reached is a screen that does not screen. ─────────────────────────────
REJECT_DUPLICATE = "duplicate_event"
REJECT_STALE_EVIDENCE = "stale_evidence"
REJECT_CADENCE = "reanalysis_cadence"
REJECT_TICKER_BUDGET = "ticker_budget_exhausted"
REJECT_MARKET_CLOSED = "market_closed"
REJECT_OFF_THESIS = "evidence_off_thesis"
REJECT_BELOW_FLOOR = "below_score_floor"

ALL_REJECT_REASONS = (
    REJECT_DUPLICATE, REJECT_STALE_EVIDENCE, REJECT_CADENCE,
    REJECT_TICKER_BUDGET, REJECT_MARKET_CLOSED, REJECT_OFF_THESIS,
    REJECT_BELOW_FLOOR,
)

# Defaults; the live values come from parameter_store via the caller.
DEFAULT_EVIDENCE_MAX_AGE_H = 36
DEFAULT_MIN_REANALYSIS_H = 12
DEFAULT_MAX_ANALYSES_PER_WEEK = 3

# Trigger types whose evidence is a market observation rather than a document.
_PRICE_TRIGGERS = {"price_above", "price_below", "pct_change", "rsi", "volume_spike"}


@dataclass
class Evidence:
    """One thing that happened, normalised. The trip's proof.

    `observed_at` is when the WORLD produced it (a headline's collection time, a
    price print), never when we looked. Using look-time would make every
    evidence item permanently fresh and the freshness gate a no-op.
    """
    kind: str                       # news | price | clock | calendar | position
    text: str = ""
    observed_at: datetime | None = None
    source_id: str | None = None
    source_url: str | None = None
    source: str = ""                # detected | provider | price | calendar | clock
    value: float | None = None
    trigger_type: str = ""

    def event_key(self, ticker: str) -> str:
        """Idempotency key. Same world-event => same key, regardless of when a
        sweep happened to notice it.

        Deliberately built from the NORMALISED text and the observation time,
        not from the sweep time or a uuid: a key that varies per sweep dedups
        nothing, which is how the same NVDA headline woke four cycles in one
        hour before the news-dedup anchor was added.
        """
        norm = re.sub(r"\W+", " ", (self.text or "").lower()).strip()
        stamp = self.observed_at.isoformat() if self.observed_at else ""
        raw = f"{ticker}|{self.kind}|{self.trigger_type}|{norm}|{stamp}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class TriageInputs:
    """Everything Stage 1 needs, passed in. No hidden reads."""
    ticker: str
    watch: dict
    evidence: Evidence
    now: datetime
    market_open: bool = True
    calendar: dict = field(default_factory=dict)
    held_qty: float = 0.0
    position_weight: float = 0.0        # fraction of portfolio, 0-1
    unrealized_pct: float | None = None  # -0.12 == 12% underwater
    last_analysis_at: datetime | None = None
    analyses_this_week: int = 0
    seen_event_keys: frozenset = frozenset()
    budget_left: int = 0
    budget_total: int = 6
    # Policy knobs
    evidence_max_age_h: int = DEFAULT_EVIDENCE_MAX_AGE_H
    min_reanalysis_h: int = DEFAULT_MIN_REANALYSIS_H
    max_analyses_per_week: int = DEFAULT_MAX_ANALYSES_PER_WEEK


@dataclass
class TriageVerdict:
    eligible: bool
    reject_reason: str | None
    score: float
    components: dict
    detail: dict
    event_key: str


# ─── Score components ───────────────────────────────────────────────────────
def _materiality(inp: TriageInputs) -> tuple[float, dict]:
    """How much this evidence could matter, on its own terms.

    A price level the desk itself chose is a stronger statement than a headline:
    the level was a commitment made when the thesis was written, the headline is
    whatever a vendor published. Measured composition of the last 30 trips —
    27 news, 3 staleness, ZERO price — is the shape this is correcting.

    `provider` attribution (the vendor's own entity tagging, which nothing has
    verified against the body) scores strictly below `detected`. It is admitted,
    not trusted, so the trust level becomes visible in telemetry instead of
    being silently equal.
    """
    ev = inp.evidence
    base = {
        "price_below": 0.9,     # our own invalidation level, breached
        "price_above": 0.7,     # our own target, hit
        "pct_change": 0.6,
        "volume_spike": 0.4,
        "rsi": 0.35,
        "news": 0.5,
        "staleness": 0.2,       # a clock is not an event
    }.get(ev.trigger_type, 0.3)

    detail = {"trigger_type": ev.trigger_type, "base": base}
    if ev.kind == "news":
        if ev.source == "provider":
            base *= 0.6
            detail["provider_unverified_discount"] = True
        elif ev.source != "detected":
            base *= 0.8
            detail["attribution"] = ev.source or "unknown"
    return max(0.0, min(1.0, base)), detail


def _portfolio_risk(inp: TriageInputs) -> tuple[float, dict]:
    """Real money exposed outranks a name we merely watch.

    Concentration and drawdown both raise it: a 12%-underwater 8% position is
    the case where a wasted wake costs the most.
    """
    if inp.held_qty <= 0:
        return 0.0, {"held": False}
    score = 0.4
    detail = {"held": True, "weight": round(inp.position_weight, 4)}
    score += min(0.3, inp.position_weight * 3.0)     # concentration
    if inp.unrealized_pct is not None:
        detail["unrealized_pct"] = round(inp.unrealized_pct, 4)
        if inp.unrealized_pct <= -0.10:
            score += 0.3
        elif inp.unrealized_pct <= -0.05:
            score += 0.15
    return max(0.0, min(1.0, score)), detail


def _thesis_uncertainty(inp: TriageInputs) -> tuple[float, dict]:
    """How unsettled the prior decision was.

    A confident BUY needs less revisiting than a 0.45-confidence HOLD whose own
    author wrote down an open question. This is the term that the stored
    `resolution_condition` makes computable at all — before it, there was
    nothing on the record that said how sure the agent was.
    """
    dc = (inp.watch or {}).get("decision_context") or {}
    conf = dc.get("confidence")
    rc = watch_schema.get_resolution_condition(inp.watch)
    detail: dict = {"has_resolution_condition": rc is not None}

    if conf is None:
        # Not "certain" — UNKNOWN. Scoring an absent confidence as 0 uncertainty
        # would make every legacy watch look maximally settled, which is exactly
        # backwards: we know least about the ones that recorded least.
        score = 0.5
        detail["confidence"] = None
    else:
        score = 1.0 - float(conf)
        detail["confidence"] = round(float(conf), 3)

    if rc and rc.get("resolves_by"):
        detail["resolves_by"] = rc["resolves_by"]
    return max(0.0, min(1.0, score)), detail


def _expected_information_gain(inp: TriageInputs) -> tuple[float, dict]:
    """How much a cycle now would actually learn.

    Two things destroy it: analysing the same facts twice (nothing new since the
    last look), and analysing hours before a known event that will rewrite the
    answer. Both are measurable without a model.
    """
    detail: dict = {}
    score = 0.5
    if inp.last_analysis_at is not None:
        hours = (inp.now - inp.last_analysis_at).total_seconds() / 3600.0
        detail["hours_since_analysis"] = round(hours, 1)
        # Elapsed time is cadence, not evidence that information changed.
        score = 0.0
    else:
        detail["hours_since_analysis"] = None
        score = 0.0

    ev = inp.evidence
    if ev.kind == "news" and ev.text:
        rc = watch_schema.get_resolution_condition(inp.watch)
        overlap = _resolution_overlap(rc, ev.text)
        detail["resolution_overlap"] = overlap
        if overlap is None:
            # No stored condition to compare against. Do not credit and do not
            # punish — record it, so the legacy penalty is the only place the
            # missing field costs anything, and it costs it once.
            detail["resolution_overlap_reason"] = "no_resolution_condition"
        elif overlap > 0:
            score = 0.8 if ev.source == "detected" else 0.4
    elif ev.kind == "price" and ev.observed_at is not None:
        score = 0.6  # a newly observed, evaluated condition
    detail["basis"] = "observed_condition_or_question_relevance"
    return max(0.0, min(1.0, score)), detail


def _resolution_overlap(rc: dict | None, text: str) -> int | None:
    """How many DISCRIMINATING terms of the resolution condition this text hits.

    Counted over the SPECIFIC tier — the terms left after generic equity
    vocabulary ("earnings", "q3", "revenue", "guidance") is removed. Counting
    the full tier passed the live "Adobe Stock: Citi Sees 'Achievable Set-Up'
    Ahead Of Q3 Earnings" headline against a net-interest-margin question on
    the tokens "q3" and "earnings" alone: two words that say both texts are
    about stocks, and nothing more.

    When a condition is stated ENTIRELY in generic words the specific tier is
    empty; the full tier is used instead. Failing closed there would make such
    a watch permanently unwakeable, which is a worse error than a loose screen
    on a vaguely-worded question.

    None means "cannot screen" (no stored condition) and MUST NOT be read as
    zero — a fail-open screen certifies exactly the evidence it cannot read.
    """
    if not rc:
        return None
    terms = rc.get("resolving_terms")
    if not terms:
        terms = watch_schema.resolution_terms(
            rc.get("open_question") or "", rc.get("resolving_fact") or "")
    if not terms:
        return None
    specific = rc.get("resolving_terms_specific")
    if specific is None:
        specific = watch_schema.specific_terms(terms)
    screen = specific or terms
    low = (text or "").lower()
    return sum(1 for t in screen if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", low))


def _recent_analysis_penalty(inp: TriageInputs) -> tuple[float, dict]:
    """Punish re-analysing a name we just looked at, and one we look at often.

    NVDA was 4 of the last 30 trips. Without this term one noisy megacap holds a
    share of every single day's budget.
    """
    detail: dict = {"analyses_this_week": inp.analyses_this_week}
    penalty = 0.0
    if inp.last_analysis_at is not None:
        hours = (inp.now - inp.last_analysis_at).total_seconds() / 3600.0
        detail["hours_since_analysis"] = round(hours, 1)
        if hours < inp.min_reanalysis_h:
            penalty += 0.6
        elif hours < inp.min_reanalysis_h * 2:
            penalty += 0.3
    penalty += 0.15 * max(0, inp.analyses_this_week - 1)
    return max(0.0, min(1.5, penalty)), detail


def _compute_cost_penalty(inp: TriageInputs) -> tuple[float, dict]:
    """The cost of the wake itself, rising as the day's budget runs out.

    A wake is not free and the last one of the day is the most expensive thing
    the desk can spend, because it forecloses every trip that comes after it.
    """
    total = max(1, inp.budget_total)
    used = max(0, total - max(0, inp.budget_left))
    frac_used = used / total
    return round(0.4 * frac_used, 4), {"budget_left": inp.budget_left,
                                       "budget_total": total,
                                       "fraction_used": round(frac_used, 3)}


def _legacy_penalty(inp: TriageInputs) -> tuple[float, dict]:
    """The cost of a watch that recorded no resolution condition.

    Its own component, never folded into another, so §6's replay can re-score
    with it set to zero and show what the penalty actually bought.
    """
    if watch_schema.get_resolution_condition(inp.watch) is not None:
        return 0.0, {"legacy": False}
    return 0.25, {"legacy": True, "reason": "no_resolution_condition_recorded"}


# ── Floor calibration ────────────────────────────────────────────────────────
# The score is a SUM of up to five positive components (each in [0,1]) less two
# penalties, so it does NOT live in [0,1] and a floor written on that scale
# would never bind. These two constants are calibrated against a measured
# distribution over representative candidates (2026-09-06):
#
#     0.97  provider-attributed news on a ticker analysed 20h ago     LOW
#     1.45  legacy watch, staleness clock, no position               LOW
#     1.95  on-thesis news, 9d since analysis, unheld                 MID
#     2.41  on-thesis news, 9d since analysis, small position         MID
#     3.83  post-earnings on-thesis news, held, low confidence        HIGH
#     4.52  held + underwater + stop breached + post-earnings         HIGH
#
# FLOOR_FULL_BUDGET admits LOW-MID and above; FLOOR_LAST_WAKE admits only HIGH.
# The numbers are the calibration, not the contract — the contract is the
# ORDERING, which test_the_floor_brackets_the_measured_distribution asserts
# against those cases, so a rescaling of any component fails loudly here
# instead of silently opening or closing the desk.
FLOOR_FULL_BUDGET = 1.2
FLOOR_LAST_WAKE = 3.0


def score_floor(budget_left: int, budget_total: int) -> float:
    """The minimum score a trip must beat, given how much budget is left.

    This is the saturation fix. With the full budget the floor is low and a
    mid-value trip may fire; as wakes are spent the floor rises, so late-day
    trips must be better than the ones that already went. Monotone
    non-increasing in `budget_left` by construction, which
    test_the_floor_is_monotone_in_budget checks across the whole range.
    """
    total = max(1, budget_total)
    left = max(0, min(budget_left, total))
    if left <= 0:
        return float("inf")          # nothing clears an exhausted budget
    frac_left = left / total
    span = FLOOR_LAST_WAKE - FLOOR_FULL_BUDGET
    return round(FLOOR_LAST_WAKE - span * frac_left, 4)


# ─── The verdict ────────────────────────────────────────────────────────────
def triage(inp: TriageInputs) -> TriageVerdict:
    """Score and screen one candidate trip. Pure; never raises on odd input."""
    ev = inp.evidence
    key = ev.event_key(inp.ticker)

    def _reject(reason: str, detail: dict) -> TriageVerdict:
        return TriageVerdict(False, reason, 0.0, {}, detail, key)

    # ── Cheapest rejections first ──
    if key in inp.seen_event_keys:
        return _reject(REJECT_DUPLICATE, {"event_key": key})

    if ev.kind in ("news", "price") and ev.observed_at is None:
        return _reject("unknown_evidence_age", {"source": ev.source, "timestamp_known": False})

    if ev.observed_at is not None:
        age_h = (inp.now - ev.observed_at).total_seconds() / 3600.0
        max_age = 0.25 if ev.kind == "price" else inp.evidence_max_age_h
        if age_h < 0:
            return _reject("future_evidence_timestamp", {"age_hours":age_h})
        if age_h > max_age:
            return _reject(REJECT_STALE_EVIDENCE,
                           {"age_hours": round(age_h, 1), "max": max_age})

    if ev.trigger_type in _PRICE_TRIGGERS and not inp.market_open:
        return _reject(REJECT_MARKET_CLOSED, {"trigger_type": ev.trigger_type})

    if inp.last_analysis_at is not None:
        hours = (inp.now - inp.last_analysis_at).total_seconds() / 3600.0
        if hours < inp.min_reanalysis_h:
            return _reject(REJECT_CADENCE,
                           {"hours_since_analysis": round(hours, 1),
                            "min_hours": inp.min_reanalysis_h})

    if inp.analyses_this_week >= inp.max_analyses_per_week:
        return _reject(REJECT_TICKER_BUDGET,
                       {"analyses_this_week": inp.analyses_this_week,
                        "max": inp.max_analyses_per_week})

    # ── Thesis relevance. Only screens what it CAN screen. ──
    rc = watch_schema.get_resolution_condition(inp.watch)
    overlap = _resolution_overlap(rc, ev.text) if ev.kind == "news" else None
    if ev.kind == "news" and rc is None:
        return _reject("missing_resolution_condition", {
            "reason":"No recorded question connects this news to a decision; qualify the fact before full research",
            "headline":(ev.text or "")[:200]})
    if ev.kind == "news" and overlap == 0:
        # A stored condition exists and this headline touches none of it. This
        # is the gate that refuses "Citigroup delays Fed rate cut forecast" for
        # a watch whose open question is Citi's own net interest margin — the
        # live trip that flipped a decision HOLD -> BUY on a research-desk note.
        return _reject(REJECT_OFF_THESIS,
                       {"resolution_overlap": 0,
                        "open_question": (rc or {}).get("open_question", "")[:200],
                        "headline": (ev.text or "")[:200]})

    # ── Score ──
    mat, d_mat = _materiality(inp)
    cat, d_cat = catalyst_urgency(inp.calendar, inp.now)
    risk, d_risk = _portfolio_risk(inp)
    unc, d_unc = _thesis_uncertainty(inp)
    gain, d_gain = _expected_information_gain(inp)
    recent, d_recent = _recent_analysis_penalty(inp)
    cost, d_cost = _compute_cost_penalty(inp)
    legacy, d_legacy = _legacy_penalty(inp)

    components = {
        # A stale-thesis maintenance review is explicit coverage work, not
        # information gain. It can use an early slot but loses to real events.
        "maintenance_coverage": 0.8 if ev.kind == "clock" and ev.trigger_type == "staleness" else 0.0,
        "materiality": round(mat, 4),
        "catalyst_urgency": round(cat, 4),
        "portfolio_risk": round(risk, 4),
        "thesis_uncertainty": round(unc, 4),
        "expected_information_gain": round(gain, 4),
        "recent_analysis_penalty": -round(recent, 4),
        "compute_cost_penalty": -round(cost, 4),
        "legacy_schema_penalty": -round(legacy, 4),
    }
    # The score is the plain sum of its components, so a dashboard can print the
    # terms and have them add up. A weighting hidden inside the sum would make
    # the printed components a decoration rather than an explanation.
    score = round(sum(components.values()), 4)

    detail = {
        "materiality": d_mat, "catalyst": d_cat, "portfolio": d_risk,
        "uncertainty": d_unc, "information": d_gain, "recent": d_recent,
        "cost": d_cost, "legacy": d_legacy,
        "resolution_overlap": overlap,
        "evidence": {"kind": ev.kind, "trigger_type": ev.trigger_type,
                     "source": ev.source, "source_id":ev.source_id,"source_url":ev.source_url,
                     "text": (ev.text or "")[:300],
                     "observed_at": ev.observed_at},
    }

    floor = score_floor(inp.budget_left, inp.budget_total)
    detail["score_floor"] = floor if floor != float("inf") else None
    if score < floor:
        return TriageVerdict(False, REJECT_BELOW_FLOOR, score, components, detail, key)

    return TriageVerdict(True, None, score, components, detail, key)
