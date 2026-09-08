"""Preserve observed quote time separately from a daily bar's calendar date."""
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

SOURCE = 'yfinance.regularMarketTime'


def quote_observation(ticker, bar_date, close, metadata, *, now=None):
    """Accept provider quote time only when identity, session and price agree.

    Never infer market close or use fetch time as price time. Daily dates alone
    cannot establish when a quote was observed, especially over holiday weekends.
    """
    if not isinstance(metadata, dict):
        return None
    now = now or datetime.now(timezone.utc)
    try:
        if str(metadata.get('symbol') or '').upper() != ticker.upper():
            return None
        stamp = float(metadata['regularMarketTime'])
        quote = float(metadata['regularMarketPrice'])
        price = float(close)
        if not all(math.isfinite(v) and v > 0 for v in (stamp, quote, price)):
            return None
        observed = datetime.fromtimestamp(stamp, timezone.utc)
        local = observed.astimezone(ZoneInfo(metadata['exchangeTimezoneName']))
        if observed > now or local.date() != bar_date:
            return None
        if not math.isclose(quote, price, rel_tol=1e-6, abs_tol=1e-6):
            return None
        return {'price_as_of': observed, 'price_as_of_source': SOURCE,
                'observed_quote_price': quote}
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return None


def verified_price_time(price, observed, source, quote_price, *, now):
    """Use only matched observation metadata; malformed/future values grant nothing."""
    if source != SOURCE:
        return None
    try:
        if isinstance(observed, str):
            observed = datetime.fromisoformat(observed.replace('Z', '+00:00'))
        if not isinstance(observed, datetime):
            return None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)  # BSON stores UTC
        p, q = float(price), float(quote_price)
        if not all(math.isfinite(v) and v > 0 for v in (p, q)):
            return None
        if observed > now or not math.isclose(p, q, rel_tol=1e-6, abs_tol=1e-6):
            return None
        return observed
    except (ValueError, TypeError, OverflowError):
        return None
