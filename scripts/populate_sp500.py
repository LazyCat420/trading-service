import sys
import os
import yfinance as yf
import pandas as pd
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# Add parent dir to path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.db import mongo_store

def fetch_sp500_tickers():
    print("Fetching S&P 500 ticker list from Wikipedia...")
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    try:
        import requests
        import io
        html = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}).text
        tables = pd.read_html(io.StringIO(html))
        df = tables[0]
    except ImportError:
        print("lxml not installed. Falling back to alternative...")
        # read_html sometimes needs lxml or html5lib. 
        # If it fails, let's just abort for now or install it.
        raise

    # Clean tickers (e.g. BRK.B -> BRK-B for yfinance)
    tickers = df['Symbol'].str.replace('.', '-').tolist()
    sectors = df['GICS Sector'].tolist()
    
    return list(zip(tickers, sectors))

def process_ticker(ticker, sector, start_date):
    """Fetch info and historical data for a single ticker."""
    try:
        t = yf.Ticker(ticker)
        
        # 1. Get Market Cap
        market_cap = t.fast_info.market_cap
        
        # 2. Get Historical Prices (last 35 days)
        hist = t.history(start=start_date.strftime('%Y-%m-%d'))
        
        prices = []
        for date, row in hist.iterrows():
            prices.append({
                "date": date.strftime('%Y-%m-%d'),
                "open": float(row['Open']),
                "close": float(row['Close']),
                "high": float(row['High']),
                "low": float(row['Low']),
                "volume": int(row['Volume'])
            })
            
        return {
            "ticker": ticker,
            "sector": sector,
            "market_cap": market_cap,
            "prices": prices,
            "success": True
        }
    except Exception as e:
        return {
            "ticker": ticker,
            "success": False,
            "error": str(e)
        }

def main():
    sp500 = fetch_sp500_tickers()
    start_date = datetime.now() - timedelta(days=40)
    
    results = []
    print(f"Fetching data for {len(sp500)} tickers using 20 threads. This may take a minute...")
    
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(process_ticker, t[0], t[1], start_date): t[0] for t in sp500}
        
        count = 0
        for future in as_completed(futures):
            count += 1
            res = future.result()
            if res["success"]:
                results.append(res)
            if count % 50 == 0:
                print(f"Processed {count}/{len(sp500)}...")

    print(f"Finished fetching data for {len(results)} tickers successfully. Writing to database...")
    
    # ON CONFLICT (ticker) DO UPDATE -> $set upsert keyed on ticker. Only the
    # three columns the old INSERT listed are written, so name / industry /
    # market_cap_tier on an existing row survive untouched ($set does not
    # remove unlisted fields) — the same guarantee EXCLUDED gave here.
    meta = [{"ticker": r["ticker"], "sector": r["sector"],
             "market_cap": r["market_cap"], "sp500": True} for r in results]
    mongo_store.bulk_upsert("ticker_metadata", meta, key_field="ticker")

    # ON CONFLICT (ticker, date, source) -> the COMPOSITE key bulk_upsert
    # already supports. One bulk write for every bar of every ticker, rather
    # than a round-trip per bar: 500 tickers x ~250 bars was 125k statements.
    bars = [{"ticker": r["ticker"], "date": p["date"], "source": "yfinance",
             "open": p["open"], "close": p["close"], "high": p["high"],
             "low": p["low"], "volume": p["volume"]}
            for r in results for p in r["prices"]]
    mongo_store.bulk_upsert("price_history", bars,
                            key_field=("ticker", "date", "source"))

    print(f"Database population complete! {len(meta)} tickers, {len(bars)} bars.")

if __name__ == "__main__":
    main()
