"""watch_policy — the layer that actually decides. Code, never the model.

The Stage-2 planner (`watch_planner`) RECOMMENDS one of four actions. This
module is what turns a recommendation into an outcome, and it applies the same
bounds to the deterministic path and the planner path alike — one seam, so the
two cannot drift.

A planner asking for something out of bounds is **clamped and logged**, never
obeyed and never crashed on. "Clamped" is the important word: rejecting an
out-of-bounds recommendation outright would throw away the judgement along with
the overreach, and the model is usually right about WHAT to look at and wrong
only about WHEN.

THE GUARDRAIL
-------------
The standing risk is a research agent that creates unlimited immediate work for
itself. So: `CLOSE_WATCH` is the only action that reduces work, and the only one
applied without a budget check. Every action that CREATES work passes the global
daily budget, the per-ticker weekly budget, the cadence floor, the score floor
and the event idempotency key. There is no path through this module that
schedules work sooner than the cadence floor allows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.services.watch_triage import (
    REJECT_BELOW_FLOOR, REJECT_TICKER_BUDGET, TriageVerdict, score_floor,
)

logger = logging.getLogger(__name__)

# Final actions this module can return.
ACT_ANALYZE_NOW = "ANALYZE_NOW"
ACT_SCHEDULE_AT = "SCHEDULE_AT"
ACT_WATCH_FOR_CONDITION = "WATCH_FOR_CONDITION"
ACT_CLOSE_WATCH = "CLOSE_WATCH"
ACT_DEFER = "DEFER"

# A SCHEDULE_AT may never land sooner than the cadence floor, nor further out
# than the watch's own expiry. Both are hard.
MAX_SCHEDULE_HORIZON_DAYS = 45


@dataclass
class PolicyInputs:
    now: datetime
    verdict: TriageVerdict
    planner_result: dict | None = None
    budget_left: int = 0
    budget_total: int = 6
    min_reanalysis_h: int = 12
    analyses_this_week: int = 0
    max_analyses_per_week: int = 3
    last_analysis_at: datetime | None = None
    watch_expires_at: datetime | None = None
    market_open: bool = True
    seen_event_keys: frozenset = frozenset()


@dataclass
class PolicyDecision:
    action: str
    reason: str
    at: datetime | None = None
    condition: dict | None = None
    close_reason: str | None = None
    clamps: list = field(default_factory=list)
    source: str = "deterministic"       # deterministic | planner
    score: float = 0.0
    components: dict = field(default_factory=dict)

    def as_doc(self) -> dict:
        return {
            "action": self.action, "reason": self.reason,
            "at": self.at, "condition": self.condition,
            "close_reason": self.close_reason, "clamps": list(self.clamps),
            "source": self.source, "score": self.score,
            "components": dict(self.components),
        }


def _cadence_floor(inp: PolicyInputs) -> datetime:
    """The earliest moment any new analysis of this ticker may happen."""
    if inp.last_analysis_at is None:
        return inp.now
    return max(inp.now, inp.last_analysis_at + timedelta(hours=inp.min_reanalysis_h))


def _horizon_ceiling(inp: PolicyInputs) -> datetime:
    """The latest moment a schedule may land: the watch's expiry, else a hard cap.

    Scheduling past a watch's expiry produces a timer for a record that will not
    exist — the schedule fires, finds nothing, and the reason the research
    existed is gone. That is the [ONE-SHOT = SPENT TIMER] failure with extra
    steps, so the ceiling is enforced rather than trusted.
    """
    hard = inp.now + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS)
    if inp.watch_expires_at is not None:
        return min(hard, inp.watch_expires_at)
    return hard


def _can_spend_a_wake(inp: PolicyInputs) -> tuple[bool, str]:
    if inp.budget_left <= 0:
        return False, "global_daily_budget_exhausted"
    if inp.analyses_this_week >= inp.max_analyses_per_week:
        return False, "ticker_weekly_budget_exhausted"
    if inp.now < _cadence_floor(inp):
        return False, "below_cadence_floor"
    if inp.verdict.event_key in inp.seen_event_keys:
        return False, "duplicate_event"
    floor = score_floor(inp.budget_left, inp.budget_total)
    if inp.verdict.score < floor:
        return False, f"below_score_floor({floor})"
    return True, ""


def apply_policy(inp: PolicyInputs) -> PolicyDecision:
    """Turn a triage verdict (+ optional planner recommendation) into an outcome."""
    v = inp.verdict
    base = {"score": v.score, "components": dict(v.components)}

    # ── No planner: the deterministic path. ──
    if not inp.planner_result:
        if not v.eligible:
            return PolicyDecision(
                ACT_DEFER, v.reject_reason or "ineligible", **base)
        ok, why = _can_spend_a_wake(inp)
        if not ok:
            return PolicyDecision(ACT_DEFER, why, **base)
        return PolicyDecision(ACT_ANALYZE_NOW, "eligible_and_affordable", **base)

    # ── Planner path. The recommendation is advice; the bounds are law. ──
    pr = inp.planner_result
    action = pr.get("action")
    clamps: list = []

    if action == ACT_CLOSE_WATCH:
        # The only action that REDUCES work, so it needs no budget. It is also
        # the only place the model can shrink the queue, which is the behaviour
        # we most want to leave unobstructed.
        return PolicyDecision(
            ACT_CLOSE_WATCH, "planner_close",
            close_reason=pr.get("close_reason") or "planner requested close",
            source="planner", clamps=clamps, **base)

    if action == ACT_WATCH_FOR_CONDITION:
        # Creates no cycle — it rewrites the trigger. Cheap, so it is allowed
        # even when the budget is gone; that is precisely the state in which
        # "keep watching instead of analysing" is the right answer.
        until = pr.get("until")
        ceiling = _horizon_ceiling(inp)
        if until is not None and until > ceiling:
            clamps.append({"field": "until", "from": until, "to": ceiling,
                           "why": "beyond watch expiry / horizon cap"})
            until = ceiling
        return PolicyDecision(
            ACT_WATCH_FOR_CONDITION, "planner_watch_for_condition",
            at=until, condition=pr.get("condition"),
            source="planner", clamps=clamps, **base)

    if action == ACT_SCHEDULE_AT:
        at = pr.get("at")
        floor, ceiling = _cadence_floor(inp), _horizon_ceiling(inp)
        if at is None:
            at = floor
            clamps.append({"field": "at", "from": None, "to": floor,
                           "why": "planner gave no time"})
        if at < floor:
            clamps.append({"field": "at", "from": at, "to": floor,
                           "why": "sooner than the cadence floor"})
            at = floor
        if at > ceiling:
            clamps.append({"field": "at", "from": at, "to": ceiling,
                           "why": "beyond watch expiry / horizon cap"})
            at = ceiling
        if floor > ceiling:
            # The cadence floor is past the watch's own expiry: there is no
            # legal moment to schedule. Say so rather than emitting a schedule
            # that can never legally run.
            return PolicyDecision(
                ACT_DEFER, "no_legal_schedule_window",
                source="planner", clamps=clamps, **base)
        return PolicyDecision(ACT_SCHEDULE_AT, "planner_schedule", at=at,
                              source="planner", clamps=clamps, **base)

    if action == ACT_ANALYZE_NOW:
        ok, why = _can_spend_a_wake(inp)
        if ok:
            return PolicyDecision(ACT_ANALYZE_NOW, "planner_analyze_now",
                                  source="planner", clamps=clamps, **base)
        # DOWNGRADE rather than refuse. The planner judged the subject worth
        # looking at; only its timing was unaffordable. Turning that into a
        # schedule at the earliest legal moment keeps the judgement and drops
        # only the overreach — and it is bounded, so it cannot loop.
        floor, ceiling = _cadence_floor(inp), _horizon_ceiling(inp)
        if floor > ceiling:
            return PolicyDecision(ACT_DEFER, f"{why}; no_legal_schedule_window",
                                  source="planner", clamps=clamps, **base)
        clamps.append({"field": "action", "from": ACT_ANALYZE_NOW,
                       "to": ACT_SCHEDULE_AT, "why": why})
        return PolicyDecision(ACT_SCHEDULE_AT, f"downgraded:{why}", at=floor,
                              source="planner", clamps=clamps, **base)

    # An unknown action is a contract violation, not a decision. Defer — the
    # safe direction is always "do less".
    logger.warning("[WatchPolicy] unknown planner action %r — deferring", action)
    return PolicyDecision(ACT_DEFER, f"unknown_planner_action:{action}",
                          source="planner", **base)
