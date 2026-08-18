import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator
from app.db.connection import get_db

logger = logging.getLogger(__name__)

def _grade(score: float) -> str:
    if score >= 0.95: return "excellent"
    if score >= 0.80: return "good"
    if score >= 0.60: return "fair"
    if score >= 0.30: return "poor"
    return "critical"

def _safe_iso(val) -> str | None:
    if val is None: return None
    if hasattr(val, "isoformat"): return val.isoformat()
    return str(val)
