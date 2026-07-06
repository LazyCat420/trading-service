import urllib.request
import json
import psycopg
import os
import hashlib
from datetime import datetime

DB_URL = os.environ.get("DATABASE_URL", "postgres://admin:admin_password@db:5432/trading_db")

def parse_date(date_str):
    if not date_str or date_str == "--":
        return None
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except:
        return date_str

def parse_type(type_str):
    t = type_str.lower()
    if "purchase" in t:
        return "buy"
    if "sale" in t:
        return "sell"
    return "exchange"

def backfill():
    conn = psycopg.connect(DB_URL, autocommit=True)
    db = conn.cursor()
    
    total = 0

    print("Fetching House data...")
    house_url = "https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data/main/data/all_transactions.json"
    req = urllib.request.Request(house_url, headers={'User-Agent': 'Mozilla/5.0'})
    house_data = json.loads(urllib.request.urlopen(req).read())
    
    for row in house_data:
        ticker = row.get("ticker")
        if not ticker or ticker == "--" or ticker == "N/A":
            continue
            
        pol = row.get("representative", "")
        t_type = parse_type(row.get("type", ""))
        amount = row.get("amount", "")
        t_date = parse_date(row.get("transaction_date"))
        d_date = parse_date(row.get("disclosure_date"))
        party = "House"
        chamber = "House"
        state = row.get("district", "")

        trade_id = hashlib.md5(f"{pol}{ticker}{t_date}{t_type}".encode()).hexdigest()
        
        db.execute(
            """
            INSERT INTO congress_trades 
            (id, politician, party, chamber, state, ticker, 
             transaction_type, amount_range, trade_date, 
             disclosure_date, days_to_disclose) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            [trade_id, pol, party, chamber, state, ticker, t_type, amount, t_date, d_date, 0]
        )
        total += 1

    print("Fetching Senate data...")
    senate_url = "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json"
    req = urllib.request.Request(senate_url, headers={'User-Agent': 'Mozilla/5.0'})
    senate_data = json.loads(urllib.request.urlopen(req).read())
    
    for row in senate_data:
        ticker = row.get("ticker")
        if not ticker or ticker == "--" or ticker == "N/A":
            continue
            
        pol = row.get("senator", "")
        t_type = parse_type(row.get("type", ""))
        amount = row.get("amount", "")
        t_date = parse_date(row.get("transaction_date"))
        d_date = None
        party = "Senate"
        chamber = "Senate"
        state = ""

        trade_id = hashlib.md5(f"{pol}{ticker}{t_date}{t_type}".encode()).hexdigest()
        
        db.execute(
            """
            INSERT INTO congress_trades 
            (id, politician, party, chamber, state, ticker, 
             transaction_type, amount_range, trade_date, 
             disclosure_date, days_to_disclose) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            [trade_id, pol, party, chamber, state, ticker, t_type, amount, t_date, d_date, 0]
        )
        total += 1

    print(f"Backfill complete! Processed {total} trades.")

if __name__ == "__main__":
    backfill()
