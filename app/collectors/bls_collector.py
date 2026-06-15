"""
bls_collector.py — US economic macro indicators from Bureau of Labor Statistics (BLS).
"""
import logging
import datetime
import httpx
from app.config import settings
from app.db.connection import get_db

logger = logging.getLogger(__name__)

BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

BLS_SERIES = {
    "CPI_ALL":       "CUUR0000SA0",     # CPI All Items
    "CPI_CORE":      "CUUR0000SA0L1E",  # CPI Less Food & Energy 
    "UNEMPLOYMENT":  "LNS14000000",     # Unemployment Rate
    "NONFARM":       "CES0000000001",    # Total Nonfarm Employment
    "AVG_HOURLY":    "CES0500000003",    # Avg Hourly Earnings
    "PPI_ALL":       "WPSFD4",           # PPI All Commodities
}

def parse_bls_date(year: str, period: str) -> datetime.date | None:
    if period.startswith("M"):
        month = int(period[1:])
        return datetime.date(int(year), month, 1)
    elif period.startswith("Q"):
        q = int(period[1:])
        month = (q - 1) * 3 + 1
        return datetime.date(int(year), month, 1)
    else:
        return datetime.date(int(year), 12, 31)

async def collect_bls_series() -> int:
    """Fetch timeseries indicators from BLS API and write to macro_indicators."""
    current_year = datetime.datetime.now(datetime.timezone.utc).year
    payload = {
        "seriesid": list(BLS_SERIES.values()),
        "startyear": str(current_year - 2),
        "endyear": str(current_year)
    }
    
    if settings.BLS_API_KEY:
        payload["registrationkey"] = settings.BLS_API_KEY

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(BLS_URL, json=payload)
            r.raise_for_status()
            res_json = r.json()
    except Exception as e:
        logger.error(f"[bls] API request failed: {e}")
        return 0

    if res_json.get("status") != "REQUEST_SUCCEEDED":
        logger.error(f"[bls] API returned non-success: {res_json.get('message')}")
        return 0

    series_map = {v: k for k, v in BLS_SERIES.items()}
    results = res_json.get("Results", {}).get("series", [])
    
    rows = []
    for s in results:
        series_id = s.get("seriesID")
        indicator_name = series_map.get(series_id)
        if not indicator_name:
            continue
            
        data_points = s.get("data", [])
        for pt in data_points:
            year = pt.get("year")
            period = pt.get("period")
            val_str = pt.get("value")
            
            parsed_date = parse_bls_date(year, period)
            try:
                val = float(val_str)
            except ValueError:
                continue

            if parsed_date:
                rows.append((indicator_name, parsed_date, val, "US", "bls"))

    if rows:
        with get_db() as db:
            db.executemany("""
                INSERT INTO macro_indicators
                (indicator, date, value, country, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (indicator, date, country) DO UPDATE SET value = EXCLUDED.value
            """, rows)
        logger.info(f"[bls] Wrote {len(rows)} macro indicators rows to macro_indicators")
        return len(rows)
    return 0

async def collect_all() -> dict:
    count = await collect_bls_series()
    return {"bls_rows": count}
