"""Post-cycle incident capture.

AutoResearch already reflects on the full cycle and delivery receipts. The
former three random-slice auditors plus chief produced repeated global prose
and lost event ordering. Capture concrete failure evidence here; do not pay a
second council to convert operational errors into analyst instructions.
"""
import logging
from app.db import mongo_store
from app.services.learning.records import write
from app.services.learning.health import record

logger = logging.getLogger(__name__)


async def run_post_cycle_evaluation(cycle_id: str):
    summary = {'observed': 0, 'stored': 0, 'failed': 0, 'llm_calls': 0}
    try:
        rows = mongo_store.find_docs('pipeline_events', {
            'cycle_id': cycle_id, 'status': {'$in': ['error', 'failed', 'timeout']},
        }, sort=[('created_at', 1)], limit=30)
        summary['observed'] = len(rows)
        for row in rows:
            text = str(row.get('detail') or '').strip()
            if len(text) < 10:
                continue
            try:
                # Error text is evidence, not the lesson body: the error guard
                # must not suppress an incident merely because it names an error.
                message = f"Investigate {row.get('phase') or 'pipeline'}/{row.get('step') or 'execution'} failure; verify the associated event before proposing a remedy."
                write(message, cycle_id=cycle_id, producer='cycle_incident_capture',
                      kind='incident', source_refs=[f"pipeline_events:{row.get('id') or cycle_id}"], evidence=text)
                summary['stored'] += 1
            except Exception as exc:
                summary['failed'] += 1
                logger.error('[Evaluator] incident capture failed for %s: %s', cycle_id, exc)
    except Exception as exc:
        summary['failed'] += 1
        logger.error('[Evaluator] cycle read failed for %s: %s', cycle_id, exc)
    record('cycle_incidents', 'failed' if summary['failed'] else 'ready', cycle_id=cycle_id, **summary)
    return summary
