"""Tool-score provenance: execution time and grading time are different clocks."""
from datetime import datetime, timedelta, timezone
from app.db import mongo_store
from app.db.collections import collection_for


def trace_quality_window(*, now=None, days=7, cycle_id=None):
    now = now or datetime.now(timezone.utc)
    query = {'created_at':{'$gte':now-timedelta(days=days), '$lte':now}}
    if cycle_id:
        query = {'run_id':cycle_id}
    rows = mongo_store.aggregate('agent_traces', [
        {'$match':query},
        {'$lookup':{'from':collection_for('eval_scores'), 'let':{'trace':'$id'},
            'pipeline':[{'$match':{'$expr':{'$eq':['$run_id','$$trace']}}},
                        {'$sort':{'created_at':-1}}, {'$limit':1},
                        {'$project':{'final_score':1,'created_at':1}}], 'as':'grades'}},
        {'$set':{'grade':{'$arrayElemAt':['$grades',0]}}},
        {'$group':{
            '_id':{'model':{'$ifNull':['$model_name','unconfirmed']},
                   'provider':{'$ifNull':['$endpoint_name','unknown']},
                   'attribution':{'$ifNull':['$model_attribution','legacy_unverified']}},
            'trace_count':{'$sum':1},
            'scored_count':{'$sum':{'$cond':[{'$isNumber':'$grade.final_score'},1,0]}},
            'mean_score':{'$avg':'$grade.final_score'},
            'first_trace_at':{'$min':'$created_at'}, 'last_trace_at':{'$max':'$created_at'},
            'last_scored_at':{'$max':'$grade.created_at'},
            'max_grading_delay_ms':{'$max':{'$subtract':['$grade.created_at','$created_at']}},
        }},
    ])
    count=sum(r['trace_count'] for r in rows)
    scored=sum(r['scored_count'] for r in rows)
    def iso(value):
        if not isinstance(value, datetime):
            return None
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    models=[{**r['_id'], **{k:r[k] for k in ('trace_count','scored_count','mean_score')},
             **{k:iso(r.get(k)) for k in ('first_trace_at','last_trace_at','last_scored_at')},
             'max_grading_delay_hours':(round(max(0,r['max_grading_delay_ms'])/3600000,2)
                                        if r.get('max_grading_delay_ms') is not None else None)}
            for r in rows]
    return {'scope':'cycle' if cycle_id else 'rolling_execution_window', 'cycle_id':cycle_id,
            'window_days':None if cycle_id else days, 'time_basis':'agent_traces.created_at',
            'trace_count':count, 'scored_count':scored, 'pending_count':count-scored,
            'mean_score':round(sum((r['mean_score'] or 0)*r['scored_count'] for r in rows)/scored,2) if scored else None,
            'models':models,
            'note':'Tool-call diagnostics; not complete decision quality. Model labels without response metadata are unverified.'}
