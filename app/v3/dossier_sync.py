"""Desk -> dossier -> deep-dive queue. The write side of the research loop.

## What was actually wrong

`ticker_dossiers` and `v3_research_queues` shipped 2026-08-07 (`ac995ee`) with
a full persistent-thesis and worklist schema. Nothing in the cycle read or wrote
either one: the only callers were `app/routers/research_firm_router.py`, and
`cycle_main.py` gained exactly two lines — the router import and
`include_router`. Both services were also unusable as written, calling
`db = get_db(); db.execute(...)` against a `@contextmanager`, so the first
statement of every method raised `AttributeError`. The unit tests passed because
their mock returned the cursor directly and encoded the wrong contract as
correct.

Meanwhile the agents were already producing the input. `quant_analyst` is
prompted for `sub_analyses_requested` ("Open questions you could not resolve"),
`agent_runner.py` maps it to `open_questions` on the whiteboard, and
`shared_desk.py` renders the first five into the compressed narrative. Then the
desk ends and it is gone.

This module closes that: every question an agent could not answer becomes a
durable ledger row and a queued piece of research, so the next cycle inherits
the desk's own unanswered questions instead of re-deriving a worklist from a
fixed screen.

## What it deliberately does NOT do

It does not change a decision, and it runs after the policy gates. It cannot
fail a desk — every entry point is wrapped, because a research bookkeeping
outage that aborts a trading cycle is not a trade-off worth making.

It also does not claim a question was answered. See `question_ledger` for why
`dropped` is not a resolution.
"""

from __future__ import annotations

import logging
from typing import Any

from app.schemas.dossier_schemas import LifecycleState, QueueType
from app.services import question_ledger
from app.services.dossier_service import DossierService
from app.services.research_queue_service import ResearchQueueService

logger = logging.getLogger(__name__)

# Every artifact type is swept rather than just the quant's. Only
# `quant_analyst` emits `sub_analyses_requested` today, but the field is
# declared in ARTIFACT_SCHEMAS and any agent may start filling it; a hardcoded
# list of one would silently ignore the second.
_ARTIFACT_ATTRS = (
    "desk_note",
    "fundamental_report",
    "quant_report",
    "valuation_report",
    "delta_report",
    "bull_argument",
    "bear_rebuttal",
    "bull_defense",
    "debate_judge",
    "regime_classification",
)

# Only a desk that reached the research stages can be said to have "not
# re-asked" a question. A triage skip or an early abort never had the chance.
_RESEARCH_ARTIFACTS = ("desk_note", "fundamental_report", "quant_report",
                       "valuation_report", "delta_report")

# How many prior questions the dossier carries forward for the next cycle to
# read. Bounded at the point of injection, not just at the point of writing:
# `open_questions` reaches a prompt, and an unbounded prompt append is what put
# the junior analyst at 130,982 characters.
_MAX_DOSSIER_QUESTIONS = 12


def collect_open_questions(desk: Any) -> list[tuple[str, str]]:
    """Return `(question, source_artifact)` pairs from every artifact on the desk."""
    found: list[tuple[str, str]] = []
    for attr in _ARTIFACT_ATTRS:
        artifact = getattr(desk, attr, None)
        if not isinstance(artifact, dict):
            continue
        raw = artifact.get("sub_analyses_requested")
        if not isinstance(raw, list):
            continue
        for question in question_ledger.clean_questions(raw):
            found.append((question, attr))
    return found


def _ran_research(desk: Any) -> bool:
    return any(isinstance(getattr(desk, a, None), dict) for a in _RESEARCH_ARTIFACTS)


