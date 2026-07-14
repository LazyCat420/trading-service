import re
import difflib
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Cache of all current members to avoid hitting DB repeatedly during match loop
_members_cache: Optional[Dict[str, str]] = None # bioguide_id -> normalized full_name/last_name info

def clean_politician_name(name: str) -> str:
    """Strip common prefixes, suffixes, and cleanup spacing."""
    if not name:
        return ""
    # Remove Hon., Sen., Rep., Mr., Mrs., Ms.
    cleaned = re.sub(r"^(Hon\.|Sen\.|Rep\.|Mr\.|Mrs\.|Ms\.)\s+", "", name, flags=re.IGNORECASE)
    cleaned = cleaned.replace("Hon ", "").replace("Sen ", "").replace("Rep ", "")
    # Remove JR, SR, III etc suffixes if comma separated
    cleaned = re.split(r",\s*(Jr\.|Sr\.|III|II|IV)\b", cleaned, flags=re.IGNORECASE)[0]
    return cleaned.strip()

def _load_members_cache(db) -> Dict[str, dict]:
    global _members_cache
    if _members_cache is not None:
        return _members_cache
    
    rows = db.execute(
        "SELECT bioguide_id, full_name, last_name, chamber FROM congress_members"
    ).fetchall()
    
    cache = {}
    for r in rows:
        bio_id, full_name, last_name, chamber = r
        cache[bio_id] = {
            "full_name": full_name.lower() if full_name else "",
            "last_name": last_name.lower() if last_name else "",
            "chamber": chamber.lower() if chamber else "",
        }
    _members_cache = cache
    return _members_cache

def resolve_bioguide_id(db, raw_name: str) -> Optional[str]:
    """Matches a raw politician name against congress_members and returns a bioguide_id."""
    if not raw_name:
        return None
        
    cleaned = clean_politician_name(raw_name).lower()
    if not cleaned:
        return None
        
    cache = _load_members_cache(db)
    
    # 1. Direct exact match on full_name
    for bio_id, m in cache.items():
        if m["full_name"] == cleaned:
            return bio_id

    # 2. Try match where cleaned name is contained in full_name or vice versa
    for bio_id, m in cache.items():
        if cleaned in m["full_name"] or m["full_name"] in cleaned:
            # Simple check: the last name must match
            if m["last_name"] in cleaned:
                return bio_id

    # 3. Match by last name if it is unique
    matching_by_last = []
    for bio_id, m in cache.items():
        if m["last_name"] and m["last_name"] == cleaned:
            matching_by_last.append(bio_id)
        elif m["last_name"] and cleaned.endswith(m["last_name"]):
            # e.g., "mitch mcconnell" ends with "mcconnell"
            matching_by_last.append(bio_id)
            
    if len(matching_by_last) == 1:
        return matching_by_last[0]
        
    # 4. Fuzzy match on full names using difflib
    full_name_to_id = {m["full_name"]: bio_id for bio_id, m in cache.items() if m["full_name"]}
    matches = difflib.get_close_matches(cleaned, full_name_to_id.keys(), n=1, cutoff=0.75)
    if matches:
        return full_name_to_id[matches[0]]
        
    return None
