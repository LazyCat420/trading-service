"""
SEC EDGAR Collector — Fetches 13F institutional holdings via edgartools.

Pure data collector. No LLM calls. No processing.
Writes to: sec_13f_holdings
Library: edgartools (free, no API key needed)

Schedule: Run once per quarter (13F filings are released 45 days after quarter end)

NOTE: edgartools is synchronous — we wrap in asyncio.to_thread to avoid blocking.
"""

import logging

logger = logging.getLogger(__name__)


import asyncio
import datetime
import math
import pandas as pd
from edgar import Company, set_identity
from app.db import mongo_query, mongo_store

# SEC EDGAR requires a User-Agent header identifying you
set_identity("TradingBot analysis@example.com")

# Top hedge funds / institutional investors
# CIK numbers verified from SEC EDGAR
# List updated quarterly — two tiers:
#   1. Mega/Large by AUM (original 20)
#   2. Top performers by 3-year annualized return (HedgeFollow-style ranking)
TRACKED_FUNDS = [
    # -- Mega Funds (>$100B AUM) --
    ("Berkshire Hathaway", "0001067983"),
    ("Bridgewater Associates", "0001350694"),
    ("Citadel Advisors", "0001423053"),
    ("Renaissance Technologies", "0001037389"),
    ("D.E. Shaw", "0001009207"),
    ("Two Sigma Investments", "0001179392"),
    ("Millennium Management", "0001273087"),
    ("AQR Capital Management", "0001167557"),
    # -- Large Funds ($10-100B AUM) --
    ("Tiger Global Management", "0001167483"),
    ("Point72 Asset Management", "0001603466"),
    ("Soros Fund Management", "0001029160"),
    ("Pershing Square Capital", "0001336528"),
    ("Viking Global Investors", "0001103804"),
    ("Baupost Group", "0001061768"),
    ("Elliott Investment Management", "0001791786"),
    ("Appaloosa Management", "0001656456"),
    # -- Notable / High Conviction --
    ("Druckenmiller (Duquesne)", "0001536411"),
    ("Greenlight Capital", "0001079114"),
    ("Third Point", "0001040273"),
    ("Coatue Management", "0001535392"),
    # -- Top Performers (High 3yr Returns) --
    # Added to capture performance-ranked conviction signals
    ("TCI Fund Management", "0001647251"),
    ("Lone Pine Capital", "0001061165"),
    ("Whale Rock Capital", "0001598336"),
    ("Maverick Capital", "0001010621"),
    ("Light Street Capital", "0001697575"),
    ("Altimeter Capital", "0001737926"),
]

# Per-fund timeout for edgartools (seconds)
EDGAR_TIMEOUT = 60


def _clean(value) -> str | None:
    """Normalize a DataFrame cell to a stripped string, or None if empty/NaN."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        # pd.isna raises on array-likes — fall through to str()
        pass
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "nat"):
        return None
    return text


def _num(value) -> float:
    """Coerce a DataFrame cell to a finite float, defaulting to 0.0.

    NOTE: `value or 0` is NOT safe here — NaN is truthy in Python, so a missing
    numeric cell would propagate NaN straight into a DOUBLE PRECISION column.
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(result) or math.isinf(result):
        return 0.0
    return result


def _first_col(row, *names) -> str | None:
    """Return the first non-empty value among several candidate column names.

    edgartools' infotable column naming varies by version and by whether the
    filing was parsed from the legacy or XML info table, so probe defensively.
    """
    for name in names:
        value = _clean(row.get(name))
        if value is not None:
            return value
    return None


def _covered_quarter(filed_date) -> tuple[str, datetime.date | None]:
    """Derive the quarter a 13F COVERS from its filing date.

    13F filings are due 45 days after quarter end:
      Filed Jan-Mar → covers Q4 of previous year
      Filed Apr-Jun → covers Q1 of same year
      Filed Jul-Sep → covers Q2 of same year
      Filed Oct-Dec → covers Q3 of same year
    Returns (filing_quarter_label, filing_date).
    """
    if hasattr(filed_date, "date"):
        filed_date = filed_date.date()
    elif isinstance(filed_date, str):
        filed_date = datetime.date.fromisoformat(filed_date)
    if not isinstance(filed_date, datetime.date):
        return "", None

    filing_q = (filed_date.month - 1) // 3 + 1
    # The covered quarter is one quarter BEFORE the filing quarter
    if filing_q == 1:
        covered_year = filed_date.year - 1
        covered_q = 4
    else:
        covered_year = filed_date.year
        covered_q = filing_q - 1
    return f"{covered_year}Q{covered_q}", filed_date


