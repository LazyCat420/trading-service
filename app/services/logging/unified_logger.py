import json
import logging
import sys
import traceback
import uuid
from datetime import datetime, timezone
from app.utils.trace import get_trace_id

logger = logging.getLogger(__name__)

# How often to surface accumulated drops after the first one. Kept coarse so a
# hard outage (every record dropping) doesn't flood stderr.
_DROP_LOG_EVERY = 50


class DbLoggingHandler(logging.Handler):
    """
    Standard logging handler that writes log messages with level WARNING or higher
    directly into the PostgreSQL 'execution_errors' and 'cycle_audit_log' tables.
    Designed with zero-crash propagation — DB failures will not interrupt execution.

    Failures inside the handler are COUNTED and reported to stderr (never via
    logging — that would recurse into this handler). Before this counter, the
    blanket except/pass made a dead capture path indistinguishable from a
    quiet system: the 07-31→08-02 outage ran 2.5 days unnoticed.
    """

    # Class-level so every instance and every registration shares one tally.
    dropped = 0

    def __init__(self, level=logging.WARNING):
        super().__init__(level=level)

    @classmethod
    def _note_drop(cls, err: BaseException) -> None:
        cls.dropped += 1
        if cls.dropped == 1 or cls.dropped % _DROP_LOG_EVERY == 0:
            print(
                f"[UnifiedLogger] DB error capture has dropped {cls.dropped} "
                f"record(s) this process; latest cause: {err!r}",
                file=sys.stderr,
                flush=True,
            )

    def emit(self, record):
        try:
            # Avoid recursive loops if db client logs a warning/error.
            # yfinance is excluded too (open item 8, 2026-08-05): its
            # "possibly delisted" ERROR for every thin ADR propagated to root
            # and this handler promoted it to severity='critical' in
            # cycle_audit_log (15x in one 3.5h window for $SKHYV). A delisted
            # ticker is vendor noise, not a critical event — it stays visible
            # on stdout at ERROR, just not in the audit tables.
            if record.name.startswith(("psycopg", "app.db", "yfinance")):
                return

            cycle_id = getattr(record, "cycle_id", get_trace_id())
            # If we don't have a cycle_id, use a fallback
            if not cycle_id:
                cycle_id = "system-log"

            phase = getattr(record, "phase", "unknown")
            ticker = getattr(record, "ticker", "system")
            error_type = getattr(record, "error_type", record.levelname)
            
            # Format message
            error_message = record.getMessage()
            
            # Format stack trace
            stack_trace = ""
            if record.exc_info:
                stack_trace = "".join(traceback.format_exception(*record.exc_info))
            else:
                stack_trace = record.stack_info or ""

            # Save to database
            self._write_to_db(cycle_id, phase, ticker, error_type, error_message, stack_trace, record.levelname)
        except Exception as e:
            # Suppress logging failures to prevent loop/hangs — but count them.
            self._note_drop(e)

    def _write_to_db(self, cycle_id: str, phase: str, ticker: str, error_type: str, error_message: str, stack_trace: str, levelname: str):
        try:
            from app.db.connection import get_db
            
            error_id = f"err_{uuid.uuid4().hex[:12]}"
            now = datetime.now(timezone.utc)
            audit_id = f"aud_{uuid.uuid4().hex[:12]}"
            severity = "warning" if levelname == "WARNING" else "critical"
            # Build both records once so the PG rows and the Mongo mirrors share ids.
            err_rec = {
                "id": error_id, "cycle_id": cycle_id, "phase": phase, "ticker": ticker,
                "error_type": error_type, "error_message": error_message[:1000],
                "stack_trace": stack_trace[:4000], "created_at": now,
            }
            audit_data = {
                "error_id": error_id, "error_type": error_type,
                "stack_trace_snippet": stack_trace[:500] if stack_trace else "",
            }
            audit_rec = {
                "id": audit_id, "cycle_id": cycle_id, "timestamp": now,
                "audit_type": "log_event", "event_type": levelname.lower(),
                "phase": phase, "ticker": ticker, "severity": severity,
                "message": f"[{levelname}] {error_message[:500]}", "data": audit_data,
            }
            with get_db() as db:
                # 1. Insert into execution_errors
                db.execute(
                    """
                    INSERT INTO execution_errors (id, cycle_id, phase, ticker, error_type, error_message, stack_trace, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (err_rec["id"], err_rec["cycle_id"], err_rec["phase"], err_rec["ticker"],
                     err_rec["error_type"], err_rec["error_message"], err_rec["stack_trace"], err_rec["created_at"])
                )
                # 2. Duplicate to cycle_audit_log
                db.execute(
                    """
                    INSERT INTO cycle_audit_log (id, cycle_id, timestamp, audit_type, event_type, phase, ticker, severity, message, data)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (audit_rec["id"], audit_rec["cycle_id"], audit_rec["timestamp"], audit_rec["audit_type"],
                     audit_rec["event_type"], audit_rec["phase"], audit_rec["ticker"], audit_rec["severity"],
                     audit_rec["message"], json.dumps(audit_rec["data"]))
                )
            # Best-effort Mongo dual-write (per-table flag; never breaks PG above).
            try:
                from app.db import mongo_store
                if mongo_store.writes_mongo("execution_errors"):
                    mongo_store.insert_docs("execution_errors", [err_rec])
                if mongo_store.writes_mongo("cycle_audit_log"):
                    mongo_store.insert_docs("cycle_audit_log", [audit_rec])
            except Exception as me:
                logging.getLogger(__name__).warning(
                    "[UnifiedLogger] Mongo mirror failed (non-fatal): %s", me
                )
        except Exception as e:
            self._note_drop(e)


def setup_db_logger():
    """Register the DbLoggingHandler globally."""
    root_logger = logging.getLogger()
    
    # Check if handler already registered
    for handler in root_logger.handlers:
        if isinstance(handler, DbLoggingHandler):
            return
            
    handler = DbLoggingHandler()
    root_logger.addHandler(handler)
    logger.info("[Logger] Unified DB error logger handler registered.")
