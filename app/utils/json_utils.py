"""Coercion for columns that hold JSON as TEXT.

The Mongo cutover left a family of fields that are a JSON *string* in some
documents and a real sub-document in others -- `result_json`, `desk_data` and
friends -- because the Postgres schema stored them as text and the migrated
rows kept that shape while new writers store documents. Every reader therefore
has to accept both, and three separate copies of this coercion had grown up in
verdict_service, debate_service and morning_briefing.

The copies disagreed on their except clause: two caught only
(JSONDecodeError, TypeError) or a bare Exception around json.loads, and one
refused to call json.loads at all unless the value was a non-blank str. This
version takes the union -- anything that is not a dict or a usable string
returns {} without an attempt, and the attempt itself catches broadly, because
a malformed stored field must never take down the read.
"""
import json


def parse_json_field(value) -> dict:
    """Return `value` as a dict: pass through documents, decode JSON text.

    Anything unusable -- None, a blank string, a list, a number, undecodable
    text -- comes back as an empty dict. Callers treat a missing field and a
    corrupt one identically, and always have.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)) and str(value).strip():
        try:
            decoded = json.loads(value)
        except Exception:  # noqa: BLE001 — a bad stored field is not a crash
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}
