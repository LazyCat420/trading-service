import asyncio
import logging
import json
from app.db.connection import get_db, safe_jsonb

logger = logging.getLogger(__name__)

# Cap on the summarize() output injected into agent system prompts.
_MAX_SUMMARY_CHARS = 8000
# Per-section cap inside the summary. Without it, one fat section (the raw
# tournament_result JSON runs 7-8KB) eats the whole global budget and the
# global truncation silently drops every section after it.
_MAX_SECTION_CHARS = 1800
# Injection order: decision-relevant sections first so the global cap can only
# ever cost the tail. The old ORDER BY section ASC (alphabetical) meant
# risk_flags and tournament_result — the sections the board most needs — were
# always the first casualties of truncation.
_SECTION_PRIORITY = [
    "final_decision",
    "regime_classification",
    "risk_flags",
    "quant_report",
    "fundamental_report",
    "desk_note",
    "tournament_result",
    "market_context",
]


def _section_sort_key(section: str):
    try:
        return (0, _SECTION_PRIORITY.index(section), "")
    except ValueError:
        return (1, 0, section)  # unknown sections: after known ones, alphabetical

class Whiteboard:
    """Central hub for inter-agent communication via a shared mutable document.

    Thread-safe via PostgreSQL transactions and an internal asyncio.Lock.
    Each board is scoped to a single ticker+cycle_id combination in the database.
    """
    def __init__(self):
        self._lock = asyncio.Lock()
        self._broadcast_callback = None
        # (callback, ticker_key) pairs; ticker_key=None receives every event.
        self._subscribers: list[tuple] = []

    def set_broadcast_callback(self, callback):
        self._broadcast_callback = callback

    def subscribe(self, callback, ticker: str | None = None):
        """Register a subscriber, optionally scoped to one ticker.

        With N concurrent tickers each running its own cycle, an unscoped bus
        fires every subscriber for every event (O(N²) callbacks per cycle).
        Passing ticker= makes publish O(1) per event for that subscriber.
        """
        key = ticker.upper().strip() if ticker else None
        # Equality, not identity: bound methods compare == across accesses
        # but are never `is` each other — identity checks would double-fire
        # and leak such subscribers.
        if not any(cb == callback for cb, _ in self._subscribers):
            self._subscribers.append((callback, key))

    def unsubscribe(self, callback):
        self._subscribers = [
            (cb, key) for cb, key in self._subscribers if cb != callback
        ]

    async def _notify_subscribers(self, event: dict):
        """Fan an event out to matching subscribers. Runs OUTSIDE self._lock —
        a slow/awaiting subscriber must not serialize other tickers' writes."""
        event_ticker = event.get("ticker")
        for cb, key in list(self._subscribers):
            if key is not None and event_ticker and key != event_ticker:
                continue
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(event)
                else:
                    cb(event)
            except Exception as ex:
                logger.warning("[Whiteboard] Dynamic subscriber callback failed: %s", ex)

    async def write_section(
        self, ticker: str, cycle_id: str, section: str, content: dict | str, author_agent: str
    ) -> int:
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        if isinstance(content, str):
            try:
                content_json = json.loads(content)
            except:
                content_json = {"text": content}
        else:
            content_json = content

        async with self._lock:
            with get_db() as db:
                with db.transaction():
                    # Get the current version of this section
                    row = db.execute(
                        "SELECT id, version, edited_by FROM whiteboard_entries "
                        "WHERE cycle_id = %s AND ticker = %s AND section = %s "
                        "AND superseded_by IS NULL",
                        [cycle_id, ticker, section]
                    ).fetchone()

                    if row:
                        prev_id, prev_version, edited_by = row
                        new_version = prev_version + 1
                        
                        # Add author_agent to edited_by if not present
                        # Handle psycopg returning ARRAY as string or list
                        if isinstance(edited_by, list):
                            new_edited_by = edited_by.copy()
                        elif isinstance(edited_by, str):
                            try:
                                parsed = json.loads(edited_by)
                                new_edited_by = parsed if isinstance(parsed, list) else [edited_by]
                            except (json.JSONDecodeError, TypeError):
                                new_edited_by = [edited_by]
                        else:
                            new_edited_by = []
                        if author_agent not in new_edited_by:
                            new_edited_by.append(author_agent)
                        
                        # Insert new version
                        res = db.execute(
                            "INSERT INTO whiteboard_entries "
                            "(cycle_id, ticker, section, author_agent, content, version, edited_by) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                            [cycle_id, ticker, section, author_agent, json.dumps(content_json), new_version, new_edited_by]
                        ).fetchone()
                        new_id = res[0]

                        # Supersede old version
                        db.execute(
                            "UPDATE whiteboard_entries SET superseded_by = %s WHERE id = %s",
                            [new_id, prev_id]
                        )
                    else:
                        # First version
                        new_version = 1
                        res = db.execute(
                            "INSERT INTO whiteboard_entries "
                            "(cycle_id, ticker, section, author_agent, content, version, edited_by) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                            [cycle_id, ticker, section, author_agent, json.dumps(content_json), new_version, [author_agent]]
                        ).fetchone()
                        new_id = res[0]

        # Lock released — the DB write is durable; notification must not hold
        # the (global) lock, or one slow subscriber stalls every other ticker.
        logger.info(
            "[Whiteboard] %s updated section '%s' for %s (v%s)",
            author_agent, section, ticker, new_version
        )

        # Broadcast to legacy callback
        if self._broadcast_callback:
            try:
                await self._broadcast_callback({
                    "type": "whiteboard_update",
                    "ticker": ticker,
                    "section": section,
                    "version": new_version
                })
            except Exception as e:
                logger.debug("[Whiteboard] Broadcast failed: %s", e)

        # Notify active subscribers for dynamic coordination
        await self._notify_subscribers({
            "type": "whiteboard_update",
            "ticker": ticker,
            "cycle_id": cycle_id,
            "section": section,
            "version": new_version,
            "author": author_agent,
            "content": content_json
        })

        return new_id

    async def get_section(self, ticker: str, cycle_id: str, section: str) -> dict | None:
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"
        
        with get_db() as db:
            row = db.execute(
                "SELECT id, author_agent, content, version, edited_by FROM whiteboard_entries "
                "WHERE cycle_id = %s AND ticker = %s AND section = %s "
                "AND superseded_by IS NULL",
                [cycle_id, ticker, section]
            ).fetchone()

            if not row:
                return None

            entry_id, author_agent, content_raw, version, edited_by = row
            content = safe_jsonb(content_raw) or {}
            
            # Fetch annotations
            ann_rows = db.execute(
                "SELECT author_agent, note, created_at FROM whiteboard_annotations "
                "WHERE entry_id = %s ORDER BY created_at ASC",
                [entry_id]
            ).fetchall()
            
            annotations = [{"author": r[0], "note": r[1], "timestamp": r[2].isoformat() if r[2] else None} for r in ann_rows]

            return {
                "id": entry_id,
                "section": section,
                "author_agent": author_agent,
                "content": content,
                "version": version,
                "edited_by": edited_by,
                "annotations": annotations
            }

    async def annotate(self, entry_id: int, agent: str, note: str) -> bool:
        with get_db() as db:
            with db.transaction():
                # Verify entry exists and get ticker/section/cycle
                row = db.execute(
                    "SELECT ticker, section, cycle_id FROM whiteboard_entries WHERE id = %s",
                    [entry_id]
                ).fetchone()
                if not row:
                    return False
                ticker, section, cycle_id = row

                db.execute(
                    "INSERT INTO whiteboard_annotations (entry_id, author_agent, note) VALUES (%s, %s, %s)",
                    [entry_id, agent, note]
                )
        logger.info("[Whiteboard] %s annotated entry_id %s", agent, entry_id)

        # Notify AFTER releasing the pooled connection — a subscriber that
        # itself hits the DB while we hold a lease can exhaust the pool.
        await self._notify_subscribers({
            "type": "whiteboard_annotation",
            "ticker": ticker,
            "cycle_id": cycle_id,
            "section": section,
            "entry_id": entry_id,
            "author": agent,
            "note": note
        })

        return True

    async def summarize(self, ticker: str, cycle_id: str) -> str:
        """Returns the full whiteboard state as a dense string for LLM injection."""
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"
        
        with get_db() as db:
            rows = db.execute(
                "SELECT id, section, author_agent, content, version, edited_by FROM whiteboard_entries "
                "WHERE cycle_id = %s AND ticker = %s AND superseded_by IS NULL",
                [cycle_id, ticker]
            ).fetchall()

            if not rows:
                return "" # Return empty so it doesn't take up tokens if there's no whiteboard

            rows = sorted(rows, key=lambda r: _section_sort_key(r[1]))

            lines = ["\n=== SHARED WHITEBOARD ==="]

            for r in rows:
                entry_id, section, author_agent, content_raw, version, edited_by = r
                content = safe_jsonb(content_raw) or {}

                lines.append(f"\n## {section.upper()} (v{version})")
                lines.append(f"Authors: {', '.join(edited_by)}")

                # Try to compress the output slightly to save tokens
                if isinstance(content, dict) and "text" in content and len(content) == 1:
                    body = content["text"]
                else:
                    body = json.dumps(content, indent=2)
                if len(body) > _MAX_SECTION_CHARS:
                    body = (
                        body[:_MAX_SECTION_CHARS]
                        + f"\n[... '{section}' truncated — whiteboard_read('{section}') for full content ...]"
                    )
                lines.append(body)

                ann_rows = db.execute(
                    "SELECT author_agent, note FROM whiteboard_annotations "
                    "WHERE entry_id = %s ORDER BY created_at ASC",
                    [entry_id]
                ).fetchall()
                
                if ann_rows:
                    lines.append("\n### Annotations:")
                    for ann in ann_rows:
                        lines.append(f"- [{ann[0]}]: {ann[1]}")
                        
            lines.append("========================\n")
            summary = "\n".join(lines)
            # The summary is injected verbatim into every agent's system
            # prompt — cap it so a fat board can't snowball every context.
            if len(summary) > _MAX_SUMMARY_CHARS:
                summary = (
                    summary[:_MAX_SUMMARY_CHARS]
                    + "\n[... whiteboard truncated — read specific sections via whiteboard_read ...]\n"
                )
            return summary

    def cleanup_old_entries(
        self, *, max_age_days: int = 14, default_cycle_age_days: int = 7
    ) -> int:
        """Retention: whiteboard boards were previously never deleted.

        Removes superseded versions and whole boards older than max_age_days,
        and prunes the legacy 'default_cycle' accumulator faster. Returns the
        number of rows deleted. Safe to call at cycle end (non-fatal).
        """
        deleted = 0
        try:
            with get_db() as db:
                for where, params in (
                    ("superseded_by IS NOT NULL AND created_at < now() - (%s || ' days')::interval",
                     [str(max_age_days)]),
                    ("created_at < now() - (%s || ' days')::interval", [str(max_age_days)]),
                    ("cycle_id = 'default_cycle' AND created_at < now() - (%s || ' days')::interval",
                     [str(default_cycle_age_days)]),
                ):
                    res = db.execute(
                        f"DELETE FROM whiteboard_entries WHERE {where}", params
                    )
                    rc = getattr(res, "rowcount", None)
                    if rc is None:
                        rc = getattr(getattr(res, "_cursor", None), "rowcount", 0)
                    deleted += rc if rc and rc > 0 else 0
            if deleted:
                logger.info("[Whiteboard] Retention pass removed %d entries", deleted)
        except Exception as e:
            logger.warning("[Whiteboard] Retention pass failed (non-fatal): %s", e)
        return deleted

# Global singleton
whiteboard = Whiteboard()
