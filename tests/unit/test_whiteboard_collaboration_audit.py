"""Real whiteboard methods with an isolated document store; never a live DB."""
import asyncio
from copy import deepcopy
import json

import pytest

from app.agents.whiteboard import Whiteboard


@pytest.fixture
def board(monkeypatch):
    import app.agents.whiteboard as module
    rows = {'whiteboard_entries': [], 'whiteboard_annotations': []}
    def matches(doc, query):
        return all(doc.get(k) == v for k, v in query.items())
    def find_docs(table, query, **kwargs):
        return [deepcopy(d) for d in rows[table] if matches(d, query)]
    def find_row(table, query, columns, **kwargs):
        found = find_docs(table, query)
        return tuple(found[0].get(k) for k in columns) if found else None
    def insert_docs(table, docs):
        rows[table].extend(deepcopy(docs))
    def update_docs(table, query, update):
        for row in rows[table]:
            if matches(row, query):
                row.update(deepcopy(update['$set']))
    monkeypatch.setattr(module.mongo_store, 'find_docs', find_docs)
    monkeypatch.setattr(module.mongo_query, 'find_row', find_row)
    monkeypatch.setattr(module.mongo_store, 'insert_docs', insert_docs)
    monkeypatch.setattr(module.mongo_store, 'update_docs', update_docs)
    return Whiteboard(), rows


@pytest.mark.asyncio
async def test_cancellation_during_notification_leaves_committed_write_and_retry_creates_version(board):
    wb, rows = board
    entered = asyncio.Event()
    async def busy_subscriber(event):
        entered.set()
        await asyncio.Event().wait()
    wb.subscribe(busy_subscriber, ticker='TEST')
    task = asyncio.create_task(wb.write_section('TEST', 'cycle-v3-audit-a', 'risk_flags', {'risk': 'fixture'}, 'analyst'))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert len(rows['whiteboard_entries']) == 1  # committed BEFORE notification
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await wb.get_section('TEST', 'cycle-v3-audit-a', 'risk_flags'))['version'] == 1
    wb.unsubscribe(busy_subscriber)
    await wb.write_section('TEST', 'cycle-v3-audit-a', 'risk_flags', {'risk': 'fixture'}, 'analyst')
    assert (await wb.get_section('TEST', 'cycle-v3-audit-a', 'risk_flags'))['version'] == 2


@pytest.mark.asyncio
async def test_annotation_cannot_target_another_cycle(board, monkeypatch):
    wb, rows = board
    import app.tools.whiteboard_tools as wt
    from app.tools.tool_context import tool_context
    monkeypatch.setattr(wt, 'whiteboard', wb)
    entry = await wb.write_section('TEST', 'cycle-v3-audit-old', 'risk_flags', {}, 'analyst')
    with tool_context(agent_name='v3_quant_analyst', cycle_id='cycle-v3-audit-new', ticker='TEST'):
        result = json.loads(await wt.whiteboard_annotate(entry, 'wrong cycle'))
    assert result['status'] == 'error'
    assert rows['whiteboard_annotations'] == []


@pytest.mark.asyncio
async def test_annotations_preserve_revision_provenance_in_reads_and_prompts(board):
    wb, _ = board
    old = await wb.write_section('TEST', 'cycle-v3-audit-a', 'risk_flags', {'leverage': 4}, 'analyst')
    await wb.annotate(old, 'quant', 'leverage disputed')
    current = await wb.write_section('TEST', 'cycle-v3-audit-a', 'risk_flags', {'leverage': 2}, 'analyst')
    result = await wb.get_section('TEST', 'cycle-v3-audit-a', 'risk_flags')
    assert result['id'] == current
    assert result['annotations'][0]['entry_id'] == old
    assert result['annotations'][0]['applies_to_current_version'] is False
    summary = await wb.summarize('TEST', 'cycle-v3-audit-a', for_agent_prompt=True)
    assert old in summary
    assert 'earlier version' in summary


@pytest.mark.asyncio
async def test_empty_section_does_not_claim_its_author_has_not_run(board, monkeypatch):
    wb, _ = board
    import app.tools.whiteboard_tools as wt
    monkeypatch.setattr(wt, 'whiteboard', wb)
    result = json.loads(await wt.whiteboard_read('TEST', 'risk_flags'))
    assert result['status'] == 'empty'
    assert 'has not run' not in result['message']
    assert 're-reading will not change' not in result['message']


@pytest.mark.asyncio
async def test_annotation_scope_accepts_current_entry_but_rejects_another_ticker(board, monkeypatch):
    wb, rows = board
    import app.tools.whiteboard_tools as wt
    from app.tools.tool_context import tool_context
    monkeypatch.setattr(wt, 'whiteboard', wb)
    own = await wb.write_section('TEST', 'cycle-v3-audit-a', 'risk_flags', {}, 'analyst')
    other = await wb.write_section('OTHER', 'cycle-v3-audit-a', 'risk_flags', {}, 'analyst')
    with tool_context(agent_name='v3_quant_analyst', cycle_id='cycle-v3-audit-a', ticker='TEST'):
        assert json.loads(await wt.whiteboard_annotate(other, 'wrong ticker'))['status'] == 'error'
        assert json.loads(await wt.whiteboard_annotate(own, 'current comment'))['status'] == 'success'
    assert len(rows['whiteboard_annotations']) == 1
    result = await wb.get_section('TEST', 'cycle-v3-audit-a', 'risk_flags')
    assert result['annotations'][0]['applies_to_current_version'] is True
    assert 'current version' in await wb.summarize('TEST', 'cycle-v3-audit-a')


@pytest.mark.asyncio
async def test_legacy_annotation_without_entry_id_is_explicitly_unknown(board):
    wb, rows = board
    await wb.write_section('TEST', 'cycle-v3-audit-a', 'risk_flags', {}, 'analyst')
    rows['whiteboard_annotations'].append({'ticker': 'TEST', 'cycle_id': 'cycle-v3-audit-a',
                                         'section': 'risk_flags', 'author_agent': 'legacy', 'note': 'legacy note'})
    result = await wb.get_section('TEST', 'cycle-v3-audit-a', 'risk_flags')
    assert result['annotations'][0]['applies_to_current_version'] is None
    assert 'version unknown' in await wb.summarize('TEST', 'cycle-v3-audit-a')
