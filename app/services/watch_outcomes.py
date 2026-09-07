"""Complete the allocation ledger after a terminal cycle, idempotently.

The wake command ID is not the analysis cycle ID. Outcome comparisons preserve
that join and separate action changes from risk updates on a retained position.
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone

TERMINAL = {'done', 'completed', 'error', 'stopped', 'interrupted'}


def _object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        for parse in (json.loads, ast.literal_eval):
            try:
                parsed = parse(value)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, SyntaxError, TypeError):
                pass
    return {}


def _decision(row):
    result = _object((row or {}).get('result_json'))
    if (result.get('action') not in ('BUY', 'SELL', 'HOLD')
            or result.get('triage_tier') in ('v3_glance', 'v3_aborted')
            or result.get('confidence', 1) == 0):
        return None
    return result


def score_completed_allocations(*, db=None, hours: int = 168, dry_run: bool = False,
                                limit: int = 200) -> dict:
    if db is None:
        from app.db.mongo_store import get_doc_db
        db = get_doc_db()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    pending = list(db.watch_triage_log.find({'created_at': {'$gte': since},
        'fired': True, 'outcome': None}).sort('created_at', 1).limit(limit))
    scored, skipped = 0, 0
    outcomes = []
    for row in pending:
        cmd = db.v3_system_commands.find_one({'id': row.get('cycle_id')}) or {}
        cycle = _object(cmd.get('result')).get('cycle_id')
        summary = db.cycle_run_summaries.find_one({'cycle_id': cycle}) if cycle else None
        status = str((summary or {}).get('status') or '').lower()
        if not cycle or status not in TERMINAL:
            skipped += 1
            continue
        woke = _decision(db.analysis_results.find_one({'cycle_id': cycle, 'ticker': row['ticker']}))
        prior = _decision(db.analysis_results.find_one({'ticker': row['ticker'],
            'created_at': {'$lt': row['created_at']}}, sort=[('created_at', -1)]))
        comparable = bool(prior and woke)
        fields = ('stop_loss', 'take_profit', 'position_size_pct', 'dynamic_trigger', 'entry_mode', 'trigger_purpose')
        old_risk = {k: (prior.get('estimate') or {}).get(k) for k in fields} if prior else None
        new_risk = {k: (woke.get('estimate') or {}).get(k) for k in fields} if woke else None
        outcome = {'cycle_id': cycle, 'cycle_status': status,
            'prior_action': prior.get('action') if prior else None,
            'woke_action': woke.get('action') if woke else None,
            'decision_changed': prior['action'] != woke['action'] if comparable else None,
            'risk_parameters_changed': old_risk != new_risk if comparable else None,
            'prior_position_intent': prior.get('hold_reason') if prior else None,
            'woke_position_intent': woke.get('hold_reason') if woke else None,
            'comparability': 'comparable' if comparable else 'no_decision_produced' if not woke else 'prior_was_not_a_decision',
            'scored_at': datetime.now(timezone.utc)}
        changed = 1 if dry_run else db.watch_triage_log.update_one(
            {'id': row['id'], 'outcome': None}, {'$set': {'outcome': outcome}}).modified_count
        scored += changed
        if changed:
            outcomes.append({'allocation_id': row['id'], 'ticker': row['ticker'], **outcome})
    return {'scored': scored, 'pending': skipped, 'outcomes': outcomes, 'dry_run': dry_run}
