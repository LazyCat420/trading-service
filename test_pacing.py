import sys
import asyncio
import logging
sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO)

from app.collectors.congress_collector import collect_trades

async def main():
    print("Testing slow pacing (3 pages)...")
    await collect_trades(pages=3)
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
