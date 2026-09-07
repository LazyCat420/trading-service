"""watch_schema — the structured contract for a watch record and a planner result.

WHY THIS FILE EXISTS, AND WHY IT IS FIRST
-----------------------------------------
Measured 2026-09-06 over the 185 live watches: every one of them carries a
`thesis_summary` (prose) and a `reason` stub, and **not one of them carries a
statement of what the deciding agent did not know**. The desk therefore had
nothing to test a headline against, and the only screen on news asked "is this
headline about the company" — never "does it bear on the open question". Over
the same window the desk spent its full daily budget of 6 wakes on 21 of 21
days, and 8 of the last 30 trips produced no decision at all.

So the mandatory field on a watch is not the thesis. Prose has been mandatory in
practice for months and has screened nothing, because code cannot evaluate it.
The mandatory field is the **resolution condition** — one object that carries
the invalidation condition and the stated uncertainty together, because a price
level and an open question are the same statement at two resolutions:

    "wake me below $145"                          <- invalidation, checkable
    "my HOLD rests on Q3 NIM guidance, due Oct 14" <- uncertainty, screenable

The first cannot refuse "Citigroup delays Fed rate cut forecast" (a live trip
that flipped a decision HOLD -> BUY on a headline about Citi's *research desk*).
The second refuses it instantly and admits the one headline that matters.

`normalize_watch_record` REFUSES a record with no resolution condition. That is
the enforcement — a prompt asking nicely for one is not enforcement.

THE 185 LEGACY WATCHES ARE NOT REWRITTEN
----------------------------------------
They are marked `schema_version: 0` and scored with an explicit penalty
component, so the cost of the missing field shows up in telemetry as its own
number. A backfill would invent resolution conditions that no agent ever stated
and make the gap unmeasurable forever.

Pure functions only — no I/O, no clock beyond an injectable `now`. The whole
contract is testable without a database.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

# Bumped when the required field set changes. 0 means "created before this
# contract existed" and is never written by this module — only read.
WATCH_SCHEMA_VERSION = 1

# ── Lifecycle ───────────────────────────────────────────────────────────────
WATCH_STATES = ("armed", "tripped", "cooling", "expired", "closed")

# ── Planner actions (Stage 2). Exactly these four, nothing else. ────────────
PLANNER_ACTIONS = ("ANALYZE_NOW", "SCHEDULE_AT", "WATCH_FOR_CONDITION", "CLOSE_WATCH")

# ── Trigger types the resolution condition may name as machine-checkable ────
# Kept in sync with watch_desk.VALID_TRIGGER_TYPES by
# test_watch_schema.py::test_the_checkable_trigger_vocabulary_matches_the_desk,
# which imports both and compares the SETS in both directions — a lone count
# would pass while one name drifted into another.
CHECKABLE_TRIGGER_TYPES = {
    "price_above", "price_below", "pct_change", "rsi", "volume_spike",
    "news", "staleness",
}

# How a trip's evidence was sourced. `provider` is the vendor's own entity
# tagging, which nothing has verified against the body — it is admitted but
# scored lower, so the trust level is visible rather than silently equal.
EVIDENCE_SOURCES = ("detected", "provider", "price", "calendar", "position", "clock")

_MAX_TEXT = 2000
_MAX_SHORT = 500


def _clean_text(v: Any, limit: int = _MAX_SHORT) -> str:
    return (str(v).strip() if v is not None else "")[:limit]


def _as_aware(v: Any) -> datetime | None:
    """Parse a datetime/ISO string to an aware UTC datetime. None on anything else.

    A naive datetime is assumed UTC rather than rejected: Mongo hands back naive
    datetimes for every document written before `ensure_aware` landed, and
    refusing them would make every legacy watch unreadable.
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        s = v.strip().replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


