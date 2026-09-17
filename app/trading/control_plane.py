"""Control Plane Rollout Modes and Precedence Router.

Defines the three explicit operational modes:
- OBSERVE: Existing paper execution is authoritative; new policy results are advisory.
           Unattributed legacy executions are recorded as LEGACY_MIGRATION_BYPASS
           without distorting executor defect statistics.
- SHADOW:  Proposals and execution are simulated without mutating the paper portfolio.
           Results are persisted to shadow collections.
- ENFORCE: Approved execution intent is strictly required for paper entry.
           Rejection or missing intent aborts the trade before reaching the executor.
           FAIL_CLOSED is the failure behavior of ENFORCE, not a separate rollout mode.
"""

from __future__ import annotations

import enum
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ControlPlaneConfigurationError(Exception):
    """Raised when control plane configuration is invalid, corrupted, or unreachable."""
    pass


class ControlPlaneMode(str, enum.Enum):
    OBSERVE = "OBSERVE"
    SHADOW = "SHADOW"
    ENFORCE = "ENFORCE"


def resolve_control_plane_mode(bot_id: Optional[str] = None) -> ControlPlaneMode:
    """Resolve effective control plane mode from environment and optional account overrides.

    Fails closed (raises ControlPlaneConfigurationError) on database error or unrecognized mode.
    """
    # 0. Check account-specific override from database (bots collection)
    if bot_id:
        try:
            from app.db import mongo_query
            bot_row = mongo_query.find_row("bots", {"bot_id": bot_id}, ["control_plane_mode"])
            if bot_row and bot_row[0]:
                db_mode = str(bot_row[0]).strip().upper()
                if db_mode not in ControlPlaneMode.__members__:
                    raise ControlPlaneConfigurationError(
                        f"Invalid mode {db_mode!r} configured in database for bot {bot_id}"
                    )
                return ControlPlaneMode(db_mode)
        except ControlPlaneConfigurationError:
            raise
        except Exception as db_err:
            raise ControlPlaneConfigurationError(
                f"Database lookup failed for bot {bot_id}: {db_err}"
            ) from db_err

    # 1. Check account-specific override from environment: CONTROL_PLANE_MODE_<BOT_ID>
    if bot_id:
        env_bot_mode = os.getenv(f"CONTROL_PLANE_MODE_{bot_id.upper().replace('-', '_')}")
        if env_bot_mode:
            env_bot_mode = env_bot_mode.strip().upper()
            if env_bot_mode not in ControlPlaneMode.__members__:
                raise ControlPlaneConfigurationError(
                    f"Invalid mode {env_bot_mode!r} in environment for bot {bot_id}"
                )
            return ControlPlaneMode(env_bot_mode)

    # 2. Check global env var first, then settings
    raw_mode = os.getenv("CONTROL_PLANE_MODE")
    if not raw_mode:
        from app.config import settings
        raw_mode = getattr(settings, "CONTROL_PLANE_MODE", "OBSERVE")
    raw_mode = str(raw_mode).strip().upper()

    if raw_mode in ControlPlaneMode.__members__:
        return ControlPlaneMode(raw_mode)

    raise ControlPlaneConfigurationError(
        f"Invalid mode {raw_mode!r} configured for control plane"
    )


def is_enforce_active(mode: ControlPlaneMode) -> bool:
    """Returns True if intent enforcement is mandatory."""
    return mode == ControlPlaneMode.ENFORCE


def is_shadow_active(mode: ControlPlaneMode) -> bool:
    """Returns True if execution should simulate without mutating paper portfolio."""
    return mode == ControlPlaneMode.SHADOW


def get_control_plane_operational_metrics() -> dict[str, Any]:
    """Aggregates end-to-end control-plane operational telemetry for monitoring and health."""
    from app.db import mongo_store
    from app.trading.outbox.repository import get_outbox_metrics
    import datetime
    import subprocess

    db = mongo_store.get_doc_db()
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Deployed Commit SHA
    commit_sha = os.getenv("GIT_COMMIT_SHA") or os.getenv("COMMIT_SHA")
    if not commit_sha:
        try:
            commit_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
            ).decode("utf-8").strip()
        except Exception:
            commit_sha = "unknown"

    # 2. Worker Heartbeats
    heartbeats_cursor = db["worker_heartbeats"].find({})
    heartbeats: dict[str, Any] = {}
    for h in heartbeats_cursor:
        w_name = h.get("worker", "unknown")
        w_time = h.get("last_heartbeat")
        if w_time and hasattr(w_time, "tzinfo") and w_time.tzinfo is None:
            w_time = w_time.replace(tzinfo=datetime.timezone.utc)
        age_s = (now - w_time).total_seconds() if isinstance(w_time, datetime.datetime) else 999999.0
        heartbeats[w_name] = {
            "last_heartbeat": w_time.isoformat() if isinstance(w_time, datetime.datetime) else None,
            "age_seconds": round(age_s, 1),
            "status": "ALIVE" if age_s <= 120.0 else "DEAD",
        }

    # 3. Outbox Metrics
    outbox = get_outbox_metrics()

    # 4. Reconciliation Telemetry
    last_rec = db["execution_reconciliations"].find_one({}, sort=[("reconciled_at", -1)])
    rec_time = last_rec.get("reconciled_at") if last_rec else None
    if rec_time and hasattr(rec_time, "tzinfo") and rec_time.tzinfo is None:
        rec_time = rec_time.replace(tzinfo=datetime.timezone.utc)

    reconciliation_info = {
        "last_reconciled_at": rec_time.isoformat() if isinstance(rec_time, datetime.datetime) else None,
        "last_reconciliation_id": last_rec.get("reconciliation_id") if last_rec else None,
        "last_verdict": last_rec.get("verdict") if last_rec else None,
    }

    # 5. Outcome Evaluation & Coverage
    last_eval = db["decision_outcomes"].find_one(
        {"maturity_status": "MATURE"}, sort=[("resolved_at", -1)]
    )
    eval_time = last_eval.get("resolved_at") if last_eval else None
    if eval_time and hasattr(eval_time, "tzinfo") and eval_time.tzinfo is None:
        eval_time = eval_time.replace(tzinfo=datetime.timezone.utc)

    total_decisions = db["decision_artifacts"].count_documents({})
    mature_decisions = db["decision_outcomes"].count_documents({"maturity_status": "MATURE"})
    unresolved_decisions = db["decision_outcomes"].count_documents({"maturity_status": "UNRESOLVED"})
    coverage_pct = round((mature_decisions / max(total_decisions, 1)) * 100, 2)

    evaluation_info = {
        "last_evaluated_at": eval_time.isoformat() if isinstance(eval_time, datetime.datetime) else None,
        "last_outcome_id": last_eval.get("outcome_id") if last_eval else None,
        "total_decisions": total_decisions,
        "mature_decisions": mature_decisions,
        "unresolved_decisions": unresolved_decisions,
        "coverage_pct": coverage_pct,
    }

    return {
        "deployed_commit_sha": commit_sha,
        "default_mode": resolve_control_plane_mode("default").value,
        "worker_heartbeats": heartbeats,
        "outbox": outbox,
        "reconciliation": reconciliation_info,
        "outcome_evaluation": evaluation_info,
    }
