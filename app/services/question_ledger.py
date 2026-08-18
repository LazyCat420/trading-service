"""The open-question ledger — the fitness function for open-ended research.

## Why this is not just `ticker_dossiers.open_questions`

That column is a JSONB list of strings: the set an agent should read *now*. It
answers "what is open" and cannot answer "did research resolve anything",
because a question that leaves the list leaves no trace of why. Overwriting it
each cycle is exactly how `agent_skills` accumulated 145 versions that joined to
zero outcome rows.

So the governing fact is stamped when it governs. One row per
`(ticker, question)`, `ask_count` bumped on every re-ask.

## The statuses, and why one of them is deliberately not a success

    open      asked; nothing has happened to it yet
    reasked   asked AGAIN in a later cycle -> research did NOT answer it
    dropped   the ticker ran a full desk and did not re-ask it
    answered  a research run produced named evidence against it
    aged_out  open past AGE_OUT_DAYS with no new ask

`dropped` is **ambiguous and must never be counted as resolved.** A question can
leave the artifact because it was answered, or because a different agent ran, or
because the model simply did not repeat itself. Only `answered` — which carries
an `evidence_ref` — is a resolution. Collapsing the two would make the metric
pass whether research worked or not, which is not a metric.

`answered` stays at zero until the deep-dive queue is actually served (Track A2).
That zero is the honest reading of "research is queued but not yet running", not
a failure of the loop.

`reasked` is the informative signal available today: it is unambiguous, it needs
no research pipeline, and it accrues in days rather than against the desk's
8.84pp minimum detectable effect.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Iterable

from datetime import datetime, timedelta, timezone

from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

# A question is a sentence an LLM wrote, so the same question comes back with
# different whitespace, capitalisation and trailing punctuation. Hash the
# normalised form or `ask_count` never leaves 1 and every re-ask looks new.
_WS = re.compile(r"\s+")

# Longer than the 7-day outcome horizon: a question that outlives the trade it
# was asked about is stale whatever happens next.
AGE_OUT_DAYS = 10

# Bound what one desk can add. The quant is the only agent emitting these today
# and it is prompted for "open questions you could not resolve", which is not a
# bounded quantity.
MAX_QUESTIONS_PER_DESK = 5

# Below this a "question" is a fragment, not something research can act on.
_MIN_QUESTION_CHARS = 12


def normalize(question: str) -> str:
    return _WS.sub(" ", (question or "").strip()).rstrip("?.!").lower()


def question_hash(question: str) -> str:
    return hashlib.sha256(normalize(question).encode("utf-8")).hexdigest()[:16]


def clean_questions(raw: Iterable[Any]) -> list[str]:
    """Filter to usable question strings, deduplicated on the normalised form.

    Order is preserved so the cap keeps the agent's own priority rather than
    whatever order a set happens to iterate in.
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, str):
            continue
        text = _WS.sub(" ", item.strip())
        if len(text) < _MIN_QUESTION_CHARS:
            continue
        key = normalize(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def record_asked(
    ticker: str,
    cycle_id: str,
    questions: Iterable[Any],
    source_agent: str = "",
) -> list[dict[str, Any]]:
    """Stamp this cycle's open questions. Returns one dict per recorded question.

    Each dict carries `is_new`: False means the question survived a previous
    cycle, which is the `reasked` signal. Never raises — a ledger outage must
    not fail a desk, and a stale ledger is visible in its own counts.
    """
    ticker = (ticker or "").upper().strip()
    cleaned = clean_questions(questions)[:MAX_QUESTIONS_PER_DESK]
    if not ticker or not cleaned:
        return []

    recorded: list[dict[str, Any]] = []
    try:
        for text in cleaned:
            qhash = question_hash(text)
            now = datetime.now(timezone.utc)
            key = {'ticker': ticker, 'question_hash': qhash}
            # ON CONFLICT (ticker, question_hash) DO UPDATE ... RETURNING
            # ask_count. Done as an atomic $inc-on-existing first: if a doc
            # was there, this is the DO UPDATE branch (bump, restamp,
            # status='reasked'). None back means no row existed, so this is
            # the INSERT branch with the DDL defaults (ask_count 1,
            # status 'open', both _at stamps).
            doc = mongo_store.find_one_and_update(
                'dossier_question_log', key,
                {'$inc': {'ask_count': 1},
                 '$set': {'last_cycle_id': cycle_id, 'last_asked_at': now,
                          'status': 'reasked'}},
                return_after=True,
            )
            if doc is None:
                mongo_store.upsert_doc('dossier_question_log', key, {
                    'ticker': ticker, 'question_hash': qhash,
                    'question': text, 'source_agent': source_agent,
                    'first_cycle_id': cycle_id, 'first_asked_at': now,
                    'last_cycle_id': cycle_id, 'last_asked_at': now,
                    'ask_count': 1, 'status': 'open',
                }, insert_only=True)
                ask_count = 1
            else:
                ask_count = doc.get('ask_count', 1)
            recorded.append({
                "ticker": ticker,
                "question": text,
                "question_hash": qhash,
                "ask_count": ask_count,
                "is_new": ask_count == 1,
            })
    except Exception as e:
        logger.warning("[questions] record_asked(%s) failed: %s", ticker, e)
        return []

    return recorded


def mark_not_reasked(ticker: str, cycle_id: str, asked_hashes: Iterable[str]) -> int:
    """Mark this ticker's still-open questions that THIS desk did not re-ask.

    Only call after a desk that actually ran the research stages — a desk that
    aborted or was triage-skipped never had the chance to re-ask, and scoring
    its silence as `dropped` would credit the loop for work it did not do.

    Returns the number of rows moved to `dropped`.
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return 0
    keep = [h for h in (asked_hashes or []) if h]
    try:
        # `NOT (question_hash = ANY(%s::text[]))` with an empty array is TRUE
        # for every row, so an empty `keep` correctly drops everything still
        # open. $nin with an empty list has the same semantics.
        return mongo_store.update_docs(
            'dossier_question_log',
            {'ticker': ticker,
             'status': {'$in': ['open', 'reasked']},
             'question_hash': {'$nin': keep}},
            {'$set': {'status': 'dropped',
                      'resolved_cycle': cycle_id,
                      'resolved_at': datetime.now(timezone.utc)}},
        )
    except Exception as e:
        logger.warning("[questions] mark_not_reasked(%s) failed: %s", ticker, e)
        return 0


def age_out(days: int = AGE_OUT_DAYS) -> int:
    """Close questions nothing has touched in `days`. Returns rows closed."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        return mongo_store.update_docs(
            'dossier_question_log',
            {'status': {'$in': ['open', 'reasked']},
             'last_asked_at': {'$lt': cutoff}},
            {'$set': {'status': 'aged_out',
                      'resolved_at': datetime.now(timezone.utc)}},
        )
    except Exception as e:
        logger.warning("[questions] age_out failed: %s", e)
        return 0


def stats(days: int = 14) -> dict[str, Any]:
    """Counts by status over the window, plus the re-ask depth distribution.

    `answered` is reported separately from `dropped` on purpose — see the module
    docstring. A caller that sums them is measuring the wrong thing.
    """
    out: dict[str, Any] = {
        "window_days": days,
        "by_status": {},
        "total": 0,
        "answered": 0,
        "reask_rate": None,
        "max_ask_count": 0,
    }
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        rows = mongo_query.group_rows(
            'dossier_question_log',
            {'first_asked_at': {'$gte': cutoff}},
            keys=['status'],
            aggs=[('count', None), ('max', 'ask_count')],
            select=[('key', 'status'), ('agg', 0), ('agg', 1)],
        )
        for status, count, max_asks in rows or []:
            out["by_status"][status] = int(count)
            out["total"] += int(count)
            out["max_ask_count"] = max(out["max_ask_count"], int(max_asks or 0))
        out["answered"] = out["by_status"].get("answered", 0)
        if out["total"]:
            reasked = out["by_status"].get("reasked", 0)
            out["reask_rate"] = round(reasked / out["total"], 4)
    except Exception as e:
        logger.warning("[questions] stats failed: %s", e)
    return out
