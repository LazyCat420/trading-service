"""Real Mongo contract checks, on an explicitly enabled disposable test DB.

No price, LLM, order or live-cycle calls. The store, queue CAS operations,
watch sweep, scorer and question-to-answer persistence are real.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.real_mongo


@pytest.fixture
def isolated_db(real_mongo, request):
    assert real_mongo.name.startswith('trading_bot_pytest')
    yield real_mongo
    # Preserve the small disposable DB before the fixture removes it; the
    # snapshot also proves the final mutations, not merely a mocked return.
    dest = os.environ.get('HARNESS_TEST_EVIDENCE_DIR')
    if dest:
        path = Path(dest)
        path.mkdir(parents=True, exist_ok=True)
        docs = {name: list(real_mongo[name].find({}, {'_id': 0}))
                for name in real_mongo.list_collection_names()}
        (path / (request.node.name + '.json')).write_text(json.dumps(
            {'database': real_mongo.name, 'counts': {k: len(v) for k,v in docs.items()}, 'documents': docs},
            default=str, indent=2))


@pytest.mark.asyncio
@pytest.mark.parametrize('headline,relevant', [
    ('Citigroup delays Fed rate cut forecast to June 2027', False),
    ('Citigroup raises guidance as net interest margin recovers above 3.5 percent', True),
])
async def test_saved_resolution_condition_reaches_real_watch_sweep(isolated_db, monkeypatch, headline, relevant):
    from app.services import watch_desk as wd, watch_allocator as wa
    from app.services.parameter_store import get_param
    from app.v3.shared_desk import SharedDesk
    from app.v3.orchestrator import _build_v1_compatible_result
    now = datetime.now(timezone.utc)
    desk = SharedDesk(ticker='C', cycle_id='isolated-watch')
    desk.cycle_metadata = {'held': False, 'decision_contract_version': 1}
    desk.final_decision = {'action': 'HOLD', 'confidence': 72, 'reasoning': 'Need the actual net interest margin.',
        'entry_mode': 'watch_only', 'trigger_purpose': 'none',
        'resolution_condition': {'open_question': 'Will net interest margin recover?',
                                 'resolving_fact': 'Net interest margin exceeds 3.5 percent'}}
    monkeypatch.setattr('app.collectors.fund_scanner.get_institutional_signal', lambda _: {})
    result = _build_v1_compatible_result(desk)
    wd.derive_baseline_watch('C', result, {'price': 60}, desk.cycle_id)
    stored = isolated_db.ticker_watches.find_one({'ticker': 'C', 'is_active': True})
    assert stored['schema_version'] == 1
    assert stored['decision_context']['resolution_condition']['open_question'] == 'Will net interest margin recover?'
    # Leave only the news candidate; genuine category/age evaluation still runs.
    isolated_db.ticker_watches.update_one({'id': stored['id']}, {'$set': {
        'triggers': json.dumps([{'type': 'news', 'categories': ['guidance']}]),
        'last_fired_at': now - timedelta(days=2)}})
    monkeypatch.setattr(wd, '_gather_context', AsyncMock(return_value={'ticker': 'C', 'news': [(headline, now)], 'price': None}))
    monkeypatch.setattr(wd, '_title_names_ticker', lambda *args: True)
    monkeypatch.setattr(wd, '_human_stop_cooldown_active', lambda: False)
    enqueue = AsyncMock(return_value='should-not-wake')
    monkeypatch.setattr(wd, '_enqueue_wake', enqueue)
    monkeypatch.setattr(wa, '_position', lambda _: (0, 0, None))
    monkeypatch.setattr(wa, 'catalyst_calendar', lambda *args: {})
    monkeypatch.setattr('app.services.parameter_store.get_param', lambda k: 2 if k == 'WATCH_TRIAGE_MODE' else get_param(k))
    summary = await wd.evaluate_watches()
    log = isolated_db.watch_triage_log.find_one({'watch_id': stored['id']})
    assert log is not None
    assert log['schema_version'] == 1 and log['has_resolution_condition']
    assert log['reject_reason'] == (None if relevant else 'evidence_off_thesis')
    assert bool(log['fired']) == relevant
    assert summary['fired'] == int(relevant)
    assert enqueue.await_count == int(relevant)


def _enqueue_question(ticker='TEST'):
    from app.services.research_queue_service import ResearchQueueService as Queue
    from app.schemas.dossier_schemas import QueueType
    from app.services import question_ledger
    question = 'Does the latest filing report operating margin above ten percent?'
    rec = question_ledger.record_asked(ticker, 'cycle-prior', [question], 'quant')[0]
    item_id = Queue.enqueue_item(ticker, QueueType.DEEP_DIVE_QUEUE, reason=question,
        payload={'question': question, 'question_hash': rec['question_hash'], 'cycle_id': 'cycle-prior'})
    return item_id, rec


def test_competing_workers_claim_question_once(isolated_db):
    from app.services.research_queue_service import ResearchQueueService as Queue
    item_id, _ = _enqueue_question()
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda i: Queue.claim_for_ticker('TEST', f'cycle-owner-{i}', limit=1), range(6)))
    won = [item for items in claims for item in items]
    assert len(won) == 1
    stored = isolated_db.v3_research_queues.find_one({'id': item_id})
    assert stored['status'] == 'processing' and stored['attempts'] == 1
    assert stored['lease_token'] == won[0]['lease_token']
    assert stored['owner_cycle_id'] == won[0]['owner_cycle_id']


def test_answer_requires_evidence_then_persists_queue_and_ledger(isolated_db):
    from app.services.research_queue_service import ResearchQueueService as Queue
    from app.services.research_work import finish_questions, record_tool_receipts
    from app.v3.shared_desk import SharedDesk
    item_id, rec = _enqueue_question()
    claimed = Queue.claim_for_ticker('TEST', 'cycle-answer', limit=1)
    desk = SharedDesk(ticker='TEST', cycle_id='cycle-answer')
    desk.cycle_metadata = {'research_questions': claimed,
                          'data_report': 'The latest filing reports operating margin of 12.4 percent.'}
    desk.fundamental_report = {'summary': 'Margin verified from the filing.',
        'research_answers': [{'item_id': item_id, 'status': 'answered',
            'answer': 'Yes. Reported operating margin is 12.4 percent, above ten percent.',
            'evidence': [{'source': 'data_report', 'quote': 'operating margin of 12.4 percent'}]}]}
    record_tool_receipts(desk.fundamental_report, [], delivered_text=desk.cycle_metadata['data_report'], metadata=desk.cycle_metadata)
    summary = finish_questions(desk)
    assert summary['answered'] == 1
    item = isolated_db.v3_research_queues.find_one({'id': item_id})
    ledger = isolated_db.dossier_question_log.find_one({'ticker': 'TEST', 'question_hash': rec['question_hash']})
    assert item['status'] == 'completed'
    assert item['answer']['artifact_ref'] == 'cycle-answer:TEST:fundamental_report'
    assert ledger['status'] == 'answered'
    assert ledger['evidence_ref'] == item['answer']['artifact_ref']
    assert ledger['answer'] == item['answer']['answer']
    assert finish_questions(desk)['answered'] == 0  # idempotent after completion


def test_missing_answer_is_deferred_not_counted_as_completed(isolated_db):
    from app.services.research_queue_service import ResearchQueueService as Queue
    from app.services.research_work import finish_questions
    from app.v3.shared_desk import SharedDesk
    item_id, rec = _enqueue_question()
    desk = SharedDesk(ticker='TEST', cycle_id='cycle-unanswered')
    desk.cycle_metadata = {'research_questions': Queue.claim_for_ticker('TEST', desk.cycle_id, limit=1)}
    desk.fundamental_report = {'summary': 'The requested filing is not available.'}
    summary = finish_questions(desk)
    stored = isolated_db.v3_research_queues.find_one({'id': item_id})
    assert summary['answered'] == 0 and summary['deferred'] == 1
    assert stored['status'] == 'pending'
    assert stored['next_attempt_at'] > datetime.now(timezone.utc).replace(tzinfo=None)
    assert stored['last_error']
    assert Queue.claim_for_ticker('TEST', 'cycle-too-soon', limit=1) == []
    assert isolated_db.dossier_question_log.find_one({'question_hash': rec['question_hash']})['status'] != 'answered'


def test_expired_lease_cannot_overwrite_new_owner(isolated_db):
    from app.services.research_queue_service import ResearchQueueService as Queue
    item_id, _ = _enqueue_question()
    old = Queue.claim_for_ticker('TEST', 'cycle-old', limit=1)[0]
    isolated_db.v3_research_queues.update_one({'id': item_id}, {'$set': {
        'updated_at': datetime.now(timezone.utc) - timedelta(hours=2)}})
    Queue.reclaim_stale()
    new = Queue.claim_for_ticker('TEST', 'cycle-new', limit=1)[0]
    assert old['lease_token'] != new['lease_token']
    assert not Queue.finish_claim(old, answer={'answer': 'stale worker answer'})
    assert isolated_db.v3_research_queues.find_one({'id': item_id})['owner_cycle_id'] == 'cycle-new'


def test_allocator_scores_only_terminal_cycle_and_is_idempotent(isolated_db):
    from app.services.watch_outcomes import score_completed_allocations
    now = datetime.now(timezone.utc)
    isolated_db.watch_triage_log.insert_one({'id': 'alloc', 'created_at': now, 'ticker': 'TEST',
        'fired': True, 'outcome': None, 'cycle_id': 'wd-test'})
    isolated_db.v3_system_commands.insert_one({'id': 'wd-test', 'result': {'cycle_id': 'cycle-test'}})
    isolated_db.cycle_run_summaries.insert_one({'cycle_id': 'cycle-test', 'status': 'running'})
    assert score_completed_allocations(db=isolated_db)['scored'] == 0
    isolated_db.analysis_results.insert_one({'ticker': 'TEST', 'cycle_id': 'cycle-prior', 'created_at': now - timedelta(days=1),
        'result_json': {'action': 'HOLD', 'hold_reason': 'KEEP'}})
    isolated_db.analysis_results.insert_one({'ticker': 'TEST', 'cycle_id': 'cycle-test', 'created_at': now,
        'result_json': {'action': 'HOLD', 'hold_reason': 'KEEP', 'estimate': {'stop_loss': 95}}})
    isolated_db.cycle_run_summaries.update_one({'cycle_id': 'cycle-test'}, {'$set': {'status': 'done'}})
    assert score_completed_allocations(db=isolated_db)['scored'] == 1
    outcome = isolated_db.watch_triage_log.find_one({'id': 'alloc'})['outcome']
    assert outcome['decision_changed'] is False
    assert outcome['risk_parameters_changed'] is True
    assert outcome['cycle_id'] == 'cycle-test'
    assert score_completed_allocations(db=isolated_db)['scored'] == 0


def test_context_manifest_is_read_from_the_persisted_desk(isolated_db):
    from app.v3.shared_desk import SharedDesk
    from app.v3.desk_persistence import save_desk
    from app.autoresearch.core import _collect_context_delivery
    desk = SharedDesk(ticker='TEST', cycle_id='cycle-delivery-receipt')
    receipt = {'agent': 'v3_debate_judge', 'defense_delivered': True,
               'defense_records_omitted': False, 'user_sha256': 'fixture-hash'}
    desk.cycle_metadata = {'context_delivery': [receipt]}
    save_desk(desk)
    measured = _collect_context_delivery(desk.cycle_id)
    assert measured == {'availability': 'measured', 'receipts': [{'ticker': 'TEST', **receipt}]}
    assert _collect_context_delivery('absent-cycle')['availability'] == 'unverified'


def test_ledger_outage_keeps_answer_ready_and_retries_once(isolated_db, monkeypatch):
    from app.services.research_queue_service import ResearchQueueService as Queue
    from app.db import mongo_store
    item_id, rec = _enqueue_question()
    item = Queue.claim_for_ticker('TEST', 'cycle-outbox', limit=1)[0]
    answer = {'answer': 'Operating margin is above ten percent.',
              'artifact_ref': 'cycle-outbox:TEST:fundamental_report',
              'evidence': [{'source': 'data_report', 'quote': 'operating margin of 12.4 percent'}]}
    real_upsert = mongo_store.upsert_doc
    def outage(collection, *args, **kwargs):
        if collection == 'dossier_question_log':
            raise RuntimeError('simulated ledger outage')
        return real_upsert(collection, *args, **kwargs)
    monkeypatch.setattr(mongo_store, 'upsert_doc', outage)
    with pytest.raises(RuntimeError, match='simulated ledger outage'):
        Queue.finish_claim(item, answer=answer)
    assert isolated_db.v3_research_queues.find_one({'id': item_id})['status'] == 'answer_ready'
    monkeypatch.setattr(mongo_store, 'upsert_doc', real_upsert)
    assert Queue.deliver_ready_answers() == 1
    assert Queue.deliver_ready_answers() == 0
    assert isolated_db.dossier_question_log.count_documents({'ticker': 'TEST', 'question_hash': rec['question_hash']}) == 1
    assert isolated_db.v3_research_queues.find_one({'id': item_id})['status'] == 'completed'
