"""
tradingeconomics_collector.py — Scrapes calendar data from TradingEconomics.
"""
import logging
import datetime
import hashlib
from bs4 import BeautifulSoup
from app.services.scraper_client import scraper_client
from app.db.connection import get_db

logger = logging.getLogger(__name__)

def parse_val(val_str: str) -> float | None:
    if not val_str:
        return None
    val_str = val_str.replace("%", "").replace(",", "").replace("+", "").strip()
    if val_str in ["", "-", "N/A"]:
        return None
    # Parse suffixes
    multiplier = 1.0
    if val_str.endswith("B"):
        multiplier = 1e9
        val_str = val_str[:-1]
    elif val_str.endswith("M"):
        multiplier = 1e6
        val_str = val_str[:-1]
    elif val_str.endswith("K"):
        multiplier = 1e3
        val_str = val_str[:-1]
    try:
        return float(val_str) * multiplier
    except ValueError:
        return None

async def collect_economic_calendar() -> int:
    """Scrapes Trading Economics calendar, writing to economic_calendar."""
    url = "https://tradingeconomics.com/calendar"
    
    res = await scraper_client.scrape(url, engine="http")
    if not res or not res.get("content"):
        logger.error("[tradingeconomics] Scraper failed to fetch TradingEconomics page")
        return 0

    soup = BeautifulSoup(res["content"], "html.parser")
    table = soup.find("table", id="calendar")
    if not table:
        logger.warning("[tradingeconomics] Table id='calendar' not found in HTML")
        return 0

    rows = []
    current_date_str = None
    
    for tr in table.find_all("tr"):
        # Detect header rows which declare dates
        if tr.get("class") and "table-header" in tr.get("class"):
            current_date_str = tr.text.strip()
            continue

        if not current_date_str:
            continue

        tds = tr.find_all("td")
        if len(tds) < 6:
            continue

        time_str = tds[0].text.strip()
        country = tds[1].text.strip().upper()
        event_name = tds[2].text.strip()
        actual_str = tds[3].text.strip()
        forecast_str = tds[4].text.strip()
        previous_str = tds[5].text.strip()

        if not event_name or not country:
            continue

        # Combine date + time
        date_combined = current_date_str
        if time_str and time_str != "All Day":
            date_combined = f"{current_date_str} {time_str}"
        
        try:
            # e.g., "Monday May 15 2026 8:30 AM" or similar format
            event_date = datetime.datetime.strptime(date_combined, "%A %B %d %Y %I:%M %p")
        except ValueError:
            try:
                event_date = datetime.datetime.strptime(current_date_str, "%A %B %d %Y")
            except ValueError:
                event_date = datetime.datetime.now()

        actual = parse_val(actual_str)
        forecast = parse_val(forecast_str)
        previous = parse_val(previous_str)

        # Importance mapping from styling classes
        importance = "medium"
        importance_span = tds[2].find("span", class_="calendar-importance")
        if importance_span:
            cls = importance_span.get("class", [])
            if "high" in cls:
                importance = "high"
            elif "low" in cls:
                importance = "low"

        id_input = f"{event_name}_{country}_{event_date.isoformat()}"
        event_id = hashlib.sha256(id_input.encode("utf-8")).hexdigest()

        rows.append((
            event_id, event_name, country, event_date,
            actual, forecast, previous, importance, "tradingeconomics"
        ))

    if rows:
        with get_db() as db:
            db.executemany("""
                INSERT INTO economic_calendar
                (id, event_name, country, event_date, actual, forecast, previous, importance, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE 
                SET actual = EXCLUDED.actual, forecast = EXCLUDED.forecast, previous = EXCLUDED.previous
            """, rows)
        logger.info(f"[tradingeconomics] Scraped and inserted {len(rows)} economic calendar events")
        return len(rows)
    return 0

async def collect_all() -> dict:
    count = await collect_economic_calendar()
    return {"economic_calendar": count}
