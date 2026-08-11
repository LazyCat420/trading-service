"""run_live_reddit_test.py — Live execution test for get_reddit_trending_stocks."""

import asyncio
import json
import logging

logging.basicConfig(level=logging.INFO)

from app.tools.reddit_tools import get_reddit_trending_stocks

async def main():
    print("Fetching live trending stocks from Reddit RSS...")
    raw_json = await get_reddit_trending_stocks(subreddits=["wallstreetbets", "stocks", "investing", "options"], per_subreddit=5, limit=15)
    data = json.loads(raw_json)
    
    print("\n" + "="*60)
    print(f"STATUS: {data.get('status')}")
    print(f"SUBREDDITS SCANNED: {', '.join(data.get('subreddits', []))}")
    print(f"TOTAL TICKERS FOUND: {data.get('count')}")
    print("="*60 + "\n")
    
    # No sentiment label is emitted any more -- the tool returns the posts
    # themselves and leaves the reading to the caller, because the only scorer
    # available counted lexicon words and inverted on ordinary sentences.
    # See app/tools/reddit_tools.py.
    stocks = data.get("stocks", [])
    for s in stocks:
        print(f"#{s['rank']} | Ticker: {s['ticker']:<5} | Mentions: {s['mention_score']:<3} | Posts: {s['post_count']}")
        for p in s.get("posts", []):
            print(f"   r/{p.get('subreddit', '')} \"{p.get('title', '')[:70]}\"")
            excerpt = (p.get("excerpt") or "").replace("\n", " ")
            if excerpt:
                print(f"      {excerpt[:110]}")
        print("-" * 60)

if __name__ == "__main__":
    asyncio.run(main())
