"""Postgres table name -> physical Mongo collection name.

The single indirection that makes the rename affordable. Until now the
collection name and the migration flag key were the same string --
`vector_store.py` said so outright:

    _TABLE = "embeddings"  # flag key in MONGO_STORE_BACKEND and Mongo collection name

Breaking that conflation is the whole design. The migration-era flag map and
write guard are gone (2026-08-19), but the ledger, the generated specs and
every call site still key on the POSTGRES TABLE NAME. Only the physical
collection is renamed, and only here. So every existing call site keeps
passing a table name and changes zero lines, while the stored collections get
standardized
names today rather than never.

Why now: 145 of the 158 collections do not exist yet, because their tables are
still at mode `pg`. Renaming them is a JSON edit that moves no bytes. After
cutover the same rename is 158 `renameCollection` calls against live traffic.

The map is HAND-AUTHORED and machine-validated, deliberately unlike
migration_ledger.json -- that one is generated, so a hand correction to it is
silently undone by the next regeneration.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

_MAP_PATH = os.path.join(os.path.dirname(__file__), "collection_map.json")

VALID_PREFIXES = ("log_", "state_", "ts_", "ref_", "q_", "ledger_")


@lru_cache(maxsize=1)
def _map() -> dict:
    with open(_MAP_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _collections() -> dict[str, dict]:
    return _map()["collections"]


def renames_active() -> bool:
    """Are the new names in force, or is the map still inert?

    The map ships INERT (`apply_renames: false`) and every lookup returns the
    table name unchanged. That is deliberate, and it is the only safe way to
    land this:

    A rename is not a code change, it is a data move. The 13 collections that
    hold data are named for their tables today. The moment `collection_for`
    starts returning `log_pipeline_events`, a running container reads and
    writes *that* collection -- which does not exist -- and Mongo creates it
    empty on first write rather than erroring. The dashboard would show no
    events, the mirror would write to a second invisible collection, and
    nothing would raise.

    So flipping this flag is a coordinated operation, not a deploy:
      1. both repos ship the resolver with the flag still false
      2. stop both containers
      3. renameCollection x13 (metadata-only, milliseconds, even for the
         372k-document one)
      4. flip the flag, deploy both in the same window
    Until step 4, this returns False and the physical layer is untouched.
    """
    return bool(_map().get("apply_renames", False))


def collection_for(table: str) -> str:
    """The physical collection a table's documents live in.

    While the map is inert this is the identity function -- see renames_active.

    Falls back to the table name for anything not in the map -- the `prism`-DB
    collections written by app/db/mongo.py, scratch collections in tests, and
    the 25 archive-only tables. Raising here instead would turn an unmapped
    read into an outage, and the CI check (scripts/check_collection_map.py)
    already fails the build on any *mapped* table that drifts, which is the
    case that actually matters.
    """
    if not renames_active():
        return table
    entry = _collections().get(table)
    return entry["collection"] if entry else table


def target_collection_for(table: str) -> str:
    """The name this table WILL use once the renames are activated.

    Separate from collection_for so the rename tooling and the CI checks can
    see the target while the live path is still the identity function.
    """
    entry = _collections().get(table)
    return entry["collection"] if entry else table


def is_mapped(table: str) -> bool:
    return table in _collections()


def id_fields_for(table: str) -> list[str]:
    """The natural key, as a list. Empty when the ledger recorded none."""
    entry = _collections().get(table)
    return list(entry.get("id_fields") or []) if entry else []


def numeric_policy_for(table: str) -> str:
    """`dec128` or `float`. Every ledger_* collection is dec128 by construction."""
    entry = _collections().get(table)
    return entry.get("numeric_policy", "float") if entry else "float"


def all_tables() -> list[str]:
    return sorted(_collections())


def all_collections() -> list[str]:
    return sorted(e["collection"] for e in _collections().values())


def archive_only() -> list[str]:
    return list(_map().get("archive_only", []))


def foreign_tables() -> list[str]:
    return list(_map().get("foreign", []))


def reset_cache() -> None:
    """Drop the cached map. For tests that rewrite the file."""
    _map.cache_clear()
