"""Populate congress_members from the CURRENT legislators feed.

Converted off Postgres 2026-08-30; parsing and storage now live in
`scripts/congress_members_common.py`, shared with the historical populator.
"""
import requests
import yaml

from scripts.congress_members_common import CURRENT_URL, store_members


def populate_members():
    print("Fetching current legislators...")
    current = yaml.safe_load(requests.get(CURRENT_URL).text)
    n = store_members(current)
    print(f"Successfully populated congress_members ({n} rows).")


if __name__ == "__main__":
    populate_members()