# ─── Resolution condition — the mandatory field ─────────────────────────────
def normalize_resolution_condition(rc: Any) -> tuple[dict | None, str | None]:
    """Validate the one field a watch may not omit. Returns (clean, error).

    Required:
      open_question  — what the deciding agent did not know, in its own words.
      resolving_fact — the fact that would settle it. This is what a headline
                       is screened AGAINST, so it must name something
                       observable, not a feeling.

    Optional but strongly load-bearing:
      resolves_by    — when the fact is expected (drives catalyst urgency).
      invalidates_if — the machine-checkable trigger, when one exists.
      becomes_if_true / becomes_if_false — the decision on each branch.

    A resolution condition with only an `invalidates_if` is REFUSED. That is
    precisely the shape the desk already had (102 of 185 watches carried a
    price_below and nothing else), and it is the shape that could not screen a
    single one of the 27 news trips.
    """
    if isinstance(rc, str):
        try:
            rc = json.loads(rc)
        except (ValueError, TypeError):
            return None, "resolution_condition must be an object, not a bare string."
    if not isinstance(rc, dict):
        return None, "resolution_condition must be an object."

    open_question = _clean_text(rc.get("open_question"), _MAX_TEXT)
    resolving_fact = _clean_text(rc.get("resolving_fact"), _MAX_TEXT)
    if not open_question:
        return None, "resolution_condition.open_question is required — state what you do not know."
    if not resolving_fact:
        return None, "resolution_condition.resolving_fact is required — name the fact that would settle it."

    invalidates_if = rc.get("invalidates_if")
    if invalidates_if is not None:
        if not isinstance(invalidates_if, dict):
            return None, "resolution_condition.invalidates_if must be a trigger object or absent."
        typ = _clean_text(invalidates_if.get("type"), 40).lower()
        if typ not in CHECKABLE_TRIGGER_TYPES:
            return None, (f"resolution_condition.invalidates_if.type {typ!r} is not checkable; "
                          f"valid: {sorted(CHECKABLE_TRIGGER_TYPES)}")
        invalidates_if = {**invalidates_if, "type": typ}

    return {
        "open_question": open_question,
        "resolving_fact": resolving_fact,
        # Keywords the triage layer screens evidence against. Derived here so
        # every consumer screens against the SAME token set — a second
        # derivation elsewhere would drift and nothing would notice.
        "resolving_terms": resolution_terms(open_question, resolving_fact),
        # Stored, not re-derived per screen: two derivations of the same tier
        # would drift and nothing would notice, because both would still "work".
        "resolving_terms_specific": specific_terms(
            resolution_terms(open_question, resolving_fact)),
        "resolves_by": _as_aware(rc.get("resolves_by")),
        "invalidates_if": invalidates_if,
        "becomes_if_true": _clean_text(rc.get("becomes_if_true")),
        "becomes_if_false": _clean_text(rc.get("becomes_if_false")),
    }, None


# Words that appear in every thesis and therefore discriminate nothing. A term
# list that keeps them matches every headline and the screen fails OPEN — which
# is the failure mode that let "Citigroup delays Fed rate cut forecast" through.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "for", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "will", "would", "could", "should", "may", "might", "can", "that", "this",
    "these", "those", "it", "its", "as", "than", "then", "so", "not", "no",
    "we", "i", "my", "our", "their", "there", "here", "up", "down", "out",
    "stock", "stocks", "share", "shares", "price", "prices", "company",
    "quarter", "quarterly", "year", "years", "next", "last", "new", "more",
    "less", "very", "much", "still", "about", "over", "under", "into",
}
_TERM_RE = re.compile(r"[a-z][a-z0-9\-\.]{2,}")

