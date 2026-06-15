# app/utils/payload_gate.py

from typing import Any
import logging
import json
import datetime

logger = logging.getLogger(__name__)

MINIMUM_FIELDS = {
    "market_data": ["ticker", "price", "volume"],
    "bull_bear":   ["ticker", "thesis", "confidence", "supporting_data"],
    "debate":      ["bull_case", "bear_case"],
    "synthesis":   ["net_signal", "confidence", "bull_case", "bear_case"],
}

class InsufficientDataError(Exception):
    def __init__(self, stage: str, missing: list):
        self.stage = stage
        self.missing = missing
        super().__init__(f"[GATE BLOCKED] Stage '{stage}' missing: {missing}")

def gate_check(payload: dict, stage: str) -> dict:
    """
    Hard gate — call this at EVERY agent handoff.
    Raises InsufficientDataError if required fields are empty/missing, or if a DATA_MISSING status/flag is present.
    Returns the payload unchanged if it passes.
    """
    if not isinstance(payload, dict):
        logger.error(f"[GATE BLOCKED] {stage} | Payload is not a dict: {type(payload)}")
        raise InsufficientDataError(stage, ["payload_not_dict"])

    # Check for DATA_MISSING status or proceed=False
    if payload.get("status") == "DATA_MISSING" or payload.get("proceed") is False:
        missing_fields = payload.get("missing_fields", ["unknown_fields"])
        logger.error(f"[GATE BLOCKED] {stage} | DATA_MISSING status reported. Missing: {missing_fields}")
        raise InsufficientDataError(stage, missing_fields)

    # Check for any value that starts with "DATA_MISSING" string
    for k, v in payload.items():
        if isinstance(v, str) and v.startswith("DATA_MISSING"):
            logger.error(f"[GATE BLOCKED] {stage} | DATA_MISSING text found in field '{k}': {v}")
            raise InsufficientDataError(stage, [k])

    required = MINIMUM_FIELDS.get(stage, [])
    missing = []
    for field in required:
        val = payload.get(field)
        if val is None or val == "" or val == [] or val == {}:
            missing.append(field)

    if missing:
        logger.error(f"[GATE BLOCKED] {stage} | missing/empty: {missing} | payload keys: {list(payload.keys())}")
        raise InsufficientDataError(stage, missing)

    return payload

def log_transition(from_stage: str, to_stage: str, payload_summary: dict, status: str):
    """
    Pipeline Audit Log — every stage transition writes a JSON log to stdout/logs.
    """
    try:
        # Avoid timezone-naive vs timezone-aware errors by using datetime.now(datetime.timezone.utc)
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "from": from_stage,
            "to": to_stage,
            "ticker": payload_summary.get("ticker"),
            "has_thesis": bool(payload_summary.get("thesis") or payload_summary.get("rationale")),
            "has_bull": bool(payload_summary.get("bull_case") or payload_summary.get("bull_claims")),
            "has_bear": bool(payload_summary.get("bear_case") or payload_summary.get("bear_claims")),
            "status": status,
        }
        # Print JSON log to stdout/logger
        logger.info(f"[AUDIT_LOG] {json.dumps(entry)}")
    except Exception as e:
        logger.warning(f"Failed to log transition: {e}")
