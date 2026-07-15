import asyncio
import logging
import json
from app.db.connection import get_db, safe_jsonb

logger = logging.getLogger(__name__)

# Cap on the summarize() output injected into agent system prompts.
_MAX_SUMMARY_CHARS = 8000

class Whiteboard:
    """Central hub for inter-agent communication via a shared mutable document.

    Thread-safe via PostgreSQL transactions and an internal asyncio.Lock.
    Each board is scoped to a single ticker+cycle_id combination in the database.
    """
    def __init__(self):
        self._lock = asyncio.Lock()
        self._broadcast_callback = None
        self._subscribers = []

    def set_broadcast_callback(self, callback):
        self._broadcast_callback = callback

    def subscribe(self, callback):
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback):
        if callback in self._subscribers:
            self._subscribers.remove(callback)

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
            for sub in list(self._subscribers):
                try:
                    if asyncio.iscoroutinefunction(sub):
                        await sub({
                            "type": "whiteboard_update",
                            "ticker": ticker,
                            "cycle_id": cycle_id,
                            "section": section,
                            "version": new_version,
                            "author": author_agent,
                            "content": content_json
                        })
                    else:
                        sub({
                            "type": "whiteboard_update",
                            "ticker": ticker,
                            "cycle_id": cycle_id,
                            "section": section,
                            "version": new_version,
                            "author": author_agent,
                            "content": content_json
                        })
                except Exception as ex:
                    logger.warning("[Whiteboard] Dynamic subscriber callback failed: %s", ex)

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

            # Notify active subscribers of the annotation
            for sub in list(self._subscribers):
                try:
                    if asyncio.iscoroutinefunction(sub):
                        await sub({
                            "type": "whiteboard_annotation",
                            "ticker": ticker,
                            "cycle_id": cycle_id,
                            "section": section,
                            "entry_id": entry_id,
                            "author": agent,
                            "note": note
                        })
                    else:
                        sub({
                            "type": "whiteboard_annotation",
                            "ticker": ticker,
                            "cycle_id": cycle_id,
                            "section": section,
                            "entry_id": entry_id,
                            "author": agent,
                            "note": note
                        })
                except Exception as ex:
                    logger.warning("[Whiteboard] Dynamic subscriber callback failed: %s", ex)

            return True

    async def summarize(self, ticker: str, cycle_id: str) -> str:
        """Returns the full whiteboard state as a dense string for LLM injection."""
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"
        
        with get_db() as db:
            rows = db.execute(
                "SELECT id, section, author_agent, content, version, edited_by FROM whiteboard_entries "
                "WHERE cycle_id = %s AND ticker = %s AND superseded_by IS NULL "
                "ORDER BY section ASC",
                [cycle_id, ticker]
            ).fetchall()

            if not rows:
                return "" # Return empty so it doesn't take up tokens if there's no whiteboard

            lines = ["\n=== SHARED WHITEBOARD ==="]
            
            for r in rows:
                entry_id, section, author_agent, content_raw, version, edited_by = r
                content = safe_jsonb(content_raw) or {}
                
                lines.append(f"\n## {section.upper()} (v{version})")
                lines.append(f"Authors: {', '.join(edited_by)}")
                
                # Try to compress the output slightly to save tokens
                if isinstance(content, dict) and "text" in content and len(content) == 1:
                    lines.append(content["text"])
                else:
                    lines.append(json.dumps(content, indent=2))
                
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
