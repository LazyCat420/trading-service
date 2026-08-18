"""Open-Ended Research Loop Worker — background consumer for `dossier_question_log`.

WHAT THIS DOES NOT DO, AND WHY THAT MATTERS
-------------------------------------------
The first version of this module fabricated its answers. Its whole "research"
step was::

    evidence = f"Verified research evidence for {ticker}: '{question[:50]}' ..."
    mark_question_answered(...)

— an f-string containing the question itself, no lookup of any kind, and the
`evidence` argument was never written to any column. It then set
``status = 'answered'``.

`answered` is the ledger's SUCCESS metric. `question_ledger.stats()` reports it
separately from `dropped` precisely so that it means "this question got an
answer", and `record_asked` resets an answered question to `reasked` when it
comes back — so a falsely-answered question would look like a research win and
then like a regression. Wiring that worker into a cycle would not have been a
partial implementation; it would have destroyed the only measurement of whether
the research loop works, permanently and silently, at ~5 questions a pass.

So this module is FAIL-CLOSED. `run_research_loop_pass` cannot answer anything
unless it is handed a `resolver` that returns real evidence, and
`mark_question_answered` refuses to write a status without evidence to store.
With no resolver the pass is a read-only report of what is waiting.

Wiring a real resolver is deliberately left to the caller: every external
lookup in this stack goes through lazy-tool-service (:5591), and a resolver
that reaches it belongs with the agent plumbing, not in the ledger writer.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.db.connection import get_db
from app.db import mongo_query

logger = logging.getLogger(__name__)

# A resolver takes the question row and returns evidence text, or None when it
# could not answer. None is the expected case, not an error.
Resolver = Callable[[dict[str, Any]], "str | None"]

# Below this, a returned "answer" is not evidence — it is an acknowledgement.
MIN_EVIDENCE_CHARS = 40


def fetch_pending_questions(limit: int = 10) -> list[dict[str, Any]]:
    """Un-answered questions, most-re-asked first. Never raises."""
    try:
        with get_db() as db:
            rows = mongo_query.find_rows('dossier_question_log', {'status': {'$in': ['open', 'reasked']}}, ['id', 'ticker', 'question_hash', 'question', 'source_agent', 'ask_count', 'status'], sort=[('ask_count', -1), ('last_asked_at', -1)], limit=limit)
            return [
                {
                    "id": r[0],
                    "ticker": r[1],
                    "question_hash": r[2],
                    "question": r[3],
                    "source_agent": r[4],
                    "ask_count": r[5],
                    "status": r[6],
                }
                for r in rows or []
            ]
    except Exception as e:  # noqa: BLE001 — a ledger outage must not fail a desk
        logger.warning("[research_loop] fetch_pending_questions failed: %s", e)
        return []


def mark_question_answered(
    ticker: str,
    qhash: str,
    evidence: str,
    cycle_id: str = "autoresearch-loop",
) -> bool:
    """Mark a question `answered` AND store the evidence that answered it.

    Refuses (returns False, writes nothing) without evidence of at least
    MIN_EVIDENCE_CHARS. The status and the evidence are written in the same
    statement so the table cannot hold an `answered` row whose `evidence_ref`
    is NULL — that combination is indistinguishable from the fabrication this
    module was rewritten to remove.
    """
    ticker = (ticker or "").upper().strip()
    evidence = (evidence or "").strip()
    if not ticker or not qhash:
        return False
    if len(evidence) < MIN_EVIDENCE_CHARS:
        logger.warning(
            "[research_loop] refusing to answer %s/%s — evidence is %d chars, "
            "below the %d-char floor. An answered question with no evidence is "
            "worse than an open one.",
            ticker, qhash[:12], len(evidence), MIN_EVIDENCE_CHARS,
        )
        return False
    try:
        with get_db() as db:
            rows = db.execute(
                """
                UPDATE dossier_question_log
                   SET status         = 'answered',
                       evidence_ref   = %s,
                       resolved_cycle = %s,
                       resolved_at    = CURRENT_TIMESTAMP
                 WHERE ticker = %s
                   AND question_hash = %s
                   AND status IN ('open', 'reasked')
              RETURNING id
                """,
                [evidence, cycle_id, ticker, qhash],
            ).fetchall()
            # RETURNING, not rowcount: PooledCursor exposes no rowcount, and
            # reading it raises into the except below — which would report
            # every successful write as a failure.
            return bool(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[research_loop] mark_question_answered failed for %s: %s", ticker, e
        )
        return False


def run_research_loop_pass(
    limit: int = 5,
    cycle_id: str = "autoresearch-loop",
    resolver: Resolver | None = None,
) -> dict[str, Any]:
    """One pass over the open questions.

    With no `resolver` this is READ-ONLY: it reports what is waiting and
    answers nothing. That is the default on purpose — see the module docstring.
    """
    pending = fetch_pending_questions(limit=limit)
    out: dict[str, Any] = {
        "pending_found": len(pending),
        "processed": 0,
        "answered": 0,
        "unresolved": 0,
        "cycle_id": cycle_id,
        "resolver": getattr(resolver, "__name__", None),
    }

    if resolver is None:
        out["note"] = (
            "no resolver supplied — reporting only. This worker will not mark "
            "a question answered without evidence from a real lookup."
        )
        return out

    for item in pending:
        out["processed"] += 1
        try:
            evidence = resolver(item)
        except Exception as e:  # noqa: BLE001 — one bad question is not a pass
            logger.warning(
                "[research_loop] resolver raised on %s/%s: %s",
                item.get("ticker"), str(item.get("question"))[:40], e,
            )
            evidence = None

        if not evidence:
            out["unresolved"] += 1
            continue

        if mark_question_answered(
            ticker=item["ticker"],
            qhash=item["question_hash"],
            evidence=evidence,
            cycle_id=cycle_id,
        ):
            out["answered"] += 1
        else:
            out["unresolved"] += 1

    if out["answered"]:
        logger.info(
            "[research_loop] %d/%d questions answered with evidence (%d left open)",
            out["answered"], out["processed"], out["unresolved"],
        )
    return out