# Vocabulary that appears in nearly every equity thesis AND in nearly every
# equity headline, and therefore carries almost no information about whether
# THIS headline bears on THIS question.
#
# MEASURED: without this tier, the live trip
#   "Adobe Stock: Citi Sees 'Achievable Set-Up' Ahead Of Q3 Earnings…"
# matched a Citigroup net-interest-margin condition on the tokens "q3" and
# "earnings" alone and passed the screen — the same false pass the old
# `_title_names_ticker` guard gave it. Two generic words is not evidence that
# a headline resolves a question; it is evidence that both are about stocks.
GENERIC_FINANCE_TERMS = {
    "earnings", "earning", "quarter", "quarterly", "q1", "q2", "q3", "q4",
    "revenue", "revenues", "eps", "results", "result", "report", "reports",
    "reported", "call", "target", "targets", "estimate", "estimates",
    "analyst", "analysts", "rating", "ratings", "outlook", "sales",
    "profit", "profits", "growth", "beat", "beats", "miss", "misses",
    "stocks", "shares", "market", "markets", "investor", "investors",
    "fiscal", "annual", "guidance",
}


def resolution_terms(*texts: str) -> list[str]:
    """The discriminating tokens of a resolution condition, for evidence screening.

    Deliberately not a bag of every word: a screen built from stopwords matches
    every headline and fails open, which is indistinguishable from having no
    screen at all. Returns a stable, de-duplicated, sorted list so two callers
    that build it from the same text cannot disagree.
    """
    terms: set[str] = set()
    for t in texts:
        for m in _TERM_RE.findall((t or "").lower()):
            w = m.strip(".-")
            if len(w) >= 3 and w not in _STOPWORDS:
                terms.add(w)
    return sorted(terms)


def specific_terms(terms) -> list[str]:
    """The subset of `terms` that actually discriminates between headlines.

    A condition made ENTIRELY of generic vocabulary ("will they beat earnings
    estimates?") yields an empty specific set. Callers must fall back to the
    full term list there rather than failing closed — an empty specific set
    means "this question is stated in generic words", not "nothing can match
    it", and failing closed would make such a watch permanently unwakeable.
    """
    return [t for t in (terms or []) if t not in GENERIC_FINANCE_TERMS]


# ─── Decision context ───────────────────────────────────────────────────────
_VALID_ACTIONS = {"BUY", "SELL", "HOLD"}


def normalize_decision_context(dc: Any) -> tuple[dict | None, str | None]:
    """The prior agent decision this watch descends from.

    `action` is checked against BUY/SELL/HOLD on purpose. Measured 2026-09-06:
    9 of the last 30 trips had a prior action of `DEGRADED` — an error state
    that `analysis_results` records as if it were a decision, so 7 of the 16
    "the decision changed" trips compared a decision against a failure. A watch
    must not descend from a non-decision.
    """
    if not isinstance(dc, dict):
        return None, "decision_context must be an object."
    action = _clean_text(dc.get("action"), 20).upper()
    if action not in _VALID_ACTIONS:
        return None, (f"decision_context.action {action!r} is not a decision "
                      f"(valid: {sorted(_VALID_ACTIONS)}). A DEGRADED/error result "
                      f"may not arm a watch.")
    rc, err = normalize_resolution_condition(dc.get("resolution_condition"))
    if err:
        return None, err

    conf = dc.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    if conf is not None:
        # Confidence arrives as 0-1 from some agents and 0-100 from others.
        # Normalising here means the uncertainty score has ONE scale; leaving
        # it would make a 0.8 look eighty times less certain than an 80.
        conf = conf / 100.0 if conf > 1.0 else conf
        conf = max(0.0, min(1.0, conf))

    return {
        "action": action,
        "confidence": conf,
        "thesis_summary": _clean_text(dc.get("thesis_summary"), _MAX_TEXT),
        "resolution_condition": rc,
        "source_cycle_id": _clean_text(dc.get("source_cycle_id"), 120) or None,
        "decided_at": _as_aware(dc.get("decided_at")),
    }, None


