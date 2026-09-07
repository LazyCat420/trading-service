"""Persist learning component state independently of cycle/audit completion."""
from datetime import datetime, timezone
import logging
from app.db import mongo_store

logger = logging.getLogger(__name__)


def record(component: str, state: str, *, cycle_id: str | None = None, **details) -> dict:
    row = {'component': component, 'state': state, 'cycle_id': cycle_id,
           'details': details, 'updated_at': datetime.now(timezone.utc)}
    try:
        mongo_store.upsert_doc('learning_health', {'id': component}, {'id': component, **row})
    except Exception as exc:
        logger.error('[LearningHealth] %s/%s persistence failed: %s', component, state, exc)
    return row
