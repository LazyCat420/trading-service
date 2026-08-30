"""Backfill congress_trades from the House and Senate stock-watcher feeds.

Converted off Postgres 2026-08-30. The two INSERTs below wrote to the frozen
archive, so a re-run would have populated a store the cycle does not read while
appearing to succeed. `ON CONFLICT (id) DO NOTHING` is preserved exactly, via
`bulk_upsert(insert_only=True)`: the id is a content hash of
politician+ticker+date+type, and the row deliberately keeps the FIRST
disclosure seen rather than the latest re-scrape.
"""
import urllib.request
import json
import hashlib
from datetime import datetime

from app.db import mongo_store

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

def _trade_doc(pol, party, chamber, state, ticker, t_type, amount, t_date, d_date):
    """One congress_trades row. The id is content-addressed, which is what makes
    the whole backfill safe to re-run."""
    trade_id = hashlib.md5(f"{pol}{ticker}{t_date}{t_type}".encode()).hexdigest()
    return {
        "id": trade_id, "politician": pol, "party": party, "chamber": chamber,
        "state": state, "ticker": ticker, "transaction_type": t_type,
        "amount_range": amount, "trade_date": t_date,
        "disclosure_date": d_date, "days_to_disclose": 0,
    }


def backfill():
    docs: list[dict] = []
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

        docs.append(_trade_doc(pol, party, chamber, state, ticker,
                               t_type, amount, t_date, d_date))
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

        docs.append(_trade_doc(pol, party, chamber, state, ticker,
                               t_type, amount, t_date, d_date))
        total += 1

    # One round-trip instead of ~30k. insert_only mirrors DO NOTHING.
    written = mongo_store.bulk_upsert("congress_trades", docs, key_field="id",
                                      insert_only=True)
    print(f"Backfill complete! Processed {total} trades, submitted {written}.")

if __name__ == "__main__":
    backfill()