def _parse_filing(filer_name: str, cik: str, filing) -> tuple[list[dict], str]:
    """Parse ONE 13F filing into holdings dicts. Returns (holdings, quarter)."""
    filing_quarter, filed_date = _covered_quarter(filing.filing_date)
    if not filing_quarter:
        logger.info(f"[sec] {filer_name}: unparseable filing date, skipping filing")
        return [], ""

    filing_obj = filing.obj()
    if not hasattr(filing_obj, "infotable") or filing_obj.infotable is None:
        logger.info(f"[sec] {filer_name}: no holdings table in 13F ({filing_quarter})")
        return [], filing_quarter

    df = filing_obj.infotable
    if not isinstance(df, pd.DataFrame):
        if hasattr(df, "to_dataframe"):
            df = df.to_dataframe()
        else:
            logger.info(
                f"[sec] {filer_name}: cannot parse holdings format ({type(df)})"
            )
            return [], filing_quarter

    holdings = []
    for _, row in df.iterrows():
        ticker = _first_col(row, "Ticker", "ticker", "Symbol")
        name_of_issuer = _first_col(
            row, "Issuer", "NameOfIssuer", "nameOfIssuer", "name_of_issuer"
        )
        cusip = _first_col(row, "Cusip", "CUSIP", "cusip")
        share_type = _first_col(
            row, "SharesPrnType", "shrsOrPrnAmt_sshPrnamtType", "share_type", "Type"
        )

        shares = int(_num(row.get("SharesPrnAmount")))
        # Since the SEC's Jan-2023 rule change, Form 13F values are filed in
        # FULL DOLLARS (the pre-2023 convention was thousands). The old
        # unconditional *1000 here inflated every edgar-sourced holding 1000x —
        # agents were shown "$185B institutional value" in an $8B company (MP,
        # 08-04 cycle). Quarters through 2022Q3 were still FILED before the
        # rule change, so a historical backfill keeps the thousands scaling.
        # Sanity check either way: value/shares should be a plausible price.
        value = _num(row.get("Value"))
        if filing_quarter and str(filing_quarter) <= "2022Q3":
            value *= 1000

        # `ticker` is part of the PRIMARY KEY (cik, ticker, filing_quarter) and is
        # declared NOT NULL, so it cannot be left NULL. When no real ticker
        # resolves we fall back to the CUSIP (a stable security identifier that
        # will never be mistaken for a ticker by charts that GROUP BY ticker),
        # and only then to the literal 'UNKNOWN'. The issuer NAME now goes to
        # name_of_issuer where it belongs — never into the ticker slot.
        ticker_slot = ticker or cusip or "UNKNOWN"

        holdings.append(
            {
                "cik": cik,
                "ticker": ticker_slot,
                "name_of_issuer": name_of_issuer,
                "cusip": cusip,
                "share_type": share_type,
                "filing_quarter": filing_quarter,
                "filing_date": filed_date,
                "shares": shares,
                "value": value,
            }
        )

    return holdings, filing_quarter


