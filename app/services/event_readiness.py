"""Cheap confirmation of reported earnings before starting post-event research."""
from datetime import datetime, timezone
import math
import re
import httpx
from app.config import settings


def reported_actual(events, ticker, event_date):
    for event in events or []:
        if event.get('symbol') != ticker or event.get('date') != event_date:
            continue
        actuals = {key:event[key] for key in ('epsActual','revenueActual')
                   if isinstance(event.get(key),(int,float)) and not isinstance(event.get(key),bool)
                   and math.isfinite(event[key])}
        if actuals:
            return {'ticker':ticker,'date':event_date,'source':'finnhub_earnings_calendar',
                    'evidence_kind':'provider_reported_actual',**actuals}
    return None


async def schedule_readiness(tickers, reason_codes, review_intent):
    dates = [match.group(1) for code in reason_codes or []
             if (match := re.fullmatch(r'earnings_(\d{4}-\d{2}-\d{2})',str(code)))]
    if review_intent != 'event_followup' or not dates:
        return {'ready':True,'required':False,'reason':'not_an_earnings_followup'}
    checked = datetime.now(timezone.utc).isoformat()
    if not settings.FINNHUB_API_KEY:
        return {'ready':False,'required':True,'reason':'release_verification_unavailable','checked_at':checked}
    releases = []
    try:
        async with httpx.AsyncClient(timeout=15,trust_env=False) as client:
            for ticker in tickers:
                for day in dates:
                    response = await client.get('https://finnhub.io/api/v1/calendar/earnings',
                        params={'symbol':ticker,'from':day,'to':day,'token':settings.FINNHUB_API_KEY})
                    response.raise_for_status()
                    release = reported_actual(response.json().get('earningsCalendar'),ticker,day)
                    if not release:
                        return {'ready':False,'required':True,'reason':'earnings_actuals_not_available',
                                'ticker':ticker,'event_date':day,'checked_at':checked}
                    releases.append(release)
    except Exception as exc:
        return {'ready':False,'required':True,'reason':'release_verification_unavailable',
                'error_type':type(exc).__name__,'checked_at':checked}
    return {'ready':bool(releases),'required':True,'reason':'provider_reported_actuals_available',
            'releases':releases,'checked_at':checked}
