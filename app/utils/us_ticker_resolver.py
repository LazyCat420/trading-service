"""
US Ticker Resolver — Maps foreign tickers to US-listed equivalents (ADRs/cross-listings).

Layered resolution strategy (cheapest → most expensive):
  Layer 1: Hard-coded ADR map (free, instant)
  Layer 2: Format detection — reject obvious foreign formats (free, instant)
  Layer 3: yfinance exchange field check (1 API call, cached)
  Layer 4: yfinance company name search for US ADR (1-2 API calls)

Usage:
    from app.utils.us_ticker_resolver import is_us_tradeable, resolve_to_us_ticker
"""

import re
import logging
import asyncio
from functools import lru_cache

logger = logging.getLogger(__name__)


# ── Layer 1: Hard-coded ADR / cross-listing map ──────────────────────
# Top foreign tickers → their US-listed equivalents.
# Updated manually for the most traded ADRs. This is the fastest lookup.
KNOWN_ADR_MAP: dict[str, str] = {
    # ── Korean stocks (.KS = KRX, .KQ = KOSDAQ) ──
    "000660.KS": "SKHYV",   # SK Hynix → NASDAQ ADR (IPO'd 2026-07-10)
    "005930.KS": "SSNLF",   # Samsung Electronics → OTC
    "005935.KS": "SSNLF",   # Samsung Electronics (preferred) → OTC
    "035420.KS": "NPSNY",   # NAVER → OTC
    "035720.KS": "KRMAY",   # Kakao → OTC
    # ── Japanese stocks (.T = Tokyo) ──
    "6758.T": "SONY",       # Sony → NYSE
    "7203.T": "TM",         # Toyota → NYSE
    "7267.T": "HMC",        # Honda → NYSE
    "6861.T": "KYOEY",      # Keyence → OTC
    "9984.T": "SFTBY",      # SoftBank Group → OTC
    "6501.T": "HTHIY",      # Hitachi → OTC
    "8306.T": "MUFG",       # Mitsubishi UFJ → NYSE
    # ── Chinese stocks (.HK = Hong Kong, .SS/.SZ = Shanghai/Shenzhen) ──
    "9988.HK": "BABA",      # Alibaba → NYSE
    "0700.HK": "TCEHY",     # Tencent → OTC
    "9618.HK": "JD",        # JD.com → NASDAQ
    "9999.HK": "NTES",      # NetEase → NASDAQ
    "1810.HK": "XIACY",     # Xiaomi → OTC
    "9888.HK": "BIDU",      # Baidu → NASDAQ
    "3690.HK": "MPNGY",     # Meituan → OTC
    # ── Taiwanese stocks (.TW = TWSE) ──
    "2330.TW": "TSM",       # TSMC → NYSE
    "2317.TW": "HNHPF",     # Hon Hai (Foxconn) → OTC
    "2454.TW": "MRAAY",     # MediaTek → OTC
    # ── European stocks (.L = London, .DE = Frankfurt, .PA = Paris) ──
    "ASML.AS": "ASML",      # ASML → NASDAQ
    "SAP.DE": "SAP",        # SAP → NYSE
    "NOVO-B.CO": "NVO",     # Novo Nordisk → NYSE
    "AZN.L": "AZN",         # AstraZeneca → NASDAQ
    "SHEL.L": "SHEL",       # Shell → NYSE
    "NESN.SW": "NSRGY",     # Nestlé → OTC
    "ROG.SW": "RHHBY",      # Roche → OTC
    "ULVR.L": "UL",         # Unilever → NYSE
    "BP.L": "BP",           # BP → NYSE
    "GSK.L": "GSK",         # GSK → NYSE
    "RIO.L": "RIO",         # Rio Tinto → NYSE
    "BHP.AX": "BHP",        # BHP → NYSE
    "TTE.PA": "TTE",        # TotalEnergies → NYSE
}

# Reverse map: US ticker → list of foreign equivalents (for reference/logging only)
_REVERSE_MAP: dict[str, list[str]] = {}
for _foreign, _us in KNOWN_ADR_MAP.items():
    _REVERSE_MAP.setdefault(_us, []).append(_foreign)


