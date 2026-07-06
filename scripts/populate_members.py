import requests
import yaml
import os
import psycopg

DB_URL = os.environ.get("DATABASE_URL", "postgres://admin:admin_password@db:5432/trading_db")

def populate_members():
    print("Fetching active legislators...")
    r = requests.get("https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-current.yaml")
    current = yaml.safe_load(r.text)
    
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            for p in current:
                bio_id = p["id"].get("bioguide")
                if not bio_id:
                    continue
                
                name = p["name"]
                full_name = f"{name.get('first', '')} {name.get('last', '')}".strip()
                last_name = name.get('last', '')
                
                terms = p.get("terms", [])
                if not terms:
                    continue
                
                latest_term = terms[-1]
                party = latest_term.get("party", "")
                chamber = latest_term.get("type", "")
                if chamber == "rep":
                    chamber = "House"
                elif chamber == "sen":
                    chamber = "Senate"
                state = latest_term.get("state", "")
                
                cur.execute("""
                    INSERT INTO congress_members (bioguide_id, full_name, last_name, party, chamber, state)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (bioguide_id) DO UPDATE SET
                        full_name = EXCLUDED.full_name,
                        last_name = EXCLUDED.last_name,
                        party = EXCLUDED.party,
                        chamber = EXCLUDED.chamber,
                        state = EXCLUDED.state
                """, (bio_id, full_name, last_name, party, chamber, state))
        conn.commit()
    print("Successfully populated congress_members.")

if __name__ == "__main__":
    populate_members()
