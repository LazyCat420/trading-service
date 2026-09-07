"""Serve queued questions inside an existing cycle's research budget.

No background model loop or implicit order path. One reserved candidate can
replace a selected ticker; up to three questions ride that ticker's full panel.
An unanswered question is deferred, never counted as completed research.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from app.services.research_queue_service import ResearchQueueService as Queue

logger = logging.getLogger(__name__)
RESEARCH_ROLES = frozenset({'desk_note', 'fundamental_report', 'quant_report', 'valuation_report'})


def reserve_candidate(tickers: list[str], budget: int) -> tuple[list[str], str | None]:
    """Reserve at most one existing slot, without claiming work prematurely."""
    if budget <= 0 or not tickers:
        return list(tickers), None
    selected = list(tickers[:budget])
    candidates = Queue.peek_worklist(1)
    if not candidates:
        return selected, None
    ticker = str(candidates[0].get('ticker') or '').upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', ticker) or ticker in selected:
        return selected, None
    selected[-1] = ticker
    return selected, ticker


def question_block(items: list[dict]) -> str:
    if not items:
        return ''
    requests = [{'item_id': item['id'],
                 'asked_at': item.get('created_at'),
                 'requested_by': item.get('source_agent'),
                 'question': (item.get('payload') or {}).get('question') or item.get('reason')}
                for item in items]
    return ('## CLAIMED RESEARCH QUESTIONS (answer these in this cycle)\n'
            + json.dumps(requests, ensure_ascii=False, default=str)
            + '\nResearch agents: return research_answers=[{"item_id":"exact id", "status":"answered|unresolved", '
              '"answer":"specific answer or why unresolved", "evidence":[{"source":"data_report or supplied *_context '
              'name or tool:<exact tool name>", "quote":"verbatim excerpt supporting your answer"}]}]. '
              'A source label without a quote is insufficient. Answer only from evidence you actually received; '
              'unresolved is valid and will be deferred. Respect asked_at: a current snapshot does not answer a historical '
              'date-specific question without evidence for that date. Do not repeat a completed answer from another research desk.\n')


def _normal(text) -> str:
    return ' '.join(str(text or '').split()).casefold()


def record_tool_receipts(artifact: dict, tool_transcript: list, *, delivered_text: str = "", metadata: dict | None = None) -> None:
    """Overwrite model-supplied verification flags with checks on real results."""
    receipts = []
    answers = artifact.get('research_answers')
    for answer in answers if isinstance(answers, list) else []:
        if not isinstance(answer, dict):
            continue
        evidence = answer.get('evidence')
        for ev in evidence if isinstance(evidence, list) else []:
            if not isinstance(ev, dict):
                continue
            source, quote = str(ev.get('source') or ''), ev.get('quote')
            if len(_normal(quote)) < 12:
                continue
            if not source.startswith('tool:'):
                source_text = (metadata or {}).get(source)
                if source_text and _normal(quote) in _normal(source_text) and _normal(quote) in _normal(delivered_text):
                    receipts.append({'source': source, 'quote': quote})
                continue
            for entry in tool_transcript or []:
                if source[5:] == entry.get('tool') and _normal(quote) in _normal(entry.get('result')):
                    receipts.append({'source': source, 'quote': quote})
                    break
    artifact['_research_tool_receipts'] = receipts


def _evidenced(answer: dict, artifact: dict, desk) -> bool:
    evidence = answer.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        return False
    for ev in evidence:
        if not isinstance(ev, dict) or len(_normal(ev.get('quote'))) < 12:
            return False
        source, quote = ev.get('source'), _normal(ev.get('quote'))
        receipts = artifact.get('_research_tool_receipts') or []
        if not any(isinstance(r, dict) and r.get('source') == source and _normal(r.get('quote')) == quote for r in receipts):
            return False
    return True


def finish_questions(desk) -> dict:
    summary = {'answered': 0, 'deferred': 0, 'delivery_pending': 0}
    items = desk.cycle_metadata.get('research_questions') or []
    answers = _verified_answers(desk)
    for item in items:
        try:
            answer = answers.get(item['id'])
            done = Queue.finish_claim(item, answer=answer)
            if done:
                summary['answered' if answer else 'deferred'] += 1
        except Exception as exc:
            # An answer_ready outbox row survives this failure for later delivery.
            summary['delivery_pending'] += 1
            logger.error('[ResearchWork] %s: question delivery failed for %s: %s', desk.ticker, item['id'], exc)
    return summary


def completed_answer_block(desk, max_chars: int = 3500) -> str:
    """Pass verified answers to later desks in the same research panel."""
    return _answer_block(list(_verified_answers(desk).values()),
                         'VERIFIED RESEARCH ANSWERS FROM THIS PANEL', max_chars)


def _verified_answers(desk) -> dict[str, dict]:
    questions = {item['id']: item for item in desk.cycle_metadata.get('research_questions') or []}
    found = {}
    for name in ('fundamental_report', 'quant_report', 'valuation_report', 'desk_note'):
        artifact = getattr(desk, name, None) or {}
        if artifact.get('_degraded'):
            continue
        answers = artifact.get('research_answers')
        for answer in answers if isinstance(answers, list) else []:
            if not isinstance(answer, dict):
                continue
            item_id = answer.get('item_id')
            if (not isinstance(item_id, str) or item_id not in questions or item_id in found or answer.get('status') != 'answered'
                    or len(_normal(answer.get('answer'))) < 20 or not _evidenced(answer, artifact, desk)):
                continue
            item = questions[item_id]
            found[item_id] = {**answer, 'asked_at': item.get('created_at'),
                'question': (item.get('payload') or {}).get('question') or item.get('reason'),
                'artifact_ref': f'{desk.cycle_id}:{desk.ticker}:{name}'}
    return found


def prior_answer_context(ticker: str, max_chars: int = 3500) -> str:
    """Read bounded historical answers with dates and source references."""
    from app.db import mongo_store
    from app.services.cycle_scope import exclude_synthetic
    try:
        rows = mongo_store.find_docs('dossier_question_log', {
            'ticker': ticker.upper(), 'status': 'answered',
            **exclude_synthetic('resolved_cycle'),
            'resolved_at': {'$gte': datetime.now(timezone.utc) - timedelta(days=30)}},
            projection={'_id': 0, 'question': 1, 'answer': 1, 'evidence': 1,
                        'evidence_ref': 1, 'resolved_at': 1, 'question_asked_at': 1},
            sort=[('resolved_at', -1)], limit=3)
        return _answer_block(rows, 'HISTORICAL RESEARCH ANSWERS (reverify time-sensitive facts)', max_chars)
    except Exception as exc:
        logger.warning('[ResearchWork] prior answers unavailable for %s: %s', ticker, exc)
        return ''


def _answer_block(rows: list[dict], title: str, max_chars: int) -> str:
    if not rows:
        return ''
    lines = [f'## {title}']
    used, omitted = len(lines[0]), 0
    for row in rows:
        text = json.dumps(row, default=str, ensure_ascii=False)
        if used + len(text) + 1 > max_chars - 100:
            omitted += 1
            continue
        lines.append(text)
        used += len(text) + 1
    if omitted:
        lines.append(f'OMITTED: {omitted} complete answer record(s); they are unknown in this view.')
    return '\n'.join(lines)
