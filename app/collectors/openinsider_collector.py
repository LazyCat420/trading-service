"""
openinsider_collector.py — Scrapes insider trades from OpenInsider.

Fetches raw HTML directly (httpx + browser headers): the scraper-service
/scrape endpoint returns text-EXTRACTED content (no tags, truncated), which
BeautifulSoup table parsing can never work on.
"""
import logging
import datetime
import hashlib
import httpx
from bs4 import BeautifulSoup
from app.db.connection import get_db

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


async def _fetch_html(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers=_BROWSER_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        logger.error("[openinsider] fetch failed for %s: %s", url, e)
        return None

def clean_float(val: str) -> float | None:
    if not val:
        return None
    val = val.replace("$", "").replace(",", "").replace("+", "").strip()
    try:
        return float(val)
    except ValueError:
        return None

def clean_int(val: str) -> int | None:
    if not val:
        return None
    val = val.replace(",", "").replace("+", "").strip()
    try:
        return int(val)
    except ValueError:
        return None

async def collect_cluster_buys(days: int = 30) -> int:
    """Scrape OpenInsider screener for cluster buys."""
    url = f"http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd={days}&fdr=&td=&tdr=&feession=&cession=&sicl=&sich=&grp=1&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=1&cnt=100&page=1"
    
    html = await _fetch_html(url)
    if not html:
        logger.error("[openinsider] Failed to fetch OpenInsider HTML content")
        return 0

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="tinytable")
    if not table:
        logger.warning("[openinsider] table.tinytable not found in page HTML")
        return 0

    tbody = table.find("tbody")
    tr_elements = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]
    
    rows = []
    for tr in tr_elements:
        tds = tr.find_all("td")
        # Live layout (2026-07): X, Filing Date, Trade Date, Ticker,
        # Company Name, Insider Name, Title, Trade Type, Price, Qty, Owned,
        # ΔOwn, Value, 1d, 1w, 1m, 6m — the old offsets skipped Company Name
        # and shifted every field one left, storing blank rows.
        if len(tds) < 13:
            continue

        filing_date_str = tds[1].text.strip()
        trade_date_str = tds[2].text.strip()
        ticker = tds[3].text.strip().upper()
        insider_name = tds[5].text.strip()
        insider_title = tds[6].text.strip()
        trade_type_full = tds[7].text.strip()
        price_str = tds[8].text.strip()
        qty_str = tds[9].text.strip()
        owned_str = tds[10].text.strip()
        delta_str = tds[11].text.strip()
        value_str = tds[12].text.strip()

        if not ticker or not insider_name:
            continue

        price = clean_float(price_str)
        qty = clean_int(qty_str)
        value = clean_float(value_str)
        shares_owned = clean_int(owned_str)

        delta_pct = None
        if delta_str:
            delta_pct_str = delta_str.replace("%", "").replace("+", "").strip()
            try:
                delta_pct = float(delta_pct_str)
            except ValueError:
                pass

        try:
            filing_date = datetime.datetime.strptime(filing_date_str, "%Y-%m-%d %H:%M:%S").date()
        except ValueError:
            try:
                filing_date = datetime.datetime.strptime(filing_date_str.split(" ")[0], "%Y-%m-%d").date()
            except ValueError:
                filing_date = datetime.date.today()

        try:
            trade_date = datetime.datetime.strptime(trade_date_str, "%Y-%m-%d").date()
        except ValueError:
            trade_date = filing_date

        trade_type = "P" if "Purchase" in trade_type_full else ("S" if "Sale" in trade_type_full else trade_type_full)
        
        # Unique ID constraint to avoid duplicates
        id_input = f"{ticker}_{insider_name}_{trade_date_str}_{qty_str}"
        trade_id = hashlib.sha256(id_input.encode("utf-8")).hexdigest()

        rows.append((
            trade_id, ticker, insider_name, insider_title, trade_type,
            price, qty, value, shares_owned, delta_pct, trade_date, filing_date, "openinsider"
        ))

    if rows:
        with get_db() as db:
            db.executemany("""
                INSERT INTO insider_trades
                (id, ticker, insider_name, insider_title, trade_type, price, qty, value, shares_owned, delta_pct, trade_date, filing_date, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, rows)
        logger.info(f"[openinsider] Scraped and inserted {len(rows)} insider trades")
        return len(rows)
    return 0

async def collect_all() -> dict:
    count = await collect_cluster_buys(days=30)
    return {"insider_trades": count}
