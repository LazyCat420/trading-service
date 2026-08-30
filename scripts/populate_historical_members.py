"""Populate congress_members from the HISTORICAL legislators feed.

Only members whose latest term ended on or after 2018-01-01 — older ones cannot
appear in the disclosure feeds this database joins against. Converted off
Postgres 2026-08-30; parsing and storage are shared with the current populator.
"""
import requests
import yaml

from scripts.congress_members_common import HISTORICAL_URL, store_members

#: Anyone whose last term ended before this cannot show up in congress_trades.
MIN_TERM_END = "2018-01-01"


def populate_historical():
    print("Fetching historical legislators...")
    historical = yaml.safe_load(requests.get(HISTORICAL_URL).text)
    n = store_members(historical, min_term_end=MIN_TERM_END)
    print(f"Successfully populated historical members ({n} rows).")


if __name__ == "__main__":
    populate_historical()
