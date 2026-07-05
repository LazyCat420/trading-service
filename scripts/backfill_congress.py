import sys
import asyncio
import logging
sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO)

from app.collectors.congress_collector import collect_trades

async def main():
    print("Starting Congress Backfill (300 pages)...")
    await collect_trades(pages=300)
    print("Backfill complete.")

if __name__ == "__main__":
    asyncio.run(main())
