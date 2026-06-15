"""
defillama_collector.py — DeFi Protocol TVL, pegged stablecoins, and yields.
Public API, no keys needed.
"""
import logging
import datetime
import httpx
from app.db.connection import get_db

logger = logging.getLogger(__name__)

BASE_URL = "https://api.llama.fi"
STABLECOIN_URL = "https://stablecoins.llama.fi"
YIELDS_URL = "https://yields.llama.fi"

async def collect_protocol_tvl(limit: int = 100) -> int:
    """Fetch current TVL for top DeFi protocols and store in asset_prices.
    symbol = protocol name
    asset_class = 'defi_tvl'
    date = today
    close = tvl
    """
    url = f"{BASE_URL}/protocols"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            protocols = r.json()
    except Exception as e:
        logger.error(f"[defillama] Failed to fetch protocols: {e}")
        return 0

    if not isinstance(protocols, list):
        logger.error(f"[defillama] Invalid response format for protocols: {type(protocols)}")
        return 0

    protocols = [p for p in protocols if p.get("tvl") is not None]
    protocols.sort(key=lambda x: x["tvl"], reverse=True)
    top_protocols = protocols[:limit]

    date_today = datetime.datetime.now(datetime.timezone.utc).date()
    rows = []
    for p in top_protocols:
        name = p.get("name", "").strip()
        tvl = p.get("tvl")
        if name and tvl is not None:
            rows.append((name, "defi_tvl", date_today, tvl, tvl, tvl, tvl, 0.0, "defillama"))

    if rows:
        with get_db() as db:
            db.executemany("""
                INSERT INTO asset_prices
                (symbol, asset_class, date, open, high, low, close, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, asset_class, date) DO UPDATE 
                SET close = EXCLUDED.close, open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low
            """, rows)
        logger.info(f"[defillama] Wrote {len(rows)} protocol TVL rows to asset_prices")
        return len(rows)
    return 0

async def collect_stablecoin_supply() -> int:
    """Fetch stablecoin circulating supply and store in macro_indicators as symbol_MCAP."""
    url = f"{STABLECOIN_URL}/peggedassets?includePrices=true"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.error(f"[defillama] Failed to fetch stablecoins: {e}")
        return 0

    pegged_assets = data.get("peggedAssets", [])
    if not pegged_assets:
        logger.warning("[defillama] No stablecoin peggedAssets found")
        return 0

    date_today = datetime.datetime.now(datetime.timezone.utc).date()
    rows = []
    for asset in pegged_assets:
        symbol = asset.get("symbol", "").upper()
        circulating = asset.get("circulating", {})
        pegged_usd = circulating.get("peggedUSD")
        if symbol and pegged_usd is not None:
            indicator = f"{symbol}_MCAP"
            rows.append((indicator, date_today, float(pegged_usd), "GLOBAL", "defillama"))

    if rows:
        with get_db() as db:
            db.executemany("""
                INSERT INTO macro_indicators
                (indicator, date, value, country, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (indicator, date, country) DO UPDATE SET value = EXCLUDED.value
            """, rows)
        logger.info(f"[defillama] Wrote {len(rows)} stablecoin supply rows to macro_indicators")
        return len(rows)
    return 0

async def collect_yields(limit: int = 50) -> int:
    """Fetch top yield pools and store in asset_prices.
    symbol = project_poolname
    asset_class = 'defi_yield'
    date = today
    close = apy
    volume = tvlUsd
    """
    url = f"{YIELDS_URL}/pools"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.error(f"[defillama] Failed to fetch yields: {e}")
        return 0

    pools = data.get("data", [])
    if not pools:
        logger.warning("[defillama] No yields data found")
        return 0

    pools = [p for p in pools if p.get("tvlUsd") is not None and p.get("apy") is not None]
    pools.sort(key=lambda x: x["tvlUsd"], reverse=True)
    top_pools = pools[:limit]

    date_today = datetime.datetime.now(datetime.timezone.utc).date()
    rows = []
    for p in top_pools:
        project = p.get("project", "").strip()
        symbol = p.get("symbol", "").strip()
        apy = p.get("apy")
        tvl = p.get("tvlUsd")
        
        pool_name = f"{project}_{symbol}".upper()
        rows.append((pool_name, "defi_yield", date_today, apy, apy, apy, apy, float(tvl), "defillama"))

    if rows:
        with get_db() as db:
            db.executemany("""
                INSERT INTO asset_prices
                (symbol, asset_class, date, open, high, low, close, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, asset_class, date) DO UPDATE 
                SET close = EXCLUDED.close, volume = EXCLUDED.volume, open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low
            """, rows)
        logger.info(f"[defillama] Wrote {len(rows)} yields rows to asset_prices")
        return len(rows)
    return 0

async def collect_all() -> dict:
    """Run all DefiLlama collectors."""
    tvl_count = await collect_protocol_tvl()
    mcap_count = await collect_stablecoin_supply()
    yield_count = await collect_yields()
    return {
        "defi_tvl": tvl_count,
        "defi_stablecoin_mcap": mcap_count,
        "defi_yields": yield_count
    }
