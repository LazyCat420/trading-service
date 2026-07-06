import requests
import cloudscraper
import os
import psycopg2
from bs4 import BeautifulSoup
import hashlib
import time
import random

DB_URL = os.environ.get("DATABASE_URL", "postgres://admin:admin_password@db:5432/trading_db")

def get_politician_ids():
    print("Fetching active legislators...")
    r = requests.get("https://raw.githubusercontent.com/theunitedstates/congress-legislators/main/legislators-current.json")
    current = r.json()
    ids = [p["id"]["bioguide"] for p in current if "bioguide" in p["id"]]
    
    print("Fetching historical legislators...")
    r2 = requests.get("https://raw.githubusercontent.com/theunitedstates/congress-legislators/main/legislators-historical.json")
    historical = r2.json()
    
    for p in historical:
        terms = p.get("terms", [])
        if not terms:
            continue
        last_term = terms[-1]
        end_date = last_term.get("end", "")
        if end_date >= "2020-01-01":
            if "bioguide" in p["id"]:
                ids.append(p["id"]["bioguide"])
                
    try:
        import json
        with open("../trading-client/app/utils/bioguide_map.json") as f:
            local_map = json.load(f)
            ids.extend(list(local_map.values()))
    except Exception as e:
        pass
        
    unique_ids = list(set(ids))
    print(f"Found {len(unique_ids)} unique politicians to scrape.")
    return unique_ids

def _parse_row(row):
    try:
        cols = row.find_all("td")
        if len(cols) < 6:
            return None

        politician_el = cols[0].find("h3")
        politician = politician_el.text.strip() if politician_el else cols[0].text.strip()

        meta_span = cols[0].find("span", class_="text-txt-muted")
        party, chamber, state = "", "", ""
        if meta_span:
            meta_parts = [p.strip() for p in meta_span.text.split()]
            if len(meta_parts) >= 3:
                party = meta_parts[0]
                chamber = meta_parts[1]
                state = meta_parts[2]

        issuer_el = cols[1].find("h3")
        ticker_span = cols[1].find("span", class_="q-field issuer-ticker")
        ticker = ticker_span.text.split(":")[0].strip() if ticker_span else ""

        asset_span = cols[1].find("span", class_="q-field asset-type")
        if asset_span and "stock" not in asset_span.text.lower():
            return None

        trade_date_el = cols[2].find("div", class_="text-txt-brand")
        trade_date = trade_date_el.text.strip() if trade_date_el else None
        
        disclosure_date_el = cols[3].find("div", class_="text-txt-brand")
        disclosure_date = disclosure_date_el.text.strip() if disclosure_date_el else None
        
        tx_type_el = cols[4].find("span", class_="q-field tx-type")
        tx_type = tx_type_el.text.strip() if tx_type_el else ""
        
        amount_el = cols[5].find("span", class_="q-field trade-size")
        amount = amount_el.text.strip() if amount_el else ""

        return {
            "politician": politician,
            "party": party,
            "chamber": chamber,
            "state": state,
            "ticker": ticker,
            "transaction_type": tx_type.lower(),
            "amount_range": amount,
            "trade_date": trade_date,
            "disclosure_date": disclosure_date,
            "days_to_disclose": 0
        }
    except Exception as e:
        return None

def main():
    conn = psycopg2.connect(DB_URL)
    db = conn.cursor()
    
    ids = get_politician_ids()
    scraper = cloudscraper.create_scraper()
    
    total_trades = 0
    random.shuffle(ids)
    
    for idx, bio_id in enumerate(ids):
        print(f"[{idx+1}/{len(ids)}] Scraping {bio_id}...")
        page = 1
        pol_trades = 0
        while True:
            params = {
                "politician": bio_id,
                "txType": ["buy", "sell"],
                "assetType": "stock",
                "page": page
            }
            try:
                r = scraper.get("https://www.capitoltrades.com/trades", params=params, timeout=20)
                if r.status_code != 200:
                    print(f"  HTTP {r.status_code} on page {page}")
                    break
                    
                soup = BeautifulSoup(r.text, "html.parser")
                rows = soup.select("table tbody tr")
                
                if not rows:
                    break
                    
                page_count = 0
                for row in rows:
                    trade = _parse_row(row)
                    if not trade or not trade.get("ticker"):
                        continue
                        
                    if trade["trade_date"] and "201" in trade["trade_date"]:
                        continue
                        
                    trade_id = hashlib.md5(
                        f"{trade['politician']}{trade['ticker']}{trade['trade_date']}{trade['transaction_type']}".encode()
                    ).hexdigest()

                    db.execute(
                        """
                        INSERT INTO congress_trades
                        (id, politician, party, chamber, state, ticker,
                         transaction_type, amount_range, trade_date,
                         disclosure_date, days_to_disclose)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        [
                            trade_id, trade["politician"], trade["party"], trade["chamber"], trade["state"],
                            trade["ticker"], trade["transaction_type"], trade["amount_range"], 
                            trade["trade_date"], trade["disclosure_date"], trade["days_to_disclose"]
                        ]
                    )
                    page_count += 1
                    
                conn.commit()
                pol_trades += page_count
                total_trades += page_count
                
                if len(rows) < 50:
                    break
                    
                page += 1
                time.sleep(random.uniform(0.5, 1.5))
                
            except Exception as e:
                print(f"  Error on page {page}: {e}")
                break
                
        if pol_trades > 0:
            print(f"  => Found {pol_trades} trades for {bio_id}")
            
        time.sleep(random.uniform(1.0, 2.0))
        
    print(f"\nBackfill complete! Added {total_trades} trades.")
    
if __name__ == "__main__":
    main()