# ── Layer 2: Foreign ticker format detection ─────────────────────────
# Patterns that identify obviously foreign tickers:
#   - Contains digits (000660, 6758, 2330) — Asian markets use numeric codes
#   - Contains dots with exchange suffix (.KS, .T, .HK, .TW, .L, .DE, etc.)
#   - Contains dots with non-class suffixes (exclude BRK.B, BF.B which are US share classes)
FOREIGN_EXCHANGE_SUFFIXES = {
    ".KS", ".KQ",           # Korea (KRX, KOSDAQ)
    ".T", ".TYO",           # Japan (Tokyo)
    ".HK",                  # Hong Kong
    ".SS", ".SZ",           # China (Shanghai, Shenzhen)
    ".TW", ".TWO",          # Taiwan
    ".L", ".IL",            # London
    ".DE", ".F", ".MU",     # Germany (Xetra, Frankfurt, Munich)
    ".PA",                  # France (Euronext Paris)
    ".AS", ".BR",           # Netherlands/Belgium (Euronext)
    ".SW", ".VX",           # Switzerland (SIX)
    ".MI",                  # Italy (Milan)
    ".MC",                  # Spain (Madrid)
    ".ST",                  # Sweden (Stockholm)
    ".CO", ".HE",           # Denmark/Finland (Copenhagen, Helsinki)
    ".OL",                  # Norway (Oslo)
    ".IS",                  # Turkey (Istanbul)
    ".SA",                  # Brazil (São Paulo)
    ".MX",                  # Mexico
    ".AX",                  # Australia (ASX)
    ".NZ",                  # New Zealand
    ".NS", ".BO",           # India (NSE, BSE)
    ".JK",                  # Indonesia (Jakarta)
    ".SI",                  # Singapore
    ".BK",                  # Thailand (Bangkok)
    ".KL",                  # Malaysia (Kuala Lumpur)
    ".TA",                  # Israel (Tel Aviv)
    ".JO",                  # South Africa (Johannesburg)
}

# US share class suffixes that should NOT be treated as foreign
US_CLASS_SUFFIXES_RE = re.compile(r"^[A-Z]+\.[A-B]$")  # BRK.A, BRK.B, BF.B

# Known US exchanges from yfinance
US_EXCHANGES = {
    "NMS",   # NASDAQ Global Select Market
    "NYQ",   # NYSE
    "NGM",   # NASDAQ Global Market
    "NCM",   # NASDAQ Capital Market
    "ASE",   # NYSE American (AMEX)
    "BTS",   # BATS
    "PCX",   # NYSE Arca
    "NYE",   # NYSE (alternate)
    "OPR",   # NYSE Arca Options
    "PNK",   # OTC Pink (include for OTC ADRs)
}


def _has_foreign_format(ticker: str) -> bool:
    """Check if a ticker has an obviously foreign format.

    Returns True for:
      - Tickers with known foreign exchange suffixes (.KS, .T, .HK, etc.)
      - Tickers that are purely numeric (000660, 6758) — Asian market codes
      - Tickers starting with digits (003160.KS)

    Returns False for:
      - Normal US tickers (AAPL, NVDA, BRK.B)
      - US share class notation (BRK.A, BF.B)
    """
    # US share class notation is fine
    if US_CLASS_SUFFIXES_RE.match(ticker):
        return False

    # Check for known foreign exchange suffixes
    for suffix in FOREIGN_EXCHANGE_SUFFIXES:
        if ticker.upper().endswith(suffix):
            return True

    # Purely numeric tickers (or starts with digit) = Asian market
    stripped = ticker.replace(".", "").replace("-", "")
    if stripped.isdigit():
        return True
    if ticker and ticker[0].isdigit():
        return True

    return False


def is_us_tradeable(ticker: str) -> bool:
    """Quick check: does this ticker look like a US-tradeable symbol?

    This is a cheap format-based check (no API calls). It returns:
      True  — ticker uses standard US format (letters only, or US share class like BRK.B)
      False — ticker has foreign exchange suffix or numeric format

    For definitive exchange verification, use verify_us_exchange() which calls yfinance.
    """
    ticker = ticker.upper().strip()

    if not ticker:
        return False

    # If it's in our known ADR map as a foreign ticker, it's NOT US-tradeable
    if ticker in KNOWN_ADR_MAP:
        return False

    # Format-based detection
    if _has_foreign_format(ticker):
        return False

    return True


def resolve_to_us_ticker(ticker: str) -> str | None:
    """Resolve a foreign ticker to its US-listed equivalent.

    Resolution layers (stops at first match):
      1. Hard-coded ADR map (instant)
      2. Returns None if no mapping found

    For async resolution with yfinance API calls, use resolve_to_us_ticker_async().
    """
    ticker = ticker.upper().strip()

    # Layer 1: Hard-coded map
    us_ticker = KNOWN_ADR_MAP.get(ticker)
    if us_ticker:
        logger.info(
            "[us_ticker_resolver] Resolved %s → %s (hard-coded ADR map)",
            ticker, us_ticker,
        )
        return us_ticker

    return None