def sync_desk_to_dossier(
    desk: Any,
    cycle_id: str,
    action: str | None,
    confidence: int,
    policy_action: str = "",
) -> dict[str, Any]:
    """Persist this desk's open questions and queue the new ones for research.

    Returns a summary dict for telemetry. Never raises.
    """
    ticker = getattr(desk, "ticker", "") or ""
    summary: dict[str, Any] = {
        "ticker": ticker,
        "questions_found": 0,
        "questions_new": 0,
        "questions_reasked": 0,
        "questions_dropped": 0,
        "queued": 0,
        "ran_research": False,
    }
    if not ticker:
        return summary

    try:
        pairs = collect_open_questions(desk)
        summary["questions_found"] = len(pairs)
        summary["ran_research"] = _ran_research(desk)

        recorded = question_ledger.record_asked(
            ticker=ticker,
            cycle_id=cycle_id,
            questions=[q for q, _ in pairs],
            source_agent=(pairs[0][1] if pairs else ""),
        )
        summary["questions_new"] = sum(1 for r in recorded if r["is_new"])
        summary["questions_reasked"] = sum(1 for r in recorded if not r["is_new"])

        # A desk that never ran research cannot be evidence that a question
        # went away. Scoring its silence as `dropped` would credit the loop for
        # work it did not do.
        if summary["ran_research"]:
            summary["questions_dropped"] = question_ledger.mark_not_reasked(
                ticker=ticker,
                cycle_id=cycle_id,
                asked_hashes=[r["question_hash"] for r in recorded],
            )

        # Queue only questions this desk raised for the FIRST time. A re-ask is
        # already sitting in the queue (enqueue_item dedupes on pending), and a
        # question the desk keeps asking is evidence research has not run yet,
        # not a reason to queue it twice.
        for rec in recorded:
            if not rec["is_new"]:
                continue
            item_id = ResearchQueueService.enqueue_item(
                ticker=ticker,
                queue_type=QueueType.DEEP_DIVE_QUEUE,
                reason=rec["question"][:400],
                source_agent="v3_pipeline",
                priority=int(confidence or 0),
                payload={
                    "question": rec["question"],
                    "question_hash": rec["question_hash"],
                    "cycle_id": cycle_id,
                },
            )
            if item_id:
                summary["queued"] += 1

        _update_dossier(desk, ticker, cycle_id, action, confidence,
                        policy_action, [q for q, _ in pairs])

    except Exception as e:  # noqa: BLE001 — research bookkeeping never fails a desk
        logger.warning("[dossier-sync] %s: failed (non-fatal): %s", ticker, e)
        return summary

    if summary["questions_found"] or summary["questions_dropped"]:
        logger.info(
            "[dossier-sync] %s: %d question(s) — %d new, %d re-asked, "
            "%d dropped, %d queued",
            ticker, summary["questions_found"], summary["questions_new"],
            summary["questions_reasked"], summary["questions_dropped"],
            summary["queued"],
        )
    return summary


def _update_dossier(
    desk: Any,
    ticker: str,
    cycle_id: str,
    action: str | None,
    confidence: int,
    policy_action: str,
    questions: list[str],
) -> None:
    """Write the decision and the current question set onto the dossier."""
    dossier = DossierService.get_dossier(ticker)

    # Keep this cycle's questions first — they are the live ones — then as much
    # prior history as the cap allows, deduplicated on the normalised form so a
    # re-phrasing does not occupy two slots.
    merged: list[str] = []
    seen: set[str] = set()
    for text in list(questions) + list(dossier.open_questions or []):
        if not isinstance(text, str):
            continue
        key = question_ledger.normalize(text)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(text)
    dossier.open_questions = merged[:_MAX_DOSSIER_QUESTIONS]

    # A blocked or degraded desk did not reach a view. Recording it as a
    # reasoned action would teach the dossier a decision that was never made —
    # the same trap the episodic memory writer avoids with DEGRADED.
    blocked = bool(policy_action) and policy_action.startswith("HOLD_POLICY_BLOCKED")
    if action in ("BUY", "SELL", "HOLD") and not blocked:
        recorded_action = action
    elif blocked:
        recorded_action = "BLOCKED"
    else:
        recorded_action = "DEGRADED"

    entry_state = _next_state(dossier.lifecycle_state, recorded_action)
    dossier.lifecycle_state = entry_state
    DossierService.save_dossier(dossier)

    DossierService.record_decision(
        ticker=ticker,
        cycle_id=cycle_id,
        action=recorded_action,
        confidence=int(confidence or 0),
        lead_analyst=(dossier.lead_analyst_id or "v3_pipeline"),
        rationale=(getattr(desk, "final_decision", None) or {}).get("reasoning", "")[:1000],
        state_transition=entry_state.value,
    )


def _next_state(current: LifecycleState, action: str) -> LifecycleState:
    """Advance the lifecycle on the desk's own verdict.

    Deliberately conservative: only BUY promotes, and nothing here can reach
    POSITION_OPEN or DROPPED. Those belong to the executor and to an explicit
    invalidation trigger respectively, and inferring them from a single desk
    would let a research bookkeeping module retire a ticker.
    """
    if action == "BUY":
        return LifecycleState.BUY_CANDIDATE
    if action in ("BLOCKED", "DEGRADED"):
        return current if current != LifecycleState.NEW else LifecycleState.LEAD
    if action == "SELL":
        return LifecycleState.EXIT_CANDIDATE
    if current in (LifecycleState.NEW, LifecycleState.LEAD):
        return LifecycleState.UNDER_RESEARCH
    return current
