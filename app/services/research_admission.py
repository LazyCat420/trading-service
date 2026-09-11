"""Shared admission for automatic discretionary research across trigger sources."""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from app.db import mongo_store
from app.services.parameter_store import get_param
from app.utils.tz import ensure_aware

PROTECTIVE = frozenset({'edge_case_stop_loss','edge_case_trailing_stop','edge_case_take_profit','edge_case_sell_limit'})

def nonwatch_starts_today(now=None):
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo('America/New_York')).replace(hour=0,minute=0,second=0,microsecond=0)
    rows = mongo_store.find_docs('pipeline_events',{
        'step':'cycle_trigger','created_at':{'$gte':day.astimezone(timezone.utc)},
        'data.source':{'$in':['order_trigger','schedule','research_governor']},
        'data.trigger_type':{'$nin':list(PROTECTIVE)},
    },projection={'cycle_id':1,'data':1},limit=1000)
    return len({r['cycle_id'] for r in rows if r.get('cycle_id')})

def admit(tickers, kwargs, now=None):
    """Called immediately before start; rejected triggers stay active.

    Watch requests were already reserved in watch_events, so their own
    reservation is included in used_today. No new cost is charged on denial.
    """
    now = now or datetime.now(timezone.utc)
    typ = kwargs.get('trigger_type') or ''
    automated = kwargs.get('watch_wake') or typ.startswith('edge_case_') or kwargs.get('dynamic_selection_mode') or kwargs.get('research_request')
    if not automated:
        return {'allowed':True,'reason':'explicit_request'}
    if typ in PROTECTIVE:
        return {'allowed':True,'reason':'protective_risk_review','budget_exempt':True}
    try:
        from app.services.watch_desk import _wakes_today, _human_stop_cooldown_active
        if _human_stop_cooldown_active(now):
            return {'allowed':False,'reason':'human_stop_cooldown'}
        used = _wakes_today() + nonwatch_starts_today(now)
        budget = int(get_param('MAX_WATCH_WAKES_PER_DAY'))
        # Watch enqueue has already reserved its slot. Other sources have not.
        over = used > budget if kwargs.get('watch_wake') else used >= budget
        detail = {'used_today':used,'daily_budget':budget,'scope':'automatic_discretionary_research'}
        if over:
            return {'allowed':False,'reason':'global_daily_budget_exhausted',**detail}
        minimum = float(get_param('WATCH_MIN_REANALYSIS_H'))
        maximum = int(get_param('WATCH_MAX_ANALYSES_PER_WEEK'))
        for ticker in tickers:
            rows = mongo_store.find_docs('analysis_results',{'ticker':ticker,
                'created_at':{'$gte':now-timedelta(days=7)}},sort=[('created_at',-1)],
                projection={'created_at':1,'cycle_id':1},limit=1000)
            # Count analyses, not duplicate persistence rows for the same cycle.
            count = len({r.get('cycle_id') or str(r.get('created_at')) for r in rows})
            last = ensure_aware(rows[0].get('created_at')) if rows else None
            if last and (now-last).total_seconds()<minimum*3600:
                return {'allowed':False,'reason':'min_reanalysis_interval','ticker':ticker,
                        'last_analysis_at':last,'minimum_hours':minimum,**detail}
            if count >= maximum:
                return {'allowed':False,'reason':'ticker_budget_exhausted','ticker':ticker,
                        'analyses_this_week':count,'weekly_limit':maximum,**detail}
        return {'allowed':True,'reason':'research_budget_and_cadence_available',**detail}
    except Exception as exc:
        return {'allowed':False,'reason':'admission_state_unavailable','error_type':type(exc).__name__}