def _fetch_holdings_sync(
    filer_name: str, cik: str, limit: int = 5
) -> list[tuple[list[dict], str]]:
    """
    Synchronous function that does the slow edgartools work.
    Parses ALL recent 13F filings (not just the newest) so one run yields
    multi-quarter history.
    Returns a list of (holdings_list, filing_quarter) — one entry per filing.
    """
    company = Company(cik)
    # Get only recent filings (limit prevents downloading full history).
    # 13F-HR/A are AMENDMENTS — they must be ingested so they can correct
    # previously written rows via the ON CONFLICT DO UPDATE below.
    filings = company.get_filings(form=["13F-HR", "13F-HR/A"]).latest(limit)

    if filings is None:
        logger.info(f"[sec] No 13F filings found for {filer_name} (CIK: {cik})")
        return []

    # .latest(n) returns a single filing when n == 1, a sequence otherwise
    if not isinstance(filings, (list, tuple)) and not hasattr(filings, "__len__"):
        filings = [filings]
    if len(filings) == 0:
        logger.info(f"[sec] No 13F filings found for {filer_name} (CIK: {cik})")
        return []

    # Apply OLDEST first so that a later-filed 13F-HR/A amendment overwrites the
    # original it corrects, rather than the other way round (.latest() is
    # newest-first, which would apply the corrections then clobber them).
    try:
        ordered = sorted(filings, key=lambda f: _covered_quarter(f.filing_date)[1] or datetime.date.min)
    except Exception:
        ordered = list(reversed(list(filings)))

    results: list[tuple[list[dict], str]] = []
    for filing in ordered:
        # Isolate per-filing failures — one bad quarter must not lose the rest
        try:
            holdings, filing_quarter = _parse_filing(filer_name, cik, filing)
        except Exception as e:
            logger.info(f"[sec] {filer_name}: filing parse error: {e}")
            continue
        if holdings:
            results.append((holdings, filing_quarter))

    return results



def _upsert_13f_filer(cik: str, filer_name: str, filing_quarter: str) -> None:
    """`INSERT INTO sec_13f_filers ... ON CONFLICT (cik) DO UPDATE SET
    filer_name = EXCLUDED.filer_name,
    latest_quarter = GREATEST(sec_13f_filers.latest_quarter, EXCLUDED.latest_quarter)`

    GREATEST keeps the LATER quarter — a backfill of an older filing must not
    roll `latest_quarter` backwards. Mongo cannot express a two-sided max in a
    plain update, so the stored value is read and the max taken in Python.
    Quarter labels are "YYYYQn", which orders correctly as a string.
    """
    row = mongo_query.find_row('sec_13f_filers', {'cik': cik}, ['latest_quarter'])
    latest = filing_quarter
    if row and row[0] and str(row[0]) > str(filing_quarter):
        latest = row[0]
    mongo_store.upsert_doc('sec_13f_filers', {'cik': cik},
                           {'cik': cik, 'filer_name': filer_name,
                            'latest_quarter': latest})


async def collect_fund_holdings(
    filer_name: str,
    cik: str,
) -> int:
    """
    Fetch latest 13F holdings for a fund and upsert into sec_13f_holdings.
    Runs edgartools in a thread with timeout to avoid blocking.
    Returns number of holdings rows inserted.
    """
    try:
        # Run with timeout — edgartools can hang on large filings
        filings = await asyncio.wait_for(
            asyncio.to_thread(_fetch_holdings_sync, filer_name, cik),
            timeout=EDGAR_TIMEOUT,
        )

        if not filings:
            return 0

        total = 0
        quarters = []
        for holdings, filing_quarter in filings:
            if not holdings:
                continue
            # Upsert filer to ensure it exists
            _upsert_13f_filer(cik, filer_name, filing_quarter)
            for h in holdings:
                mongo_store.update_docs('sec_13f_holdings', {'cik': h["cik"], 'ticker': h["ticker"], 'filing_quarter': h["filing_quarter"]}, {'$set': {'name_of_issuer': h["name_of_issuer"], 'cusip': h["cusip"], 'filing_date': h["filing_date"], 'shares': h["shares"], 'share_type': h["share_type"], 'value_usd': h["value"], 'source': "edgar", 'collected_at': datetime.datetime.now(datetime.timezone.utc)}, '$setOnInsert': {'pct_change': None, 'is_new_position': False, 'is_exit': False}}, upsert=True)
            total += len(holdings)
            quarters.append(filing_quarter)

        from app.telemetry import send_system_log
        send_system_log(
            subsystem="DB",
            message=f"[SEC] {filer_name}: Upserted {total} holdings rows to sec_13f_holdings"
        )
        logger.info(
            f"[sec] {filer_name}: {total} holdings written across "
            f"{len(quarters)} quarters ({', '.join(quarters)})"
        )
        return total

    except asyncio.TimeoutError:
        logger.info(f"[sec] {filer_name}: TIMEOUT after {EDGAR_TIMEOUT}s (CIK: {cik})")
        return 0
    except Exception as e:
        logger.info(f"[sec] {filer_name} error: {e}")
        return 0


