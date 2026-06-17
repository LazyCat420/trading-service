"""
TaskBoard — Async-safe inter-agent communication hub.

Inspired by Claude Code's coordinator/SendMessageTool pattern.
Allows agents to post findings, request investigations, and read
team results during multi-agent debate sessions.

Each ticker+cycle_id gets its own board instance for isolation.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Finding:
    """A fact or insight posted by an agent for other agents to see."""

    id: str
    source_agent: str
    content: str
    category: str  # "fact", "risk", "opportunity", "question"
    ticker: str
    confidence: int  # 0-100
    timestamp: float = field(default_factory=time.monotonic)
    responses: list[dict] = field(default_factory=list)


@dataclass
class InvestigationRequest:
    """A request from one agent for another to investigate something."""

    id: str
    requester: str
    target_agent: str  # "*" for any agent
    question: str
    ticker: str
    status: str = "open"  # open, claimed, completed
    claimed_by: str | None = None
    result: str | None = None
    timestamp: float = field(default_factory=time.monotonic)
import json
from app.db.connection import get_db, safe_jsonb

class TaskBoard:
    """Central hub for inter-agent communication within a debate session.

    Thread-safe via PostgreSQL transactions and an internal asyncio.Lock.
    Each board is scoped to a single ticker+cycle_id combination in the database.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._broadcast_callback = None

    def set_broadcast_callback(self, callback):
        """Set a callback for broadcasting TaskBoard events to the frontend."""
        self._broadcast_callback = callback

    async def post_finding(
        self,
        source_agent: str,
        content: str,
        ticker: str,
        cycle_id: str = "",
        category: str = "fact",
        confidence: int = 75,
    ) -> str:
        """Post a finding for other agents to see.

        Saves to the taskboard_findings table in PostgreSQL.
        """
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        async with self._lock:
            with get_db() as db:
                with db.transaction():
                    # Calculate next sequential finding_id for this cycle + ticker
                    row = db.execute(
                        "SELECT COALESCE(MAX(CAST(NULLIF(REGEXP_REPLACE(finding_id, '\\D', '', 'g'), '') AS INTEGER)), 0) "
                        "FROM taskboard_findings WHERE cycle_id = %s AND ticker = %s",
                        [cycle_id, ticker]
                    ).fetchone()
                    next_seq = (row[0] if row else 0) + 1
                    finding_id = f"f-{next_seq:04d}"

                    db.execute(
                        "INSERT INTO taskboard_findings "
                        "(finding_id, cycle_id, ticker, source_agent, content, category, confidence, responses) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [finding_id, cycle_id, ticker, source_agent, content, category, confidence, json.dumps([])]
                    )

            logger.info(
                "[TaskBoard] %s posted finding %s (%s) for %s: %s",
                source_agent,
                finding_id,
                category,
                ticker,
                content[:80],
            )

            # Broadcast to frontend if callback is set
            if self._broadcast_callback:
                try:
                    await self._broadcast_callback(
                        {
                            "type": "taskboard_finding",
                            "finding_id": finding_id,
                            "source_agent": source_agent,
                            "category": category,
                            "content": content[:200],
                            "ticker": ticker,
                        }
                    )
                except Exception as e:
                    logger.debug("[TaskBoard] Broadcast failed: %s", e)

            return finding_id

    async def get_findings(
        self,
        ticker: str,
        cycle_id: str = "",
        category: str | None = None,
        exclude_agent: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Get findings for a ticker, optionally filtered by category."""
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        with get_db() as db:
            query = (
                "SELECT finding_id, source_agent, content, category, confidence, responses "
                "FROM taskboard_findings WHERE cycle_id = %s AND ticker = %s"
            )
            params = [cycle_id, ticker]

            if category:
                query += " AND category = %s"
                params.append(category)
            if exclude_agent:
                query += " AND source_agent != %s"
                params.append(exclude_agent)

            query += " ORDER BY id ASC LIMIT %s"
            params.append(limit)

            rows = db.execute(query, params).fetchall()

            results = []
            for r in rows:
                finding_id, source_agent, content, category_val, confidence, responses_raw = r
                results.append(
                    {
                        "id": finding_id,
                        "source_agent": source_agent,
                        "content": content,
                        "category": category_val,
                        "confidence": confidence,
                        "responses": safe_jsonb(responses_raw) or [],
                    }
                )

            return results

    async def request_investigation(
        self,
        requester: str,
        question: str,
        ticker: str,
        cycle_id: str = "",
        target_agent: str = "*",
    ) -> str:
        """Request another agent to investigate a question.

        Saves to the taskboard_investigations table in PostgreSQL.
        """
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        async with self._lock:
            with get_db() as db:
                with db.transaction():
                    # Calculate next sequential investigation_id for this cycle + ticker
                    row = db.execute(
                        "SELECT COALESCE(MAX(CAST(NULLIF(REGEXP_REPLACE(investigation_id, '\\D', '', 'g'), '') AS INTEGER)), 0) "
                        "FROM taskboard_investigations WHERE cycle_id = %s AND ticker = %s",
                        [cycle_id, ticker]
                    ).fetchone()
                    next_seq = (row[0] if row else 0) + 1
                    investigation_id = f"inv-{next_seq:04d}"

                    db.execute(
                        "INSERT INTO taskboard_investigations "
                        "(investigation_id, cycle_id, ticker, requester, target_agent, question, status) "
                        "VALUES (%s, %s, %s, %s, %s, %s, 'open')",
                        [investigation_id, cycle_id, ticker, requester, target_agent, question]
                    )

            logger.info(
                "[TaskBoard] %s requested investigation %s → %s for %s: %s",
                requester,
                investigation_id,
                target_agent,
                ticker,
                question[:80],
            )

            return investigation_id

    async def claim_investigation(
        self,
        req_id: str,
        claiming_agent: str,
        ticker: str,
        cycle_id: str = "",
    ) -> bool:
        """Claim an open investigation request."""
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        with get_db() as db:
            with db.transaction():
                # Fetch target agent to verify permission
                row = db.execute(
                    "SELECT target_agent, status FROM taskboard_investigations "
                    "WHERE cycle_id = %s AND ticker = %s AND investigation_id = %s",
                    [cycle_id, ticker, req_id]
                ).fetchone()

                if not row:
                    return False
                target_agent, status = row
                if status != "open":
                    return False
                if target_agent != "*" and target_agent != claiming_agent:
                    return False

                db.execute(
                    "UPDATE taskboard_investigations SET status = 'claimed', claimed_by = %s "
                    "WHERE cycle_id = %s AND ticker = %s AND investigation_id = %s",
                    [claiming_agent, cycle_id, ticker, req_id]
                )
                logger.info(
                    "[TaskBoard] %s claimed investigation %s",
                    claiming_agent,
                    req_id,
                )
                return True

    async def complete_investigation(
        self,
        req_id: str,
        result: str,
        ticker: str,
        cycle_id: str = "",
    ) -> bool:
        """Complete an investigation with results."""
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        with get_db() as db:
            with db.transaction():
                row = db.execute(
                    "SELECT status FROM taskboard_investigations "
                    "WHERE cycle_id = %s AND ticker = %s AND investigation_id = %s",
                    [cycle_id, ticker, req_id]
                ).fetchone()

                if not row or row[0] != "claimed":
                    return False

                db.execute(
                    "UPDATE taskboard_investigations SET status = 'completed', result = %s "
                    "WHERE cycle_id = %s AND ticker = %s AND investigation_id = %s",
                    [result, cycle_id, ticker, req_id]
                )
                logger.info(
                    "[TaskBoard] Investigation %s completed: %s",
                    req_id,
                    result[:80],
                )
                return True

    async def get_open_investigations(
        self,
        ticker: str,
        cycle_id: str = "",
        for_agent: str | None = None,
    ) -> list[dict]:
        """Get open investigation requests, optionally filtered for a specific agent."""
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        with get_db() as db:
            query = (
                "SELECT investigation_id, requester, target_agent, question "
                "FROM taskboard_investigations WHERE cycle_id = %s AND ticker = %s AND status = 'open'"
            )
            params = [cycle_id, ticker]

            rows = db.execute(query, params).fetchall()

            results = []
            for r in rows:
                investigation_id, requester, target_agent, question = r
                if for_agent and target_agent != "*" and target_agent != for_agent:
                    continue
                results.append(
                    {
                        "id": investigation_id,
                        "requester": requester,
                        "target_agent": target_agent,
                        "question": question,
                    }
                )
            return results

    def clear_board(self, ticker: str, cycle_id: str = ""):
        """Clear all findings and investigations for a completed cycle."""
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        with get_db() as db:
            db.execute("DELETE FROM taskboard_findings WHERE cycle_id = %s AND ticker = %s", [cycle_id, ticker])
            db.execute("DELETE FROM taskboard_investigations WHERE cycle_id = %s AND ticker = %s", [cycle_id, ticker])
        logger.info("[TaskBoard] Cleared board for %s in cycle %s", ticker, cycle_id)


# Global singleton
task_board = TaskBoard()