# ─── Catalyst calendar ──────────────────────────────────────────────────────
def normalize_catalyst_calendar(cc: Any) -> dict:
    """Never refuses — an absent calendar is a real, common state (a ticker with
    no upcoming earnings), and refusing it would block arming the watch. A miss
    is recorded as an empty calendar with its sources, so "we looked and found
    nothing" is distinguishable from "we never looked"."""
    cc = cc if isinstance(cc, dict) else {}
    events = cc.get("events")
    events = events if isinstance(events, list) else []
    clean_events = []
    for e in events:
        if not isinstance(e, dict):
            continue
        at = _as_aware(e.get("at"))
        if at is None:
            continue
        clean_events.append({
            "kind": _clean_text(e.get("kind"), 40).lower() or "unknown",
            "at": at,
            "confidence": _clean_text(e.get("confidence"), 20).lower() or "unknown",
            "label": _clean_text(e.get("label"), 200),
            "source": _clean_text(e.get("source"), 60),
        })
    clean_events.sort(key=lambda e: e["at"])
    return {
        "events": clean_events,
        "earnings_at": _as_aware(cc.get("earnings_at")),
        "earnings_confidence": _clean_text(cc.get("earnings_confidence"), 20).lower() or "unknown",
        # `sources` present + `events` empty means "looked, found nothing".
        # `sources` empty means "never looked". Do not collapse these.
        "sources": [_clean_text(s, 60) for s in (cc.get("sources") or []) if s],
        "checked_at": _as_aware(cc.get("checked_at")),
    }


# ─── Revisit plan / budget / priority ───────────────────────────────────────
DEFAULT_MAX_ANALYSES_PER_WEEK = 3
DEFAULT_MIN_REANALYSIS_HOURS = 12


def normalize_revisit_plan(rp: Any, now: datetime, expiry_at: datetime | None = None) -> dict:
    rp = rp if isinstance(rp, dict) else {}
    return {
        "next_review_at": _as_aware(rp.get("next_review_at")),
        "cooldown_until": _as_aware(rp.get("cooldown_until")),
        "expires_at": _as_aware(rp.get("expires_at")) or expiry_at,
        "min_hours_between_analyses": max(
            1, int(rp.get("min_hours_between_analyses") or DEFAULT_MIN_REANALYSIS_HOURS)),
        "rationale": _clean_text(rp.get("rationale"), _MAX_SHORT),
        "expected_information": _clean_text(rp.get("expected_information"), _MAX_SHORT),
    }


def normalize_budget(b: Any) -> dict:
    b = b if isinstance(b, dict) else {}

    def _int(k, d):
        try:
            return max(0, int(b.get(k) if b.get(k) is not None else d))
        except (TypeError, ValueError):
            return d

    return {
        "analysis_count": _int("analysis_count", 0),
        "max_analyses_per_week": max(1, _int("max_analyses_per_week", DEFAULT_MAX_ANALYSES_PER_WEEK)),
        "cost_units_spent": _int("cost_units_spent", 0),
        "window_started_at": _as_aware(b.get("window_started_at")),
    }


