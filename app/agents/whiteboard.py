import asyncio
import logging
import json
import uuid
from datetime import datetime, timezone, timedelta
from app.db import mongo_query, mongo_store
from app.agents.whiteboard_sections import sort_key as _section_sort_key
from app.agents.whiteboard_sections import (
    ANNOTATIONS_ONLY,
    SKIP,
    render_mode,
)

logger = logging.getLogger(__name__)

_MAX_SUMMARY_CHARS = 8000
_MAX_SECTION_CHARS = 1800


class Whiteboard:
    """Central hub for inter-agent communication via a shared mutable document.

    Thread-safe via MongoDB queries and an internal asyncio.Lock.
    Each board is scoped to a single ticker+cycle_id combination in the database.
    """
    def __init__(self):
        self._lock = asyncio.Lock()
        self._broadcast_callback = None
        self._subscribers: list[tuple] = []

    def set_broadcast_callback(self, callback):
        self._broadcast_callback = callback

    def subscribe(self, callback, ticker: str | None = None):
        """Register a subscriber, optionally scoped to one ticker."""
        key = ticker.upper().strip() if ticker else None
        if not any(cb == callback for cb, _ in self._subscribers):
            self._subscribers.append((callback, key))

    def unsubscribe(self, callback):
        self._subscribers = [
            (cb, key) for cb, key in self._subscribers if cb != callback
        ]

    async def _notify_subscribers(self, event: dict):
        """Fan an event out to matching subscribers."""
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
                logger.exception(
                    "[Whiteboard] %s/%s: subscriber failed on section '%s': %s",
                    event.get("cycle_id") if isinstance(event, dict) else "?",
                    event_ticker or "?",
                    event.get("section") if isinstance(event, dict) else "?",
                    ex,
                )

    async def write_section(
        self, ticker: str, cycle_id: str, section: str, content: dict | str, author_agent: str
    ) -> str:
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        if isinstance(content, str):
            try:
                content_json = json.loads(content)
            except Exception:
                content_json = {"text": content}
        else:
            content_json = content

        async with self._lock:
            # Get the current active version of this section
            row = mongo_query.find_row(
                'whiteboard_entries',
                {'cycle_id': cycle_id, 'ticker': ticker, 'section': section, 'superseded_by': None},
                ['id', 'version', 'edited_by'],
            )

            now_utc = datetime.now(timezone.utc)
            new_id = f"wb_{uuid.uuid4().hex[:10]}"

            if row:
                prev_id, prev_version, edited_by = row
                new_version = int(prev_version or 1) + 1

                if isinstance(edited_by, list):
                    new_edited_by = edited_by.copy()
                elif isinstance(edited_by, str):
                    try:
                        parsed = json.loads(edited_by)
                        new_edited_by = parsed if isinstance(parsed, list) else [edited_by]
                    except Exception:
                        new_edited_by = [edited_by]
                else:
                    new_edited_by = []
                if author_agent not in new_edited_by:
                    new_edited_by.append(author_agent)

                # Insert new version
                mongo_store.insert_docs('whiteboard_entries', [{
                    'id': new_id,
                    'cycle_id': cycle_id,
                    'ticker': ticker,
                    'section': section,
                    'author_agent': author_agent,
                    'content': content_json,
                    'version': new_version,
                    'edited_by': new_edited_by,
                    'superseded_by': None,
                    'created_at': now_utc,
                }])

                # Supersede old version
                mongo_store.update_docs('whiteboard_entries', {'id': prev_id}, {'$set': {'superseded_by': new_id}})
            else:
                new_version = 1
                mongo_store.insert_docs('whiteboard_entries', [{
                    'id': new_id,
                    'cycle_id': cycle_id,
                    'ticker': ticker,
                    'section': section,
                    'author_agent': author_agent,
                    'content': content_json,
                    'version': new_version,
                    'edited_by': [author_agent],
                    'superseded_by': None,
                    'created_at': now_utc,
                }])

        logger.info(
            "[Whiteboard] %s updated section '%s' for %s (v%s)",
            author_agent, section, ticker, new_version,
        )

        if self._broadcast_callback:
            try:
                await self._broadcast_callback({
                    "type": "whiteboard_update",
                    "ticker": ticker,
                    "section": section,
                    "version": new_version,
                })
            except Exception as e:
                logger.debug("[Whiteboard] Broadcast failed: %s", e)

        await self._notify_subscribers({
            "type": "whiteboard_update",
            "ticker": ticker,
            "cycle_id": cycle_id,
            "section": section,
            "version": new_version,
            "author": author_agent,
            "content": content_json,
        })

        return new_id

    async def get_section(self, ticker: str, cycle_id: str, section: str) -> dict | None:
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        row = mongo_query.find_row(
            'whiteboard_entries',
            {'cycle_id': cycle_id, 'ticker': ticker, 'section': section, 'superseded_by': None},
            ['id', 'author_agent', 'content', 'version', 'edited_by'],
        )

        if not row:
            return None

        entry_id, author_agent, content_raw, version, edited_by = row
        content = content_raw if isinstance(content_raw, dict) else (json.loads(content_raw) if isinstance(content_raw, str) else {})

        ann_docs = mongo_store.find_docs(
            'whiteboard_annotations',
            {'cycle_id': cycle_id, 'ticker': ticker, 'section': section},
            sort=[('created_at', 1)],
        )

        annotations = [
            {
                "author": doc.get("author_agent"),
                "note": doc.get("note"),
                "timestamp": doc["created_at"].isoformat() if hasattr(doc.get("created_at"), "isoformat") else str(doc.get("created_at")),
            }
            for doc in ann_docs
        ]

        return {
            "id": entry_id,
            "section": section,
            "author_agent": author_agent,
            "content": content,
            "version": version,
            "edited_by": edited_by if isinstance(edited_by, list) else [author_agent],
            "annotations": annotations,
        }

    async def annotate(self, entry_id: str, agent: str, note: str) -> bool:
        row = mongo_query.find_row('whiteboard_entries', {'id': entry_id}, ['ticker', 'section', 'cycle_id'])
        if not row:
            return False
        ticker, section, cycle_id = row

        now_utc = datetime.now(timezone.utc)
        ann_id = f"ann_{uuid.uuid4().hex[:10]}"
        mongo_store.insert_docs('whiteboard_annotations', [{
            'id': ann_id,
            'entry_id': entry_id,
            'cycle_id': cycle_id,
            'ticker': ticker,
            'section': section,
            'author_agent': agent,
            'note': note,
            'created_at': now_utc,
        }])

        logger.info("[Whiteboard] %s annotated entry_id %s", agent, entry_id)

        await self._notify_subscribers({
            "type": "whiteboard_annotation",
            "ticker": ticker,
            "cycle_id": cycle_id,
            "section": section,
            "entry_id": entry_id,
            "author": agent,
            "note": note,
        })

        return True

    async def summarize(
        self, ticker: str, cycle_id: str, *, for_agent_prompt: bool = False
    ) -> str:
        """The whiteboard as a dense string."""
        ticker = ticker.upper().strip()
        cycle_id = cycle_id.strip() if cycle_id else "default_cycle"

        docs = mongo_store.find_docs(
            'whiteboard_entries',
            {'cycle_id': cycle_id, 'ticker': ticker, 'superseded_by': None},
        )

        if not docs:
            return ""

        docs = sorted(docs, key=lambda d: _section_sort_key(d.get("section", "")))

        ann_docs = mongo_store.find_docs(
            'whiteboard_annotations',
            {'cycle_id': cycle_id, 'ticker': ticker},
            sort=[('created_at', 1)],
        )

        ann_by_section: dict[str, list[tuple]] = {}
        for ann in ann_docs:
            sec = ann.get("section", "")
            ann_by_section.setdefault(sec, []).append((ann.get("author_agent"), ann.get("note")))

        try:
            from app.v3.shared_desk import tournament_debate_mode, TOURNAMENT_MODE_SHADOW
            if tournament_debate_mode() == TOURNAMENT_MODE_SHADOW:
                docs = [d for d in docs if d.get("section") not in ("tournament_result", "debate_judge")]
        except Exception as mode_err:
            logger.warning("[Whiteboard] debate-mode gate skipped: %s", mode_err)

        if not docs:
            return ""

        lines = ["\n=== SHARED WHITEBOARD ==="]

        for d in docs:
            entry_id = d.get("id")
            section = d.get("section", "")
            author_agent = d.get("author_agent", "")
            content_raw = d.get("content", {})
            version = d.get("version", 1)
            edited_by = d.get("edited_by", [author_agent])
            if not isinstance(edited_by, list):
                edited_by = [author_agent]

            ann_rows = ann_by_section.get(section, ())
            mode = render_mode(
                section,
                for_agent_prompt=for_agent_prompt,
                has_annotations=bool(ann_rows),
            )
            if mode == SKIP:
                continue

            lines.append(f"\n## {section.upper()} (v{version}, entry_id={entry_id})")
            lines.append(f"Authors: {', '.join(edited_by)}")

            if mode == ANNOTATIONS_ONLY:
                lines.append(
                    f"(full text in the SharedDesk context above — "
                    f"{len(ann_rows)} teammate note(s) on it:)"
                )
            else:
                content = content_raw if isinstance(content_raw, dict) else (json.loads(content_raw) if isinstance(content_raw, str) else {})
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

            if ann_rows:
                lines.append("\n### Annotations:")
                for ann in ann_rows:
                    lines.append(f"- [{ann[0]}]: {ann[1]}")

        if len(lines) == 1:
            return ""

        lines.append("========================\n")
        summary = "\n".join(lines)
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = (
                summary[:_MAX_SUMMARY_CHARS]
                + "\n[... whiteboard truncated — read specific sections via whiteboard_read ...]\n"
            )
        return summary

    def cleanup_old_entries(
        self, *, max_age_days: int = 14, default_cycle_age_days: int = 7
    ) -> int:
        deleted = 0
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
            deleted += mongo_store.delete_docs('whiteboard_entries', {'created_at': {'$lt': cutoff}})
            deleted += mongo_store.delete_docs('whiteboard_annotations', {'created_at': {'$lt': cutoff}})
            if deleted:
                logger.info("[Whiteboard] Retention pass removed %d entries", deleted)
        except Exception as e:
            logger.warning("[Whiteboard] Retention pass failed (non-fatal): %s", e)
        return deleted


# Global singleton
whiteboard = Whiteboard()
