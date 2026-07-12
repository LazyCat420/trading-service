"""
Directive Reader — Allows pipeline agents to read and act on active directives.

This closes the feedback loop: AutoResearch generates directives → pipeline reads them.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def get_active_directives(ticker: Optional[str] = None, limit: int = 10) -> list[dict]:
    """
    Fetch active directives for pipeline consumption.
    Optionally filter by ticker for ticker-specific directives.
    """
    try:
        with get_db() as db:
            if ticker:
                rows = db.execute(
                    """SELECT id, cycle_id, directive_type, directive_text,
                              target_ticker, severity, created_at
                    FROM cycle_directives
                    WHERE status = 'active'
                      AND (target_ticker = %s OR target_ticker IS NULL)
                    ORDER BY
                        CASE severity
                            WHEN 'critical' THEN 0
                            WHEN 'warning' THEN 1
                            ELSE 2
                        END,
                        created_at DESC
                    LIMIT %s""",
                    [ticker, limit],
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT id, cycle_id, directive_type, directive_text,
                              target_ticker, severity, created_at
                    FROM cycle_directives
                    WHERE status = 'active'
                    ORDER BY
                        CASE severity
                            WHEN 'critical' THEN 0
                            WHEN 'warning' THEN 1
                            ELSE 2
                        END,
                        created_at DESC
                    LIMIT %s""",
                    [limit],
                ).fetchall()

            return [
                {
                    "id": r[0],
                    "cycle_id": r[1],
                    "directive_type": r[2],
                    "directive_text": r[3],
                    "target_ticker": r[4],
                    "severity": r[5],
                    "created_at": str(r[6]) if r[6] else None,
                }
                for r in rows
            ]
    except Exception as e:
        logger.warning("[DIRECTIVES] Failed to read active directives: %s", e)
        return []


def get_directive_context_for_prompt(ticker: Optional[str] = None) -> str:
    """
    Build a compact text block of active directives suitable for injecting
    into pipeline agent prompts.

    Returns empty string if no active directives.
    """
    directives = get_active_directives(ticker, limit=5)
    if not directives:
        return ""

    lines = ["=== ACTIVE AUTORESEARCH DIRECTIVES ==="]
    for d in directives:
        severity_icon = {"critical": "🔴", "warning": "🟡"}.get(d["severity"], "🟢")
        ticker_tag = f" [{d['target_ticker']}]" if d.get("target_ticker") else ""
        lines.append(f"{severity_icon}{ticker_tag} {d['directive_text']}")
    lines.append("=== END DIRECTIVES ===")

    return "\n".join(lines)


def mark_directive_actioned(directive_id: str, resolution_note: str = "") -> bool:
    """Mark a directive as actioned by the pipeline."""
    try:
        with get_db() as db:
            db.execute(
                """UPDATE cycle_directives
                SET status = 'actioned', resolved_at = %s
                WHERE id = %s AND status = 'active'""",
                [datetime.now(timezone.utc), directive_id],
            )
        return True
    except Exception as e:
        logger.warning("[DIRECTIVES] Failed to mark directive %s as actioned: %s", directive_id, e)
        return False
