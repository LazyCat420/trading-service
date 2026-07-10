import sys
import os
import yfinance as yf
import pandas as pd
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# Add parent dir to path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.db.connection import get_db

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
    
    with get_db() as db:
        # Upsert into ticker_metadata
        for r in results:
            db.execute("""
                INSERT INTO ticker_metadata (ticker, sector, market_cap, sp500)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (ticker) DO UPDATE SET 
                    sector = EXCLUDED.sector,
                    market_cap = EXCLUDED.market_cap,
                    sp500 = TRUE
            """, (r['ticker'], r['sector'], r['market_cap']))
            
            # Upsert into price_history
            for p in r['prices']:
                db.execute("""
                    INSERT INTO price_history (ticker, date, open, close, high, low, volume, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'yfinance')
                    ON CONFLICT (ticker, date, source) DO UPDATE SET 
                        open = EXCLUDED.open,
                        close = EXCLUDED.close,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        volume = EXCLUDED.volume
                """, (r['ticker'], p['date'], p['open'], p['close'], p['high'], p['low'], p['volume']))
                
        db.commit()
    
    print("Database population complete!")

if __name__ == "__main__":
    main()
