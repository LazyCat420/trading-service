import asyncio
import logging
import json
from app.db.connection import get_db, safe_jsonb
from app.agents.whiteboard_sections import sort_key as _section_sort_key
from app.agents.whiteboard_sections import (
    ANNOTATIONS_ONLY,
    SKIP,
    render_mode,
)

logger = logging.getLogger(__name__)

# Cap on the summarize() output.
#
# UNCHANGED at 8,000 on purpose. The obvious response to "93% of boards
# overflow" is to raise this, and the measurement says that is the wrong lever:
# across 331 boards a 16,000-char cap still delivers only 10.9% of boards whole
# and 60,000 is needed for 96%, because the median board holds 26,354 chars.
# Dropping the duplicates instead (see `whiteboard_sections`) fits 331 of 331
# boards inside this existing cap. The block got smaller, not bigger.
_MAX_SUMMARY_CHARS = 8000
# Per-section cap inside the summary. Without it, one fat section (the raw
# tournament_result JSON runs 7-8KB) eats the whole global budget and the
# global truncation silently drops every section after it.
#
# It does not bite on the collaboration sections this method now leads with:
# every section measured over 1800 chars was a desk-carried artifact.
_MAX_SECTION_CHARS = 1800

# Section ordering and inclusion live in `whiteboard_sections`, which ranks by
# CLASS (collaboration before duplicate) rather than by a hand-maintained list
# of names. The list that used to live here went stale three times: the v3
# debate sections were missing from it (fixed 08-04), and `bull_defense`,
# `delta_report` and `trade_decision` were still missing on 08-08 — 73
# `bull_defense` entries written, 0 ever delivered.

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
            
            # Annotations for this SECTION, across every version of it — same
            # reason as in summarize(): an annotation written against v1 is
            # still the dispute someone recorded, and dropping it when the
            # author revises the entry silently empties the disagreement
            # channel. Matching on the current entry id alone orphaned 20 of
            # 518 measured notes.
            ann_rows = db.execute(
                "SELECT a.author_agent, a.note, a.created_at "
                "FROM whiteboard_annotations a "
                "JOIN whiteboard_entries e ON e.id = a.entry_id "
                "WHERE e.cycle_id = %s AND e.ticker = %s AND e.section = %s "
                "ORDER BY a.created_at ASC",
                [cycle_id, ticker, section]
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

    async def summarize(
        self, ticker: str, cycle_id: str, *, for_agent_prompt: bool = False
    ) -> str:
        """The whiteboard as a dense string.

        `for_agent_prompt=True` is the block injected into every v3 agent's
        context. It drops the sections the SharedDesk already delivers by its
        own path — see `whiteboard_sections.wanted_in_summary`. Measured
        2026-08-08: those duplicates were 87% of everything this method
        delivered, and they pushed the whiteboard's OWN payload past the cap on
        93% of boards. `market_context` — written 312 times, mandatory in the
        junior analyst's prompt — reached 0 of 39 downstream readers.

        Default False: an explicit `whiteboard_read`/`whiteboard_summarize`
        call asked for the board and gets the board.
        """
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

            # Every annotation for this board in ONE query, keyed by SECTION
            # rather than by entry id.
            #
            # Two fixes in one. It used to run a SELECT per section inside the
            # render loop (15 round-trips per prompt build, on a pooled
            # connection held for the whole render). And it used to match the
            # CURRENT entry id only, so annotating an entry that was later
            # rewritten orphaned the note permanently — 20 of 518 measured, and
            # it grows with every rewrite, on a channel whose whole purpose is
            # that "unwritten disagreement reads as consensus". `market_context`
            # and `risk_flags` both reach v4 in production.
            #
            # The thread follows the section, so a dispute survives the author
            # revising what it disputed. Nothing is mutated to achieve it: the
            # annotation still points at the exact version it was written
            # against, which is what makes the history honest.
            ann_by_section: dict[str, list[tuple]] = {}
            for sec, author, note in db.execute(
                "SELECT e.section, a.author_agent, a.note "
                "FROM whiteboard_annotations a "
                "JOIN whiteboard_entries e ON e.id = a.entry_id "
                "WHERE e.cycle_id = %s AND e.ticker = %s AND e.section = ANY(%s) "
                "ORDER BY a.created_at ASC",
                [cycle_id, ticker, [r[1] for r in rows]],
            ).fetchall():
                ann_by_section.setdefault(sec, []).append((author, note))

            # Shadow mode: the desk's compressed context is NOT the only way the
            # debate reaches the Board — the orchestrator also writes
            # tournament_result to the whiteboard, and this summary is injected
            # into every agent's prompt. Gating only get_compressed_context
            # would leave the verdict fully legible here and the experiment
            # would measure nothing. Execution, storage and the veto are
            # untouched; this drops the section from the PROMPT only.
            try:
                from app.v3.shared_desk import tournament_debate_mode, TOURNAMENT_MODE_SHADOW
                if tournament_debate_mode() == TOURNAMENT_MODE_SHADOW:
                    rows = [r for r in rows if r[1] not in ("tournament_result", "debate_judge")]
            except Exception as mode_err:  # noqa: BLE001 — fail-open to active
                logger.warning("[Whiteboard] debate-mode gate skipped: %s", mode_err)

            if not rows:
                return ""

            lines = ["\n=== SHARED WHITEBOARD ==="]

            for r in rows:
                entry_id, section, author_agent, content_raw, version, edited_by = r
                ann_rows = ann_by_section.get(section, ())
                mode = render_mode(
                    section,
                    for_agent_prompt=for_agent_prompt,
                    has_annotations=bool(ann_rows),
                )
                if mode == SKIP:
                    continue

                # entry_id is printed so agents can whiteboard_annotate straight
                # from this summary — prompts say "don't spend a turn re-reading",
                # but annotate requires an entry_id that only a read used to
                # surface (measured: VA passed entry_id=0 and failed).
                lines.append(f"\n## {section.upper()} (v{version}, entry_id={entry_id})")
                lines.append(f"Authors: {', '.join(edited_by)}")

                if mode == ANNOTATIONS_ONLY:
                    # The desk already delivered this artifact's body in its own
                    # block; only the notes teammates left on it are unique to
                    # the whiteboard. Say where the body went, so the header is
                    # not read as an empty section.
                    lines.append(
                        f"(full text in the SharedDesk context above — "
                        f"{len(ann_rows)} teammate note(s) on it:)"
                    )
                else:
                    # Try to compress the output slightly to save tokens
                    content = safe_jsonb(content_raw) or {}
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

            # Every section was skipped (an agent-prompt board carrying only
            # desk-duplicated sections with no annotations on them). Return ""
            # rather than a header promising a whiteboard and then showing an
            # empty one — the caller injects nothing when this is empty.
            if len(lines) == 1:
                return ""

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
