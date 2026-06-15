"""
worldbank_collector.py — World Bank Open Data API collector.
"""
import logging
import datetime
import httpx
from app.db.connection import get_db

logger = logging.getLogger(__name__)

WB_INDICATORS = {
    "GDP_GROWTH":    "NY.GDP.MKTP.KD.ZG",
    "INFLATION":     "FP.CPI.TOTL.ZG",
    "UNEMPLOYMENT":  "SL.UEM.TOTL.ZS",
    "CURRENT_ACCT":  "BN.CAB.XOKA.GD.ZS",
}

async def collect_indicators(country: str = "US", years: int = 5) -> int:
    """Fetch World Bank macro indicators."""
    total_written = 0
    date_cutoff = datetime.datetime.now(datetime.timezone.utc).year - years
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for indicator_name, code in WB_INDICATORS.items():
            url = f"https://api.worldbank.org/v2/country/{country}/indicator/{code}"
            try:
                r = await client.get(url, params={"format": "json", "per_page": 100})
                r.raise_for_status()
                res_data = r.json()
            except Exception as e:
                logger.error(f"[worldbank] Failed to retrieve {indicator_name} for {country}: {e}")
                continue

            if not isinstance(res_data, list) or len(res_data) < 2:
                continue

            data_list = res_data[1]
            if not data_list:
                continue

            rows = []
            for item in data_list:
                year_str = item.get("date")
                val = item.get("value")
                if not year_str or val is None:
                    continue

                year = int(year_str)
                if year < date_cutoff:
                    continue

                # Store as end of year date
                parsed_date = datetime.date(year, 12, 31)
                rows.append((indicator_name, parsed_date, float(val), country.upper(), "worldbank"))

            if rows:
                with get_db() as db:
                    db.executemany("""
                        INSERT INTO macro_indicators
                        (indicator, date, value, country, source)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (indicator, date, country) DO UPDATE SET value = EXCLUDED.value
                    """, rows)
                total_written += len(rows)

    logger.info(f"[worldbank] Wrote {total_written} macro rows for {country}")
    return total_written

async def collect_all() -> dict:
    count = await collect_indicators("US", years=5)
    return {"worldbank_US_rows": count}
