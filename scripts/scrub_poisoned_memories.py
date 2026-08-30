#!/usr/bin/env python3
"""
Scrub Poisoned Memories — One-time cleanup script.

Removes entries from the database and disk that contain error/warning text
that was accidentally stored as trading lessons, market memories, or claims.

This happened because the "response was cut short" warning from Prism Gateway
was treated as real LLM output and fed into the learning pipeline.

Usage:
    python scripts/scrub_poisoned_memories.py [--dry-run]

    --dry-run: Show what would be deleted without actually deleting.

Safety:
    - All deletions use parameterized queries (no SQL injection risk)
    - Disk writes use atomic rename (no partial writes)
    - Archived entries are preserved (archived before deletion)
"""

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("scrub_poisoned")

# Patterns that indicate poisoned content
POISON_SUBSTRINGS = [
    "response was cut short",
    "max_tokens",
    "The model's response was cut short",
    "⚠️ The model",
    "context budget exceeded",
    "token limit exceeded",
]


def _is_poisoned(text: str) -> bool:
    """Check if text contains any poison substring."""
    if not text:
        return False
    lower = text.lower()
    for pattern in POISON_SUBSTRINGS:
        if pattern.lower() in lower:
            return True
    return False


def scrub_evolution_lessons(dry_run: bool = True) -> int:
    """Remove poisoned entries from evolution_lessons table."""
    try:
        from datetime import datetime, timezone

        from app.db import mongo_store

        if True:
            rows = mongo_store.find_docs("evolution_lessons", {})
            poisoned = [(r.get("id"), r.get("lesson_text") or "")
                        for r in rows if _is_poisoned(r.get("lesson_text") or "")]

            if not poisoned:
                logger.info("[SCRUB] evolution_lessons: No poisoned entries found")
                return 0

            logger.info(
                "[SCRUB] evolution_lessons: Found %d poisoned entries", len(poisoned)
            )
            for row_id, text in poisoned:
                logger.info("  → [%s] %s", row_id[:8], text[:100])

            if not dry_run:
                by_id = {r.get("id"): r for r in rows}
                for row_id, text in poisoned:
                    # Archive before deleting. INSERT ... SELECT becomes a read
                    # of the row we already hold plus one write, carrying the
                    # same columns and the same 'scrubbed_poison' status.
                    try:
                        src = by_id.get(row_id) or {}
                        mongo_store.upsert_doc(
                            "evolution_lessons_archive", {"id": row_id},
                            {"id": row_id,
                             "session_id": src.get("session_id"),
                             "round": src.get("round"),
                             "score": src.get("score"),
                             "status": "scrubbed_poison",
                             "lesson_text": src.get("lesson_text"),
                             "timestamp": src.get("timestamp"),
                             "archived_at": datetime.now(timezone.utc)},
                            insert_only=True,
                        )
                    except Exception:
                        pass  # already archived is not an error

                    mongo_store.delete_docs("evolution_lessons", {"id": row_id})

                    # Also remove stale embeddings. The comment that used to sit
                    # here recorded that a PG-only DELETE "scrubbed a store
                    # nobody reads and left the live Mongo docs poisoned" — the
                    # same was true of the lesson rows above until 2026-08-30.
                    try:
                        mongo_store.delete_docs(
                            "embeddings",
                            {"source_table": "evolution_lessons",
                             "source_id": {"$in": [row_id, str(row_id)]}},
                        )
                    except Exception:
                        logger.warning(
                            "[SCRUB] embeddings cleanup failed for id=%s", row_id
                        )

                logger.info(
                    "[SCRUB] evolution_lessons: Deleted %d poisoned entries",
                    len(poisoned),
                )

            return len(poisoned)

    except Exception as e:
        logger.error("[SCRUB] evolution_lessons failed: %s", e)
        return 0


def scrub_cycle_context(dry_run: bool = True) -> int:
    """Remove poisoned entries from cycle_context table (claims/summaries)."""
    try:
        from app.db import mongo_store

        if True:
            rows = mongo_store.find_docs("cycle_context", {})
            poisoned = [r.get("id") for r in rows
                        if _is_poisoned(r.get("summary") or "")]

            if not poisoned:
                logger.info("[SCRUB] cycle_context: No poisoned entries found")
                return 0

            logger.info(
                "[SCRUB] cycle_context: Found %d poisoned entries", len(poisoned)
            )

            if not dry_run:
                for row_id in poisoned:
                    mongo_store.delete_docs("cycle_context", {"id": row_id})
                logger.info(
                    "[SCRUB] cycle_context: Deleted %d poisoned entries",
                    len(poisoned),
                )

            return len(poisoned)

    except Exception as e:
        logger.error("[SCRUB] cycle_context failed: %s", e)
        return 0


def scrub_market_memory(dry_run: bool = True) -> int:
    """Remove poisoned entries from MARKET_MEMORY.md file."""
    project_root = Path(__file__).resolve().parent.parent
    memory_path = project_root / "data" / "memory" / "MARKET_MEMORY.md"

    if not memory_path.exists():
        logger.info("[SCRUB] MARKET_MEMORY.md: File not found at %s", memory_path)
        return 0

    content = memory_path.read_text(encoding="utf-8")
    delimiter = "§"
    entries = [e.strip() for e in content.split(delimiter) if e.strip()]

    poisoned = [e for e in entries if _is_poisoned(e)]
    clean = [e for e in entries if not _is_poisoned(e)]

    if not poisoned:
        logger.info("[SCRUB] MARKET_MEMORY.md: No poisoned entries found")
        return 0

    logger.info(
        "[SCRUB] MARKET_MEMORY.md: Found %d poisoned entries out of %d total",
        len(poisoned),
        len(entries),
    )
    for entry in poisoned:
        logger.info("  → %s", entry[:100])

    if not dry_run:
        new_content = f" {delimiter}\n".join(clean) if clean else ""
        # Atomic write
        fd, tmp = tempfile.mkstemp(
            dir=str(memory_path.parent), suffix=".tmp", prefix=".scrub_"
        )
        try:
            os.write(fd, new_content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp, str(memory_path))
            logger.info(
                "[SCRUB] MARKET_MEMORY.md: Removed %d poisoned entries, %d remain",
                len(poisoned),
                len(clean),
            )
        except Exception as e:
            try:
                os.close(fd)
            except Exception:
                pass
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            logger.error("[SCRUB] MARKET_MEMORY.md: Failed to write: %s", e)

    return len(poisoned)


def main():
    parser = argparse.ArgumentParser(
        description="Scrub poisoned error messages from trading memories and lessons"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be deleted without actually deleting",
    )
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN MODE — no changes will be made ===")

    total = 0
    total += scrub_evolution_lessons(dry_run=args.dry_run)
    total += scrub_cycle_context(dry_run=args.dry_run)
    total += scrub_market_memory(dry_run=args.dry_run)

    logger.info(
        "=== SCRUB COMPLETE: %d poisoned entries %s ===",
        total,
        "found (dry run)" if args.dry_run else "removed",
    )


if __name__ == "__main__":
    main()
