#!/usr/bin/env python3
"""Backfill congress_trades.bioguide_id from the politician name.

Converted off Postgres 2026-08-30. It read and UPDATEd the frozen archive, so
it would have reported "N mapped" while every live row kept a null
bioguide_id. The matcher itself was already on Mongo — only this caller and
its now-removed `db` argument were left behind.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db import mongo_query, mongo_store  # noqa: E402
from app.utils.politician_matcher import resolve_bioguide_id  # noqa: E402


def main():
    print("Starting Congress Trades backfill for bioguide_id...")

    trades = mongo_query.find_rows("congress_trades", {}, ["id", "politician"])
    print(f"Loaded {len(trades)} trades from database.")

    mapped_count = 0
    unmapped_count = 0
    # One bulk write rather than ~30k round-trips; only the id and the resolved
    # bioguide_id are touched, so no other column can be clobbered.
    updates: list[dict] = []
    for trade_id, politician in trades:
        bio_id = resolve_bioguide_id(politician)
        if bio_id:
            updates.append({"id": trade_id, "bioguide_id": bio_id})
            mapped_count += 1
        else:
            unmapped_count += 1

    if updates:
        mongo_store.bulk_upsert("congress_trades", updates, key_field="id")

    print(f"Backfill finished: {mapped_count} mapped, {unmapped_count} unmapped.")


if __name__ == "__main__":
    main()
