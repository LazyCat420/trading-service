"""
Finviz Scraper — full snapshot-table scrape into `fundamentals`.

The quote page splits its stats across SIX `snapshot-table2` tables
(company/dividends, valuation, EPS/sales growth, ownership/margins,
shares/short/technicals, performance/analyst). The old version used
`soup.find` (first table only), so after finviz's redesign it silently
wrote near-empty rows. This version merges all six and maps ~45 fields.

Unit convention (matches the rest of the `fundamentals` table):
percent-like values stored as FRACTIONS (27% -> 0.27); Debt/Eq as RATIO
(finviz already reports a ratio). Upsert merges with COALESCE so a
same-day row from another source is FILLED, not blocked.
"""

import logging
import datetime
import re
from bs4 import BeautifulSoup
from app.db.connection import get_db
from app.services.request_utils import SmartClient

logger = logging.getLogger(__name__)

_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def parse_val(v_str):
    """'4948.32B' -> 4.94832e12, '13.66%' -> 13.66, '-' -> None."""
    if not v_str or v_str == "-":
        return None
    v_str = v_str.replace(",", "").strip()
    mult = 1
    if v_str and v_str[-1] in _MULT:
        mult = _MULT[v_str[-1]]
        v_str = v_str[:-1]
    if v_str.endswith("%"):
        v_str = v_str[:-1]
    try:
        return float(v_str) * mult
    except ValueError:
        return None


def _frac(v_str):
    """Percent string -> fraction ('13.66%' -> 0.1366)."""
    v = parse_val(v_str)
    return v / 100.0 if v is not None else None


def _first_num(v_str):
    """'334.99 0.57%' -> 334.99 (pairs like 52W High = price + distance)."""
    if not v_str or v_str == "-":
        return None
    return parse_val(v_str.split()[0])


def _second_frac(v_str):
    """'6.89% 17.91%' -> 0.1791 (pairs like 'EPS past 3/5Y' = 3Y then 5Y)."""
    if not v_str:
        return None
    parts = v_str.split()
    if len(parts) < 2:
        return None
    v = parse_val(parts[1])
    return v / 100.0 if v is not None else None


def _paren_frac(v_str):
    """'1.05 (0.31%)' -> 0.0031 (dividend $ amount + yield in parens)."""
    if not v_str:
        return None
    m = re.search(r"\(([-\d.]+)%\)", v_str)
    return float(m.group(1)) / 100.0 if m else None


def _parse_date(v_str):
    """'Dec 12, 1980' -> date."""
    if not v_str or v_str == "-":
        return None
    try:
        return datetime.datetime.strptime(v_str.strip(), "%b %d, %Y").date()
    except ValueError:
        return None


def _parse_earnings(v_str, today):
    """'Jul 30 AMC' -> nearest date for that month/day (finviz omits the
    year; earnings shown are within ~6 months of today, past or future)."""
    if not v_str or v_str == "-":
        return None
    m = re.match(r"([A-Z][a-z]{2}) (\d{1,2})", v_str.strip())
    if not m:
        return None
    try:
        base = datetime.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%b %d").date()
    except ValueError:
        return None
    candidates = [base.replace(year=today.year + dy) for dy in (-1, 0, 1)]
    return min(candidates, key=lambda d: abs((d - today).days))


async def fetch_snapshot(ticker: str) -> dict | None:
    """Fetch and merge every snapshot-table2 on the quote page."""
    url = f"https://finviz.com/quote.ashx?t={ticker}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with SmartClient(base_delay=2.0, max_retries=2) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.info(f"[finviz] Error scraping {ticker}: HTTP {resp.status_code}")
            return None

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table", class_="snapshot-table2")
    if not tables:
        logger.info(f"[finviz] No snapshot tables found for {ticker}")
        return None

    data = {}
    for table in tables:
        for row in table.find_all("tr"):
            cols = row.find_all("td")
            for i in range(0, len(cols), 2):
                if i + 1 < len(cols):
                    data[cols[i].text.strip()] = cols[i + 1].text.strip()
    return data


