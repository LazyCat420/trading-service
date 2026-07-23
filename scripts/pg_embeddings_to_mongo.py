#!/usr/bin/env python3
"""Migrate the pgvector `embeddings` table into the Mongo `embeddings`
collection (packed little-endian float32 BinData, matching
app/db/vector_store.py's Mongo backend), verify parity, and optionally
dedupe the dual-write collections that predate their unique indexes.

Standalone by design — needs only psycopg2, pymongo, numpy (no app imports),
so it can run from the dev box against the NAS or inside the container.

Usage:
  python scripts/pg_embeddings_to_mongo.py backfill   # copy + verify
  python scripts/pg_embeddings_to_mongo.py verify     # parity check only
  python scripts/pg_embeddings_to_mongo.py dedupe-dual  # drop dup-id docs in
        execution_errors / cycle_audit_log (keeps first _id per id)

Env (fall back to the standard NAS coordinates):
  PG_DSN     postgresql://trader:...@10.0.0.16:5433/trading_bot
  MONGO_URI  mongodb://...@10.0.0.16:27017
  MONGO_DB   trading_bot
"""

import os
import struct
import sys
import random

import numpy as np
import psycopg2
import pymongo
from bson import Binary

PG_DSN = os.getenv("PG_DSN") or os.getenv(
    "DATABASE_URL", "postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot"
).strip('"')
MONGO_URI = os.getenv("MONGO_URI") or os.getenv(
    "PRISM_MONGO_URI", "mongodb://sun:sun@10.0.0.16:27017/?directConnection=true"
).strip('"')
MONGO_DB = os.getenv("MONGO_DB", "trading_bot")

BATCH = 2000


def _parse_pgvector(text: str) -> list[float]:
    return [float(x) for x in text.strip()[1:-1].split(",")] if text and len(text) > 2 else []


def _mongo_coll():
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    return client[MONGO_DB]["embeddings"]


def backfill() -> None:
    coll = _mongo_coll()
    coll.create_index("id", unique=True)
    coll.create_index([("source_table", 1), ("source_id", 1)])
    coll.create_index([("ticker", 1), ("created_at", -1)])
    coll.create_index([("content_preview", "text")])

    pg = psycopg2.connect(PG_DSN)
    cur = pg.cursor()
    last_id = ""
    total = 0
    while True:
        cur.execute(
            """
            SELECT id, source_table, source_id, ticker, content_preview,
                   embedding::text, created_at
            FROM embeddings WHERE id > %s ORDER BY id ASC LIMIT %s
            """,
            [last_id, BATCH],
        )
        rows = cur.fetchall()
        if not rows:
            break
        ops = []
        for rid, st, sid, tkr, prev, emb_txt, created in rows:
            vec = _parse_pgvector(emb_txt)
            if not vec or not any(vec):
                continue
            ops.append(
                pymongo.UpdateOne(
                    {"id": rid},
                    {"$set": {
                        "id": rid, "source_table": st, "source_id": sid,
                        "ticker": tkr, "content_preview": prev,
                        "embedding": Binary(struct.pack(f"<{len(vec)}f", *vec)),
                        "dim": len(vec), "created_at": created,
                    }},
                    upsert=True,
                )
            )
        if ops:
            coll.bulk_write(ops, ordered=False)
        total += len(rows)
        last_id = rows[-1][0]
        print(f"  … {total} rows migrated (last id {last_id[:16]})", flush=True)
    pg.close()
    print(f"Backfill complete: {total} PG rows processed.")


def verify() -> bool:
    coll = _mongo_coll()
    pg = psycopg2.connect(PG_DSN)
    cur = pg.cursor()

    ok = True
    cur.execute("SELECT source_table, COUNT(*) FROM embeddings GROUP BY source_table ORDER BY 1")
    print("source_table          PG      Mongo")
    for st, n_pg in cur.fetchall():
        n_mongo = coll.count_documents({"source_table": st})
        flag = "" if n_mongo >= n_pg * 0.999 else "  <-- DRIFT"
        # Mongo may exceed PG slightly: live dual-writes land in both, but a
        # PG row deleted after copy stays in Mongo until re-upserted.
        if n_mongo < n_pg * 0.999:
            ok = False
        print(f"{st:20s} {n_pg:7d} {n_mongo:9d}{flag}")

    # Sample cosine parity: PG vector vs unpacked Mongo BinData ≈ identical.
    cur.execute("SELECT id, embedding::text FROM embeddings ORDER BY random() LIMIT 20")
    worst = 1.0
    missing = 0
    for rid, emb_txt in cur.fetchall():
        doc = coll.find_one({"id": rid}, {"embedding": 1})
        if not doc:
            missing += 1
            continue
        a = np.asarray(_parse_pgvector(emb_txt), dtype=np.float32)
        b = np.frombuffer(bytes(doc["embedding"]), dtype="<f4")
        cos = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
        worst = min(worst, cos)
    print(f"sample cosine parity: worst {worst:.6f} over 20 samples, {missing} missing")
    if worst < 0.9999 or missing:
        ok = False
    pg.close()
    print("VERIFY:", "OK" if ok else "FAILED")
    return ok


def dedupe_dual() -> None:
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    db = client[MONGO_DB]
    for coll_name in ("execution_errors", "cycle_audit_log"):
        coll = db[coll_name]
        pipeline = [
            {"$match": {"id": {"$ne": None}}},
            {"$group": {"_id": "$id", "keep": {"$first": "$_id"}, "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}},
        ]
        removed = 0
        for grp in coll.aggregate(pipeline, allowDiskUse=True):
            res = coll.delete_many({"id": grp["_id"], "_id": {"$ne": grp["keep"]}})
            removed += res.deleted_count
        print(f"{coll_name}: removed {removed} duplicate docs")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "backfill"
    if mode == "backfill":
        backfill()
        sys.exit(0 if verify() else 1)
    elif mode == "verify":
        sys.exit(0 if verify() else 1)
    elif mode == "dedupe-dual":
        dedupe_dual()
    else:
        print(__doc__)
        sys.exit(2)