# ─── The whole record ───────────────────────────────────────────────────────
def normalize_watch_record(
    *,
    ticker: str,
    watch_id: str,
    decision_context: Any,
    triggers: Any = None,
    catalyst_calendar: Any = None,
    revisit_plan: Any = None,
    budget: Any = None,
    state: str = "armed",
    expiry_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[dict | None, str | None]:
    """Build the persisted watch document. Returns (doc, error).

    The single hard refusal is a missing/!invalid `resolution_condition` inside
    `decision_context`. Everything else degrades to a documented default,
    because a watch that cannot be armed monitors nothing — refusing on a soft
    field would trade a weak watch for no watch.
    """
    now = now or datetime.now(timezone.utc)
    ticker = _clean_text(ticker, 20).upper()
    if not ticker:
        return None, "ticker required."
    if state not in WATCH_STATES:
        return None, f"state {state!r} invalid; valid: {list(WATCH_STATES)}"

    dc, err = normalize_decision_context(decision_context)
    if err:
        return None, err

    return {
        "schema_version": WATCH_SCHEMA_VERSION,
        "id": watch_id,
        "ticker": ticker,
        "state": state,
        "decision_context": dc,
        "catalyst_calendar": normalize_catalyst_calendar(catalyst_calendar),
        "revisit_plan": normalize_revisit_plan(revisit_plan, now, expiry_at),
        "budget": normalize_budget(budget),
        # Filled by the triage layer on each evaluation; present from creation so
        # a consumer never has to distinguish "absent" from "not yet scored".
        "priority": {"score": None, "components": {}, "scored_at": None,
                     "selection_reason": None},
        "triggers": triggers if isinstance(triggers, list) else [],
    }, None


def is_legacy(watch: dict) -> bool:
    """A watch created before this contract. Scored with an explicit penalty
    rather than backfilled — see the module docstring."""
    try:
        return int(watch.get("schema_version") or 0) < WATCH_SCHEMA_VERSION
    except (TypeError, ValueError):
        return True


def get_resolution_condition(watch: dict) -> dict | None:
    """The resolution condition of a watch, whatever shape it was stored in.

    Reads the nested schema-1 location and falls back to a top-level key, so a
    caller never has to know which vintage it holds. Returns None for a legacy
    watch — and None MUST be handled as "cannot screen", never as "screens
    clean": treating an absent condition as a pass is exactly how a fail-open
    screen certifies everything it cannot read.
    """
    if not isinstance(watch, dict):
        return None
    dc = watch.get("decision_context")
    if isinstance(dc, dict) and isinstance(dc.get("resolution_condition"), dict):
        return dc["resolution_condition"]
    rc = watch.get("resolution_condition")
    return rc if isinstance(rc, dict) else None


# ─── Planner result (Stage 2) ───────────────────────────────────────────────
def normalize_planner_result(
    result: Any,
    *,
    now: datetime | None = None,
) -> tuple[dict | None, str | None]:
    """Validate one bounded-agent recommendation. Returns (clean, error).

    This normalises SHAPE only. It deliberately does not enforce time bounds,
    cooldowns, trading windows or budgets — `watch_policy.apply_policy` does
    that, and it does it to the planner's output AND to the deterministic path
    alike. Enforcing bounds here too would put the same rule on two seams, and
    the two would drift.
    """
    now = now or datetime.now(timezone.utc)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return None, "planner result must be a JSON object."
    if not isinstance(result, dict):
        return None, "planner result must be an object."

    action = _clean_text(result.get("action"), 40).upper()
    if action not in PLANNER_ACTIONS:
        return None, f"action {action!r} invalid; valid: {list(PLANNER_ACTIONS)}"

    out: dict = {
        "action": action,
        "rationale": _clean_text(result.get("rationale"), _MAX_SHORT),
        "expected_information": _clean_text(result.get("expected_information"), _MAX_SHORT),
        "confidence": None,
    }
    conf = result.get("confidence")
    try:
        if conf is not None:
            c = float(conf)
            out["confidence"] = max(0.0, min(1.0, c / 100.0 if c > 1.0 else c))
    except (TypeError, ValueError):
        pass

    if action == "SCHEDULE_AT":
        at = _as_aware(result.get("at"))
        if at is None:
            return None, "SCHEDULE_AT requires `at` (an ISO timestamp)."
        out["at"] = at
    elif action == "WATCH_FOR_CONDITION":
        cond = result.get("condition")
        if not isinstance(cond, dict):
            return None, "WATCH_FOR_CONDITION requires a `condition` trigger object."
        typ = _clean_text(cond.get("type"), 40).lower()
        if typ not in CHECKABLE_TRIGGER_TYPES:
            return None, (f"WATCH_FOR_CONDITION.condition.type {typ!r} is not checkable; "
                          f"valid: {sorted(CHECKABLE_TRIGGER_TYPES)}")
        out["condition"] = {**cond, "type": typ}
        out["until"] = _as_aware(result.get("until"))
    elif action == "CLOSE_WATCH":
        reason = _clean_text(result.get("close_reason"), _MAX_SHORT)
        if not reason:
            return None, "CLOSE_WATCH requires `close_reason` — a watch is not closed silently."
        out["close_reason"] = reason

    return out, None