async def collect_fundamentals(ticker: str) -> bool:
    """Scrape the full finviz snapshot into `fundamentals`."""
    data = await fetch_snapshot(ticker)
    if not data:
        return False

    today = datetime.date.today()
    opt_short = (data.get("Option/Short") or "").split("/")
    optionable = opt_short[0].strip().lower() == "yes" if len(opt_short) == 2 else None
    shortable = opt_short[1].strip().lower() == "yes" if len(opt_short) == 2 else None

    fields = {
        # legacy columns
        "market_cap": parse_val(data.get("Market Cap")),
        "pe_ratio": parse_val(data.get("P/E")),
        "forward_pe": parse_val(data.get("Forward P/E")),
        "peg_ratio": parse_val(data.get("PEG")),
        "price_to_book": parse_val(data.get("P/B")),
        "price_to_sales": parse_val(data.get("P/S")),
        "ev_to_ebitda": parse_val(data.get("EV/EBITDA")),
        "profit_margin": _frac(data.get("Profit Margin")),
        "roe": _frac(data.get("ROE")),
        "roa": _frac(data.get("ROA")),
        "revenue": parse_val(data.get("Sales")),
        "net_income": parse_val(data.get("Income")),
        "debt_to_equity": parse_val(data.get("Debt/Eq")),  # already a ratio
        "current_ratio": parse_val(data.get("Current Ratio")),
        "beta": parse_val(data.get("Beta")),
        "week_52_high": _first_num(data.get("52W High")),
        "week_52_low": _first_num(data.get("52W Low")),
        "short_float_pct": _frac(data.get("Short Float")),
        "revenue_growth": _frac(data.get("Sales Y/Y TTM")),
        # extension (2026-07-27)
        "dividend_yield": _paren_frac(data.get("Dividend TTM")),
        "dividend_ttm": _first_num(data.get("Dividend TTM")),
        "payout_ratio": _frac(data.get("Payout")),
        "price_to_cash": parse_val(data.get("P/C")),
        "price_to_fcf": parse_val(data.get("P/FCF")),
        "ev_to_sales": parse_val(data.get("EV/Sales")),
        "lt_debt_to_equity": parse_val(data.get("LT Debt/Eq")),
        "quick_ratio": parse_val(data.get("Quick Ratio")),
        "eps_ttm": parse_val(data.get("EPS (ttm)")),
        "eps_next_q": parse_val(data.get("EPS next Q")),
        "eps_growth_this_y": _frac(data.get("EPS this Y")),
        "eps_growth_next_y": _frac(data.get("EPS next Y")),
        "eps_growth_next_5y": _frac(data.get("EPS next 5Y")),
        "eps_growth_past_5y": _second_frac(data.get("EPS past 3/5Y")),
        "sales_growth_past_5y": _second_frac(data.get("Sales past 3/5Y")),
        "eps_growth_qoq": _frac(data.get("EPS Q/Q")),
        "sales_growth_qoq": _frac(data.get("Sales Q/Q")),
        "eps_surprise": _frac((data.get("EPS/Sales Surpr.") or "").split()[0] if data.get("EPS/Sales Surpr.") else None),
        "sales_surprise": _second_frac(data.get("EPS/Sales Surpr.")),
        "insider_own_pct": _frac(data.get("Insider Own")),
        "insider_trans_pct": _frac(data.get("Insider Trans")),
        "inst_own_pct": _frac(data.get("Inst Own")),
        "inst_trans_pct": _frac(data.get("Inst Trans")),
        "roic": _frac(data.get("ROIC")),
        "gross_margin": _frac(data.get("Gross Margin")),
        "oper_margin": _frac(data.get("Oper. Margin")),
        "shares_outstanding": parse_val(data.get("Shs Outstand")),
        "shares_float": parse_val(data.get("Shs Float")),
        "short_ratio": parse_val(data.get("Short Ratio")),
        "short_interest": parse_val(data.get("Short Interest")),
        "recom_score": parse_val(data.get("Recom")),
        "target_price": parse_val(data.get("Target Price")),
        "earnings_date": _parse_earnings(data.get("Earnings"), today),
        "ipo_date": _parse_date(data.get("IPO")),
        "optionable": optionable,
        "shortable": shortable,
    }

    cols = list(fields.keys())
    updates = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, fundamentals.{c})" for c in cols)
    filled = sum(1 for v in fields.values() if v is not None)

    with get_db() as db:
        db.execute(
            f"""
            INSERT INTO fundamentals (ticker, snapshot_date, source, {', '.join(cols)})
            VALUES (%s, %s, 'finviz', {', '.join(['%s'] * len(cols))})
            ON CONFLICT (ticker, snapshot_date) DO UPDATE SET {updates}
            """,
            [ticker, today] + [fields[c] for c in cols],
        )

    logger.info(f"[finviz] {ticker}: {filled}/{len(cols)} snapshot fields scraped")
    return True
