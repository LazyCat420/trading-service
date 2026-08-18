"""
Pending Review Service — Consolidates news outlier approvals and (retired) evolution fixes.

RETIRED SUBSYSTEM: ``pending_evolution_fixes``
----------------------------------------------
The evolution-fix half of this module reads a table that is no longer live work.
It was superseded by CORAL, which stores its state in ``evolution_repair_queue``
and ``evolution_attempts`` (see ``app/cognition/evolution/coral/attempts.py``).

Evidence, measured 2026-07-28:

* 96 rows total — rejected 54, deployed 34, FAILED_REQUIRES_HUMAN 4, pending 3,
  error 1.
* The most recent row with status ``deployed`` is dated 2026-06-01. Nothing has
  been deployed out of this table since.
* The 3 rows still marked ``pending`` (2 from May 2026, 1 from 2026-07-27) all
  carry ``judge_score`` 1.0 and ``attempt_count`` 0 — scored, never acted on,
  and no live code path will ever act on them. The old loop stored proposals
  with no measured score that was comparable across rows, which is precisely the
  defect CORAL's ScoreBundle exists to fix.

Because the UI rendered these rows as "pending evolution fixes", their presence
read as *the evolution loop is broken* rather than *the evolution loop moved*.
That misdiagnosis is the reason for this note.

The table and its 96 rows are deliberately KEPT as historical evidence. The read
path below still returns them, but every row is labelled archived so nothing
presents them as actionable. The mutators are inert by design — see
``approve_fix``/``reject_fix``. To un-retire, delete the labelling in
``get_pending_fixes`` and drop the guards in the mutators; nothing else changed.
"""

import uuid
import logging
from datetime import datetime, timezone
from fastapi import HTTPException
from app.db.connection import get_db
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


# ── NEWS OUTLIERS ──

def get_pending_outliers() -> list[dict]:
    """Fetch all news articles flagged as outliers pending human review."""
    with get_db() as db:
        rows = db.execute("""
            SELECT n.id, n.ticker, n.title, n.summary, n.publisher, n.quality_reason, c.consensus, n.published_at, n.url
            FROM news_articles n
            LEFT JOIN ticker_consensus c ON n.ticker = c.ticker
            WHERE n.quality_status = 'pending_review'
            ORDER BY n.published_at DESC
            LIMIT 50
        """).fetchall()
        
    return [
        {
            "id": r[0],
            "ticker": r[1],
            "title": r[2],
            "summary": r[3],
            "publisher": r[4],
            "reason": r[5],
            "consensus": r[6] or "No consensus generated yet.",
            "published_at": r[7].isoformat() if r[7] else None,
            "url": r[8]
        }
        for r in rows
    ]


def approve_outlier(article_id: str) -> dict:
    """Approve an outlier as valid breaking news."""
    with get_db() as db:
        mongo_store.update_docs('news_articles', {'id': article_id}, {'$set': {'quality_status': 'ok'}})
        
        row = mongo_query.find_row('news_articles', {'id': article_id}, ['publisher'])
        if row and row[0]:
            db.execute("""
                UPDATE source_trust 
                SET quality_wins = quality_wins + 2
                WHERE source_type = 'publisher' AND source_name = %s
            """, [row[0]])
            
    return {"status": "approved", "article_id": article_id}


def reject_outlier(article_id: str) -> dict:
    """Reject an outlier as fake news or spam."""
    with get_db() as db:
        row = mongo_query.find_row('news_articles', {'id': article_id}, ['publisher'])
        mongo_store.update_docs('news_articles', {'id': article_id}, {'$set': {'quality_status': 'rejected'}})
        
        if row and row[0]:
            db.execute("""
                UPDATE source_trust 
                SET total_items = total_items + 5,
                    win_rate = quality_wins::FLOAT / NULLIF((total_items + 5), 0)
                WHERE source_type = 'publisher' AND source_name = %s
            """, [row[0]])
            
    return {"status": "rejected", "article_id": article_id}


