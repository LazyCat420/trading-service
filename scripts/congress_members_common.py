"""Shared parse-and-store for the congress_members feed.

`populate_members.py` (current legislators) and `populate_historical_members.py`
(historical, recent terms only) carried byte-identical copies of the term
parsing and the upsert. Converting both off Postgres on 2026-08-30 would have
meant writing the same Mongo call twice, which is how the two copies would
drift the next time either changes.
"""
from typing import Iterable, Optional

from app.db import mongo_store

#: The unitedstates/congress-legislators YAML feeds.
CURRENT_URL = ("https://raw.githubusercontent.com/unitedstates/"
               "congress-legislators/main/legislators-current.yaml")
HISTORICAL_URL = ("https://raw.githubusercontent.com/unitedstates/"
                  "congress-legislators/main/legislators-historical.yaml")

_CHAMBER = {"rep": "House", "sen": "Senate"}


def member_doc(p: dict, *, min_term_end: Optional[str] = None) -> Optional[dict]:
    """One congress_members row from a feed entry, or None to skip it.

    `min_term_end` keeps the historical feed's "active recently enough" filter
    where it belongs — in the caller's argument, not in a forked copy of this
    function.
    """
    bio_id = p["id"].get("bioguide")
    if not bio_id:
        return None

    terms = p.get("terms", [])
    if not terms:
        return None
    latest = terms[-1]

    if min_term_end and (latest.get("end", "") or "") < min_term_end:
        return None

    name = p["name"]
    return {
        "bioguide_id": bio_id,
        # `first_name` is present on the stored rows but was NOT in the old
        # 6-column INSERT, so a PG re-run left it null on anything it created.
        # The feed has it; write it.
        "first_name": name.get("first", ""),
        "full_name": f"{name.get('first', '')} {name.get('last', '')}".strip(),
        "last_name": name.get("last", ""),
        "party": latest.get("party", ""),
        "chamber": _CHAMBER.get(latest.get("type", ""), latest.get("type", "")),
        "state": latest.get("state", ""),
    }


def store_members(entries: Iterable[dict], *, min_term_end: Optional[str] = None) -> int:
    """Upsert the feed into congress_members, keyed on bioguide_id.

    ON CONFLICT (bioguide_id) DO UPDATE SET ... -> a plain $set bulk_upsert:
    the feed is the authority for these fields, so a re-run SHOULD refresh
    party/chamber/state when someone changes seat.
    """
    # NOTE: stored rows also carry `collected_at`, which no reader in app/
    # consults and neither this script nor congress_collector writes. Left
    # alone deliberately — $set preserves it on existing rows — and recorded
    # as a candidate for the dead-column sweep rather than propagated.
    docs = [d for d in (member_doc(p, min_term_end=min_term_end) for p in entries) if d]
    if docs:
        mongo_store.bulk_upsert("congress_members", docs, key_field="bioguide_id")
    return len(docs)
