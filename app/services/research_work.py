"""Serve queued questions inside an existing cycle's research budget.

No background model loop or implicit order path. One reserved candidate can
replace a selected ticker; up to three questions ride that ticker's full panel.
An unanswered question is deferred, never counted as completed research.
"""
from __future__ import annotations

import json
import logging
import re

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
                 'question': (item.get('payload') or {}).get('question') or item.get('reason')}
                for item in items]
    return ('## CLAIMED RESEARCH QUESTIONS (answer these in this cycle)\n'
            + json.dumps(requests, ensure_ascii=False, default=str)
            + '\nResearch agents: return research_answers=[{"item_id":"exact id", "status":"answered|unresolved", '
              '"answer":"specific answer or why unresolved", "evidence":[{"source":"data_report or supplied *_context '
              'name or tool:<exact tool name>", "quote":"verbatim excerpt supporting your answer"}]}]. '
              'A source label without a quote is insufficient. Answer only from evidence you actually received; '
              'unresolved is valid and will be deferred. Do not repeat a completed answer from another research desk.\n')


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
    for item in items:
        answer = None
        for name in ('fundamental_report', 'quant_report', 'valuation_report', 'desk_note'):
            artifact = getattr(desk, name, None) or {}
            if artifact.get('_degraded'):
                continue
            answers = artifact.get('research_answers')
            for row in answers if isinstance(answers, list) else []:
                if (isinstance(row, dict) and row.get('item_id') == item['id']
                        and row.get('status') == 'answered' and len(_normal(row.get('answer'))) >= 20
                        and _evidenced(row, artifact, desk)):
                    answer = {**row, 'artifact_ref': f'{desk.cycle_id}:{desk.ticker}:{name}'}
                    break
            if answer:
                break
        try:
            done = Queue.finish_claim(item, answer=answer)
            if done:
                summary['answered' if answer else 'deferred'] += 1
        except Exception as exc:
            # An answer_ready outbox row survives this failure for later delivery.
            summary['delivery_pending'] += 1
            logger.error('[ResearchWork] %s: question delivery failed for %s: %s', desk.ticker, item['id'], exc)
    return summary
