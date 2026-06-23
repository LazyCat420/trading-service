"""
MongoDB Connection & Collection management for the Civilization Council.
"""

import logging
import pymongo
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from app.config import settings

logger = logging.getLogger(__name__)

_mongo_client: Optional[pymongo.MongoClient] = None

def get_mongo_client() -> pymongo.MongoClient:
    """Return the global MongoClient instance, initializing it if necessary."""
    global _mongo_client
    if _mongo_client is None:
        logger.info(f"[Mongo] Connecting to MongoDB: {settings.PRISM_MONGO_URI}")
        _mongo_client = pymongo.MongoClient(settings.PRISM_MONGO_URI, serverSelectionTimeoutMS=5000)
    return _mongo_client

def get_mongo_db() -> pymongo.database.Database:
    """Return the Database object for the configured database name."""
    client = get_mongo_client()
    db_name = settings.PRISM_MONGO_DB or "prism"
    return client[db_name]

def init_mongo_schema():
    """Create collections and indexes for the Civilization Council if they do not exist."""
    try:
        db = get_mongo_db()
        
        # 1. agent_trust_scores
        col_scores = db["agent_trust_scores"]
        col_scores.create_index("role", unique=True)
        
        # Seed initial trust scores if empty (DELETED - Legacy)
        
        # 2. debate_transcripts
        db["debate_transcripts"].create_index([("ticker", pymongo.ASCENDING), ("cycle_id", pymongo.ASCENDING)])
        db["debate_transcripts"].create_index("timestamp")

        # 3. nomination_history
        db["nomination_history"].create_index([("ticker", pymongo.ASCENDING), ("cycle_id", pymongo.ASCENDING)])

        # 4. challenge_log
        db["challenge_log"].create_index([("ticker", pymongo.ASCENDING), ("cycle_id", pymongo.ASCENDING)])

        # 5. dissent_log
        db["dissent_log"].create_index([("ticker", pymongo.ASCENDING), ("cycle_id", pymongo.ASCENDING)])

        # 6. agent_configs
        db["agent_configs"].create_index([("role", pymongo.ASCENDING), ("version", pymongo.ASCENDING)])

        # 7. post_mortems
        db["post_mortems"].create_index([("role", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])

        logger.info("[Mongo] Civilization Council collections initialized successfully.")
    except Exception as e:
        logger.error(f"[Mongo] Failed to initialize MongoDB collections: {e}")