async def resolve_to_us_ticker_async(ticker: str) -> str | None:
    """Async version with yfinance API fallback.

    Resolution layers:
      1. Hard-coded ADR map (instant)
      2. yfinance exchange check — get company name, search for US ADR
      3. Returns None if no US equivalent found

    Caches results to avoid repeated API calls.
    """
    ticker = ticker.upper().strip()

    # Layer 1: Hard-coded map (instant)
    us_ticker = KNOWN_ADR_MAP.get(ticker)
    if us_ticker:
        logger.info(
            "[us_ticker_resolver] Resolved %s → %s (hard-coded ADR map)",
            ticker, us_ticker,
        )
        return us_ticker

    # Layer 2: yfinance — get company name, then search for US listing
    try:
        import yfinance as yf

        def _yf_resolve():
            try:
                foreign_info = yf.Ticker(ticker).info
                company_name = foreign_info.get("shortName") or foreign_info.get("longName")
                if not company_name:
                    return None

                # Strip common suffixes for better search
                search_name = company_name
                for suffix in [" Inc", " Inc.", " Corp", " Corp.", " Ltd", " Ltd.",
                               " Co.", " Co", " PLC", " plc", " AG", " SA", " SE",
                               " NV", " N.V.", " Group", " Holdings"]:
                    search_name = search_name.replace(suffix, "")
                search_name = search_name.strip()

                if not search_name:
                    return None

                # Search yfinance for the company name
                search_results = yf.Search(search_name)
                quotes = getattr(search_results, "quotes", [])
                if not quotes:
                    return None

                # Look for US-listed result
                for quote in quotes:
                    q_exchange = quote.get("exchange", "")
                    q_symbol = quote.get("symbol", "")
                    q_type = quote.get("quoteType", "")

                    # Must be on a US exchange and be equity/ETF
                    if q_exchange in US_EXCHANGES and q_type in ("EQUITY", "ETF"):
                        # Avoid matching back to the same foreign ticker
                        if q_symbol.upper() != ticker:
                            return q_symbol.upper()

                return None
            except Exception as e:
                logger.warning(
                    "[us_ticker_resolver] yfinance resolve failed for %s: %s",
                    ticker, e,
                )
                return None

        result = await asyncio.wait_for(
            asyncio.to_thread(_yf_resolve),
            timeout=15.0,
        )

        if result:
            logger.info(
                "[us_ticker_resolver] Resolved %s → %s (yfinance search)",
                ticker, result,
            )
            # Cache it in the hard-coded map for future lookups within this process
            KNOWN_ADR_MAP[ticker] = result
            return result

    except asyncio.TimeoutError:
        logger.warning("[us_ticker_resolver] yfinance resolve timed out for %s", ticker)
    except Exception as e:
        logger.warning("[us_ticker_resolver] yfinance resolve error for %s: %s", ticker, e)

    logger.info(
        "[us_ticker_resolver] No US equivalent found for %s", ticker,
    )
    return None


async def verify_us_exchange(ticker: str) -> bool:
    """Verify a ticker is actually listed on a US exchange via yfinance.

    More expensive than is_us_tradeable() (makes an API call) but definitive.
    Use sparingly — primarily for tickers that pass format checks but might
    be delisted or foreign.
    """
    try:
        import yfinance as yf

        def _check():
            info = yf.Ticker(ticker).info
            exchange = info.get("exchange", "")
            return exchange in US_EXCHANGES

        return await asyncio.wait_for(
            asyncio.to_thread(_check),
            timeout=10.0,
        )
    except Exception as e:
        logger.warning("[us_ticker_resolver] Exchange verification failed for %s: %s", ticker, e)
        return False


def resolve_tickers_batch(tickers: list[str]) -> list[str]:
    """Synchronous batch resolve: filter/resolve a list of tickers.

    Returns only US-tradeable tickers, resolving foreign ones where possible.
    Uses only the hard-coded map (no API calls) for speed.
    """
    resolved = []
    for ticker in tickers:
        t = ticker.upper().strip()
        if is_us_tradeable(t):
            resolved.append(t)
        else:
            us_alt = resolve_to_us_ticker(t)
            if us_alt:
                logger.info(
                    "[us_ticker_resolver] Batch resolved %s → %s", t, us_alt
                )
                resolved.append(us_alt)
            else:
                logger.warning(
                    "[us_ticker_resolver] Dropped non-US ticker %s (no US equivalent)", t
                )
    return resolved


async def resolve_tickers_batch_async(tickers: list[str]) -> list[str]:
    """Async batch resolve: filter/resolve a list of tickers.

    Uses yfinance API fallback for tickers not in the hard-coded map.
    """
    resolved = []
    for ticker in tickers:
        t = ticker.upper().strip()
        if is_us_tradeable(t):
            resolved.append(t)
        else:
            us_alt = await resolve_to_us_ticker_async(t)
            if us_alt:
                resolved.append(us_alt)
            else:
                logger.warning(
                    "[us_ticker_resolver] Dropped non-US ticker %s (no US equivalent)", t
                )
    return resolved