async def collect_all_funds() -> dict:
    """Fetch 13F holdings for all tracked funds. Isolates errors per fund."""
    results = {}
    total_holdings = 0
    for name, cik in TRACKED_FUNDS:
        count = await collect_fund_holdings(name, cik)
        results[name] = count
        total_holdings += count
        # Pace between funds to avoid hammering EDGAR
        from app.telemetry import send_system_log
        send_system_log(
            subsystem="SCRAPER",
            message="SEC Edgar rate limit pause: 2s"
        )
        await asyncio.sleep(2)
    logger.info(
        f"[sec] Total: {total_holdings} holdings across {len(TRACKED_FUNDS)} funds "
        f"({sum(1 for v in results.values() if v > 0)} succeeded)"
    )
    return results


async def collect_ticker_institutional(ticker: str) -> int:
    """Fetch institutional holders for a specific ticker via yfinance.

    This is MORE RELIABLE than edgartools for per-ticker data because:
    - No CIK lookup needed
    - Works for any publicly traded ticker
    - Returns structured holder/shares/value data
    - Free, no rate limiting issues

    Returns number of holders inserted.
    """
    try:
        import os

        # Redirect yfinance cache to /tmp to avoid Permission Denied on /home/appusr
        os.environ["YFINANCE_CACHE_DIR"] = "/tmp/yfinance"
        import yfinance as yf

        t = yf.Ticker(ticker)
        ih = await asyncio.to_thread(lambda: t.institutional_holders)

        if ih is None or len(ih) == 0:
            logger.info(f"[sec] {ticker}: no yfinance institutional data")
            return 0

        now = datetime.datetime.now()
        quarter = f"{now.year}Q{(now.month - 1) // 3 + 1}"
        count = 0

        for _, row in ih.iterrows():
            holder = _clean(row.get("Holder")) or "Unknown"
            shares = int(_num(row.get("Shares")))
            value = _num(row.get("Value"))

            import hashlib

            holder_hash = hashlib.md5(holder.encode()).hexdigest()[:10]
            pseudo_cik = f"yf_{holder_hash}"

            # Upsert filer
            _upsert_13f_filer(pseudo_cik, holder, quarter)


            # Insert holdings.
            # source='yfinance' — these rows use a SYNTHESIZED pseudo-CIK and
            # are NOT real EDGAR 13F filings. Any query counting "funds
            # holding X" must filter source='edgar' or it double-counts.
            mongo_store.update_docs('sec_13f_holdings', {'cik': pseudo_cik, 'ticker': ticker, 'filing_quarter': quarter}, {'$set': {'name_of_issuer': holder, 'shares': shares, 'value_usd': value, 'source': "yfinance", 'collected_at': datetime.datetime.now(datetime.timezone.utc)}, '$setOnInsert': {'pct_change': None, 'is_new_position': False, 'is_exit': False}}, upsert=True)
            count += 1

        from app.telemetry import send_system_log
        send_system_log(
            subsystem="DB",
            message=f"[yfinance] {ticker}: Upserted {count} institutional holder rows to sec_13f_holdings"
        )
        logger.info(f"[sec] {ticker}: {count} institutional holders via yfinance")
        return count

    except ImportError:
        logger.info("[sec] yfinance not installed")
        return 0
    except Exception as e:
        logger.info(f"[sec] {ticker} yfinance error: {e}")
        return 0


async def collect_all_tickers_institutional(tickers: list[str]) -> dict:
    """Fetch institutional holders for a list of tickers via yfinance."""
    results = {}
    for ticker in tickers:
        count = await collect_ticker_institutional(ticker)
        results[ticker] = count
        await asyncio.sleep(1)  # Rate limit
    return results
