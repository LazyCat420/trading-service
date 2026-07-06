import logging
import asyncio
import httpx
from app.db.connection import get_db

logger = logging.getLogger(__name__)

async def collect_congress_members() -> int:
    """Fetch current congress members from theunitedstates.io and upsert to congress_members table."""
    url = "https://theunitedstates.io/congress-legislators/legislators-current.json"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        members = resp.json()

    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS congress_members (
                bioguide_id VARCHAR PRIMARY KEY,
                first_name VARCHAR,
                last_name VARCHAR,
                full_name VARCHAR,
                party VARCHAR,
                chamber VARCHAR,
                state VARCHAR,
                collected_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        count = 0
        for m in members:
            try:
                bioguide_id = m["id"]["bioguide"]
                first_name = m["name"]["first"]
                last_name = m["name"]["last"]
                full_name = f"{first_name} {last_name}"
                
                # Get current term
                current_term = m["terms"][-1]
                party = current_term.get("party", "Unknown")
                chamber = "Senate" if current_term.get("type") == "sen" else "House"
                state = current_term.get("state", "")
                
                db.execute("""
                    INSERT INTO congress_members (
                        bioguide_id, first_name, last_name, full_name, party, chamber, state, collected_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (bioguide_id) DO UPDATE SET
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        full_name = EXCLUDED.full_name,
                        party = EXCLUDED.party,
                        chamber = EXCLUDED.chamber,
                        state = EXCLUDED.state,
                        collected_at = CURRENT_TIMESTAMP
                """, [bioguide_id, first_name, last_name, full_name, party, chamber, state])
                count += 1
            except Exception as e:
                logger.error(f"Error parsing member {m}: {e}")
                
        logger.info(f"Inserted/updated {count} congress members.")
        return count
