"""Trading-owned, append-only data lineage. No hidden model reasoning is retained.

Content-addressed bounded snapshots preserve inspectable data at each boundary.
W3C-sized trace/span IDs permit joining exports to OpenTelemetry collectors.
"""
from __future__ import annotations
import hashlib
import json
import logging
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from app.db import mongo_store

log = logging.getLogger(__name__)
_parent = ContextVar('data_trace_parent', default=None)
MAX_BYTES = 256 * 1024
_PRIVATE = re.compile(r'<(think|thought_process|analysis|reasoning)\b[^>]*>[\s\S]*?(?:</\1>|$)', re.I)
_SECRET = re.compile(r'authorization|api[_-]?key|password|secret|_lazy_trading_context', re.I)

def sanitize(value):
    if isinstance(value, dict):
        return {str(k): ('[redacted]' if _SECRET.search(str(k)) else sanitize(v))
                for k,v in value.items() if str(k) not in ('reasoning_content', 'thinking')}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return _PRIVATE.sub('[private reasoning omitted]', value)
    return value

def snapshot(value):
    raw = json.dumps(sanitize(value), sort_keys=True, ensure_ascii=False, default=str).encode()
    digest = hashlib.sha256(raw).hexdigest()
    clipped = len(raw) > MAX_BYTES
    return {'hash':digest, 'bytes':len(raw), 'truncated':clipped,
            'encoding':'utf8-json-prefix' if clipped else 'json',
            'content':raw[:MAX_BYTES].decode('utf-8', errors='ignore')}

def record(cycle_id, ticker, agent, stage, *, data=None, parent_span_id=None, **attributes):
    if not cycle_id:
        return None
    span = root_span(cycle_id) if stage == 'cycle.admission' else uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc)
    event = {'id':uuid.uuid4().hex, 'trace_id':hashlib.sha256(cycle_id.encode()).hexdigest()[:32],
             'span_id':span, 'parent_span_id':parent_span_id or _parent.get(),
             'cycle_id':cycle_id, 'ticker':ticker, 'agent':agent, 'stage':stage,
             'created_at':now, 'attributes':sanitize(attributes), 'schema_version':1}
    try:
        if data is not None:
            blob = snapshot(data)
            event['snapshot'] = {k:v for k,v in blob.items() if k != 'content'}
            mongo_store.update_docs('pipeline_trace_blobs', {'_id':blob['hash']},
                {'$setOnInsert':{**blob, 'created_at':now},
                 '$max':{'last_referenced_at':now}}, upsert=True)
        mongo_store.insert_docs('pipeline_trace_events', [event])
        return span
    except Exception as exc:
        log.warning('Data trace write failed at %s: %s', stage, type(exc).__name__)
        return None

@contextmanager
def scope(cycle_id, ticker, agent, **attributes):
    span = record(cycle_id,ticker,agent,'agent.start',parent_span_id=root_span(cycle_id),**attributes)
    token = _parent.set(span)
    identity_token = _identity.set({'cycle_id':cycle_id,'ticker':ticker,'agent':agent})
    try:
        yield
    except BaseException as exc:
        record(cycle_id,ticker,agent,'agent.end',status='error',error_type=type(exc).__name__)
        raise
    else:
        record(cycle_id,ticker,agent,'agent.end',status='returned')
    finally:
        _parent.reset(token)
        _identity.reset(identity_token)


def current_span():
    return _parent.get()


def root_span(cycle_id):
    return hashlib.sha256((cycle_id + ':root').encode()).hexdigest()[:16]

_identity = ContextVar('data_trace_identity', default=None)
_SOURCE_COLLECTIONS = frozenset({
    'fundamentals','news_articles','price_history','technicals','economic_calendar',
    'earnings_calendar','sec_filings','company_profiles','options_data','options_chain',
    'institutional_holders','insider_transactions','analyst_ratings','analyst_estimates',
    'income_statements','balance_sheets','cash_flow_statements','short_interest',
    'reddit_posts','youtube_transcripts','congress_trades','fund_holdings',
})

def trace_cycle(fn):
    """Bind only this explicitly started cycle, never the global active singleton."""
    from functools import wraps
    @wraps(fn)
    async def wrapped(cls, cycle_id, *args, **kwargs):
        token = _identity.set({'cycle_id':cycle_id,'ticker':'','agent':'collector'})
        parent = _parent.set(root_span(cycle_id))
        try:
            return await fn(cls,cycle_id,*args,**kwargs)
        finally:
            _parent.reset(parent)
            _identity.reset(token)
    return wrapped

def observe_store(collection, operation, query, documents):
    identity = _identity.get()
    if not identity or collection not in _SOURCE_COLLECTIONS:
        return
    record(identity['cycle_id'],identity['ticker'],identity['agent'],f'source.{operation}',
           data={'collection':collection,'query':query,'documents':documents},
           collection=collection, rows=len(documents) if isinstance(documents,list) else 1)


def otlp_export(events):
    """OTLP/HTTP JSON ExportTraceServiceRequest (IDs hex; int64 timestamps strings).

    Point observations have zero duration. Only start/end pairs give measured
    spans a duration; no fabricated timing is assigned to parsing or tools.
    Snapshot content stays in Trading; the export carries hashes and references.
    """
    def nanos(value):
        if isinstance(value,str):
            value=datetime.fromisoformat(value.replace('Z','+00:00'))
        if value.tzinfo is None:
            value=value.replace(tzinfo=timezone.utc)
        delta=value-datetime(1970,1,1,tzinfo=timezone.utc)
        return (delta.days*86400+delta.seconds)*1_000_000_000+delta.microseconds*1000
    ends={}
    ids={e['span_id'] for e in events}
    for event in events:
        if event['stage'] in ('agent.end','cycle.end') and event.get('parent_span_id'):
            ends[event['parent_span_id']]=nanos(event['created_at'])
    spans=[]
    for event in events:
        start=nanos(event['created_at'])
        attrs={'trading.cycle_id':event['cycle_id'],'trading.ticker':event.get('ticker') or '',
               'trading.agent':event.get('agent') or '',
               'trading.attributes':json.dumps(event.get('attributes') or {},default=str),
               'trading.snapshot':json.dumps(event.get('snapshot') or {},default=str)}
        span={'traceId':event['trace_id'],'spanId':event['span_id'], 'name':event['stage'],'kind':1,
              'startTimeUnixNano':str(start),'endTimeUnixNano':str(max(start,ends.get(event['span_id'],start))),
              'attributes':[{'key':k,'value':{'stringValue':v}} for k,v in attrs.items()]}
        if event.get('parent_span_id') in ids:
            span['parentSpanId']=event['parent_span_id']
        spans.append(span)
    return {'resourceSpans':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':'trading-pipeline'}}]},
                             'scopeSpans':[{'scope':{'name':'trading.data-lineage','version':'1'},'spans':spans}]}]}