def add_outlier_rule(article_id: str, rule_content: str) -> dict:
    """Add a permanent rule based on this outlier, then reject it."""
    with get_db() as db:
        row = mongo_query.find_row('news_articles', {'id': article_id}, ['ticker', 'publisher'])
        if not row:
            raise HTTPException(404, "Article not found")
            
        ticker, publisher = row
        fb_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        
        mongo_store.insert_docs('user_feedback', [{'id': fb_id, 'ticker': ticker, 'feedback_type': 'constraint', 'content': rule_content, 'created_at': now, 'is_active': True}])
        
        mongo_store.update_docs('news_articles', {'id': article_id}, {'$set': {'quality_status': 'rejected'}})
        
        if publisher:
            db.execute("""
                UPDATE source_trust 
                SET total_items = total_items + 5,
                    win_rate = quality_wins::FLOAT / NULLIF((total_items + 5), 0)
                WHERE source_type = 'publisher' AND source_name = %s
            """, [publisher])

    return {"status": "rule_added", "article_id": article_id, "rule_id": fb_id}


# ── EVOLUTION FIXES (RETIRED — superseded by CORAL) ──

#: Marker attached to every row this module returns. Readers (and the UI) should
#: treat a truthy ``archived`` as "historical record, not a queue item".
RETIRED_TABLE = "pending_evolution_fixes"
RETIRED_SUPERSEDED_BY = "CORAL (evolution_repair_queue + evolution_attempts)"
RETIRED_NOTE = (
    "RETIRED 2026-07-28. The pending_evolution_fixes loop was superseded by "
    "CORAL, which records graded attempts in evolution_repair_queue and "
    "evolution_attempts. Last deployment out of this table: 2026-06-01. These "
    "96 rows are kept as historical evidence and are not actionable work."
)


def get_pending_fixes(status: str = "all", limit: int = 50) -> list[dict]:
    """List evolution-fix rows from the RETIRED ``pending_evolution_fixes`` table.

    Rows are returned for historical inspection only. Every row is stamped with
    ``archived=True``, ``actionable=False`` and a ``display_status`` of
    ``"archived"`` so no caller can mistake a row whose stored ``status`` still
    reads ``pending`` for live queued work — the raw ``status`` is preserved
    untouched alongside it, because it is the historical record.
    """
    with get_db() as db:
        if status == "all":
            rows = mongo_query.find_rows('pending_evolution_fixes', {}, ['id', 'cycle_id', 'target_type', 'target_name', 'proposed_fix', 'motivation', 'proposer_model', 'critic_concerns', 'judge_score', 'status', 'created_at', 'resolved_at'], sort=[('created_at', -1)], limit=limit)
        else:
            rows = mongo_query.find_rows('pending_evolution_fixes', {'status': status}, ['id', 'cycle_id', 'target_type', 'target_name', 'proposed_fix', 'motivation', 'proposer_model', 'critic_concerns', 'judge_score', 'status', 'created_at', 'resolved_at'], sort=[('created_at', -1)], limit=limit)

    cols = [
        "id", "cycle_id", "target_type", "target_name", "proposed_fix",
        "motivation", "proposer_model", "critic_concerns", "judge_score", "status",
        "created_at", "resolved_at",
    ]
    fixes = []
    for row in rows:
        d = dict(zip(cols, row))
        # Label, don't rewrite: `status` stays as stored so the archive keeps
        # its own history, while `display_status` is what any surface renders.
        d["archived"] = True
        d["actionable"] = False
        d["display_status"] = "archived"
        d["retired_note"] = RETIRED_NOTE
        d["superseded_by"] = RETIRED_SUPERSEDED_BY
        fixes.append(d)
    return fixes


def approve_fix(fix_id: str) -> dict:
    """No-op. Approving an archived row would queue work nothing consumes.

    The deploy path that consumed ``approved`` rows has not run since
    2026-06-01; marking a row approved now would leave it stuck in that state
    forever while the UI showed it as accepted. Returns an explanatory payload
    instead of raising — this sits behind a request handler and a retired
    subsystem must never become an error path.
    """
    return {
        "status": "archived",
        "id": fix_id,
        "archived": True,
        "actionable": False,
        "error": "pending_evolution_fixes is retired; approval is a no-op.",
        "retired_note": RETIRED_NOTE,
        "superseded_by": RETIRED_SUPERSEDED_BY,
    }


def reject_fix(fix_id: str) -> dict:
    """No-op. Rejecting an archived row would edit the historical record.

    See :func:`approve_fix`. The 96 rows are evidence of what the old loop did;
    rewriting their status now would erase that.
    """
    return {
        "status": "archived",
        "id": fix_id,
        "archived": True,
        "actionable": False,
        "error": "pending_evolution_fixes is retired; rejection is a no-op.",
        "retired_note": RETIRED_NOTE,
        "superseded_by": RETIRED_SUPERSEDED_BY,
    }
