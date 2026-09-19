"""
Bounded GLM Autonomous Retraining Proposal Engine.

Enforces Item 7:
- Autonomous retraining proposals from recorded failures.
- Allowed hyperparameters (learning rate in [5e-6, 5e-4], batch size in [8, 32], epochs in [1, 5]).
- Experiment budget: max jobs per 24 hours (default 2).
- Task cooldown: minimum cooldown period per specialist task (default 4 hours).
- Proposal deduplication across identical specifications.
- Strict isolation: protected evaluation datasets and promotion policies cannot be modified
  or selected by GLM.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from app.db import mongo_store

logger = logging.getLogger(__name__)

# Strict hyperparameter bounds
MIN_LEARNING_RATE = 5e-6
MAX_LEARNING_RATE = 5e-4
MIN_BATCH_SIZE = 8
MAX_BATCH_SIZE = 32
MIN_EPOCHS = 1
MAX_EPOCHS = 5

ALLOWED_HYPERPARAMETERS = frozenset({"learning_rate", "batch_size", "epochs"})
ALLOWED_TASKS = frozenset({"gliner", "market_cnn", "timeseries_rnn", "gliner_finetune", "cnn_regime", "rnn_volatility"})


class ProposalStatus(str, Enum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    COOLDOWN_BLOCKED = "COOLDOWN_BLOCKED"
    DUPLICATE_REJECTED = "DUPLICATE_REJECTED"
    COMPLETED = "COMPLETED"


@dataclass
class RetrainingProposal:
    task: str
    candidate_name: str
    dataset_manifest_id: str
    hyperparameters: dict[str, Any]
    failure_cluster_ids: list[str] = field(default_factory=list)
    justification: str = ""
    proposal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ProposalResult:
    proposal_id: str
    status: ProposalStatus
    job_id: str | None = None
    reason: str = ""


class GLMRetrainingProposalService:
    """Evaluates and bounds autonomous retraining proposals submitted by GLM with durable MongoDB persistence."""

    COLLECTION_NAME = "specialist_retraining_proposals"
    _budget_lock = None  # Initialized in __init__

    def __init__(
        self,
        max_jobs_per_24h: int = 2,
        cooldown_hours: float = 4.0,
        protected_datasets: set[str] | None = None,
        protected_content_hashes: set[str] | None = None,
        db: Any = None,
    ):
        import threading
        self.max_jobs_per_24h = max_jobs_per_24h
        self.cooldown_hours = cooldown_hours
        self.protected_datasets = set(protected_datasets or {
            "holdout_eval_v1",
            "champion_test_slice_v2",
            "protected_test_dataset",
        })
        self.protected_content_hashes = set(protected_content_hashes or set())
        self.db = db
        # Fallback volatile store if mongo is unavailable
        self._proposals: dict[str, RetrainingProposal] = {}
        self._accepted_proposals: list[tuple[RetrainingProposal, str, str]] = []
        self._budget_lock = threading.Lock()

    def check_holdout_collision(self, dataset_manifest_id: str) -> tuple[bool, str]:
        """Verify that candidate dataset does not collide with protected holdouts by name, provenance, or content hash."""
        # 1. Exact name/ID match
        if dataset_manifest_id in self.protected_datasets:
            return True, f"Dataset '{dataset_manifest_id}' is a named protected evaluation holdout."

        # 2. Check provenance and content hashes if manifest exists on disk or DB
        from pathlib import Path
        candidate_paths = [
            Path("/tmp/trading_datasets"),
            Path("./trading_datasets"),
        ]
        for base in candidate_paths:
            if not base.exists():
                continue
            for task_dir in base.iterdir():
                if not task_dir.is_dir():
                    continue
                m_path = task_dir / dataset_manifest_id / "manifest.json"
                if m_path.exists():
                    try:
                        with open(m_path, "r", encoding="utf-8") as f:
                            m_data = json.load(f)
                        # Provenance check
                        prov = m_data.get("provenance", {})
                        meta = m_data.get("metadata", {})
                        if prov.get("role") == "holdout" or meta.get("is_holdout") or meta.get("role") == "holdout":
                            return True, f"Dataset '{dataset_manifest_id}' provenance marks it as a protected holdout."
                        # Content hash collision check
                        split_hashes = m_data.get("splits", {}).get("test", {}).get("sha256") or m_data.get("sha256")
                        if split_hashes and split_hashes in self.protected_content_hashes:
                            return True, f"Dataset '{dataset_manifest_id}' content hash matches a protected holdout dataset."
                    except Exception as ex:
                        logger.debug("[ProposalService] Error reading manifest %s: %s", m_path, ex)

        return False, ""

    def reconcile_failed_submission(self, proposal_id: str, task: str) -> None:
        """Rolls back accepted proposal capacity if subsequent execution or submission failed."""
        with self._budget_lock:
            col = self._get_collection()
            if col is not None:
                col.update_one(
                    {"proposal_id": proposal_id},
                    {"$set": {"status": ProposalStatus.REJECTED.value, "reason": "Execution submission failed"}},
                )
            self._accepted_proposals = [p for p in self._accepted_proposals if p[0].proposal_id != proposal_id]
            logger.info("[ProposalService] Reconciled failed submission for proposal %s on task %s", proposal_id, task)

    def _get_collection(self) -> Any:
        if self.db is not None:
            if hasattr(self.db, "get_collection"):
                return self.db.get_collection(self.COLLECTION_NAME)
            return self.db[self.COLLECTION_NAME]
        if mongo_store:
            try:
                return mongo_store.get_doc_db()[self.COLLECTION_NAME]
            except Exception:
                if hasattr(mongo_store, "db") and mongo_store.db is not None:
                    return mongo_store.db[self.COLLECTION_NAME]
        return None

    def _compute_proposal_hash(self, proposal: RetrainingProposal) -> str:
        """Compute deterministic hash of proposal intent for deduplication."""
        h_items = sorted((str(k), str(v)) for k, v in proposal.hyperparameters.items())
        payload = {
            "task": proposal.task,
            "dataset_manifest_id": proposal.dataset_manifest_id,
            "failure_cluster_ids": sorted(proposal.failure_cluster_ids),
            "hyperparameters": h_items,
        }
        raw = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def evaluate_proposal(self, proposal: RetrainingProposal) -> ProposalResult:
        """Evaluate a retraining proposal against strict safety and capacity bounds with persistent checks."""
        pid = proposal.proposal_id
        col = self._get_collection()

        # Authoritative wall-clock time prevents client-side forged timestamps
        now = datetime.now(timezone.utc)
        if abs((proposal.submitted_at - now).total_seconds()) > 60:
            proposal.submitted_at = now

        self._proposals[pid] = proposal

        # 1. Task validation
        if proposal.task not in ALLOWED_TASKS:
            msg = f"Task '{proposal.task}' is not allowed. Permitted: {sorted(ALLOWED_TASKS)}"
            logger.warning("[ProposalService] %s: %s", pid, msg)
            return ProposalResult(proposal_id=pid, status=ProposalStatus.REJECTED, reason=msg)

        # 2. Protected dataset isolation (by name, provenance, and content hash)
        is_colliding, collision_reason = self.check_holdout_collision(proposal.dataset_manifest_id)
        if is_colliding:
            logger.error("[ProposalService] %s: %s", pid, collision_reason)
            return ProposalResult(proposal_id=pid, status=ProposalStatus.REJECTED, reason=collision_reason)

        # 3. Hyperparameter validation & policy override prohibition
        hp = proposal.hyperparameters
        if not isinstance(hp, dict):
            return ProposalResult(proposal_id=pid, status=ProposalStatus.REJECTED, reason="Hyperparameters must be a dictionary")

        disallowed = set(hp.keys()) - ALLOWED_HYPERPARAMETERS
        if disallowed:
            msg = (
                f"Disallowed hyperparameters or policy overrides detected: {sorted(disallowed)}. "
                f"Allowed hyperparameters: {sorted(ALLOWED_HYPERPARAMETERS)}. "
                "Promotion policies and evaluation thresholds are outside GLM control."
            )
            logger.warning("[ProposalService] %s: %s", pid, msg)
            return ProposalResult(proposal_id=pid, status=ProposalStatus.REJECTED, reason=msg)

        # 3a. Learning rate bounds: float/int, not bool, not NaN, not Inf
        lr = hp.get("learning_rate")
        if isinstance(lr, bool) or not isinstance(lr, (int, float)) or math.isnan(lr) or math.isinf(lr) or lr < MIN_LEARNING_RATE or lr > MAX_LEARNING_RATE:
            msg = f"Invalid learning_rate: {lr}. Must be numeric between {MIN_LEARNING_RATE} and {MAX_LEARNING_RATE}."
            return ProposalResult(proposal_id=pid, status=ProposalStatus.REJECTED, reason=msg)

        # 3b. Batch size bounds: int, not bool
        bs = hp.get("batch_size")
        if isinstance(bs, bool) or not isinstance(bs, int) or bs < MIN_BATCH_SIZE or bs > MAX_BATCH_SIZE:
            msg = f"Invalid batch_size: {bs}. Must be integer between {MIN_BATCH_SIZE} and {MAX_BATCH_SIZE}."
            return ProposalResult(proposal_id=pid, status=ProposalStatus.REJECTED, reason=msg)

        # 3c. Epoch bounds: int, not bool
        ep = hp.get("epochs")
        if isinstance(ep, bool) or not isinstance(ep, int) or ep < MIN_EPOCHS or ep > MAX_EPOCHS:
            msg = f"Invalid epochs: {ep}. Must be integer between {MIN_EPOCHS} and {MAX_EPOCHS}."
            return ProposalResult(proposal_id=pid, status=ProposalStatus.REJECTED, reason=msg)

        p_hash = self._compute_proposal_hash(proposal)

        # 4, 5, 6: Atomic Deduplication, Cooldown, and 24h Budget Reservation
        with self._budget_lock:
            # 4. Persistent Deduplication check
            if col is not None:
                dup = col.find_one({"proposal_hash": p_hash, "status": ProposalStatus.ACCEPTED.value})
                if dup:
                    msg = f"Duplicate proposal rejected. Matches prior accepted proposal {dup.get('proposal_id')}."
                    logger.info("[ProposalService] %s: %s", pid, msg)
                    return ProposalResult(proposal_id=pid, status=ProposalStatus.DUPLICATE_REJECTED, reason=msg)
            else:
                for prev_p, prev_hash, _ in self._accepted_proposals:
                    if p_hash == prev_hash:
                        msg = f"Duplicate proposal rejected. Matches prior accepted proposal {prev_p.proposal_id}."
                        logger.info("[ProposalService] %s: %s", pid, msg)
                        return ProposalResult(proposal_id=pid, status=ProposalStatus.DUPLICATE_REJECTED, reason=msg)

            # 5. Persistent Cooldown check per task
            cutoff_cooldown = now - timedelta(hours=self.cooldown_hours)
            if col is not None:
                recent_task_p = col.find_one({
                    "task": proposal.task,
                    "status": ProposalStatus.ACCEPTED.value,
                    "submitted_at": {"$gte": cutoff_cooldown.isoformat()},
                })
                if recent_task_p:
                    prev_ts_str = recent_task_p.get("submitted_at")
                    elapsed = (now - datetime.fromisoformat(prev_ts_str)).total_seconds() / 3600.0 if prev_ts_str else 0.0
                    msg = (
                        f"Task '{proposal.task}' cooldown active. "
                        f"Last job was {elapsed:.2f}h ago; cooldown is {self.cooldown_hours}h."
                    )
                    logger.info("[ProposalService] %s: %s", pid, msg)
                    return ProposalResult(proposal_id=pid, status=ProposalStatus.COOLDOWN_BLOCKED, reason=msg)
            else:
                for prev_p, _, _ in reversed(self._accepted_proposals):
                    if prev_p.task == proposal.task:
                        if prev_p.submitted_at > cutoff_cooldown:
                            elapsed = (proposal.submitted_at - prev_p.submitted_at).total_seconds() / 3600.0
                            msg = (
                                f"Task '{proposal.task}' cooldown active. "
                                f"Last job was {elapsed:.2f}h ago; cooldown is {self.cooldown_hours}h."
                            )
                            logger.info("[ProposalService] %s: %s", pid, msg)
                            return ProposalResult(proposal_id=pid, status=ProposalStatus.COOLDOWN_BLOCKED, reason=msg)
                        break

            # 6. Persistent Experiment budget limit per 24h
            cutoff_24h = now - timedelta(hours=24)
            if col is not None:
                if hasattr(col, "count_documents"):
                    recent_count = col.count_documents({
                        "status": ProposalStatus.ACCEPTED.value,
                        "submitted_at": {"$gte": cutoff_24h.isoformat()},
                    })
                else:
                    recent_count = sum(1 for _ in col.find({
                        "status": ProposalStatus.ACCEPTED.value,
                        "submitted_at": {"$gte": cutoff_24h.isoformat()},
                    }))
            else:
                recent_count = sum(1 for prev_p, _, _ in self._accepted_proposals if prev_p.submitted_at > cutoff_24h)

            if recent_count >= self.max_jobs_per_24h:
                msg = (
                    f"24-hour experiment budget exceeded ({recent_count}/{self.max_jobs_per_24h} jobs used). "
                    "Autonomous retraining throttled."
                )
                logger.warning("[ProposalService] %s: %s", pid, msg)
                return ProposalResult(proposal_id=pid, status=ProposalStatus.BUDGET_EXCEEDED, reason=msg)

            # All gates passed: assign job ID and record persistently
            job_id = f"job-{proposal.task}-{uuid.uuid4().hex[:8]}"
            doc = {
                "proposal_id": pid,
                "task": proposal.task,
                "candidate_name": proposal.candidate_name,
                "dataset_manifest_id": proposal.dataset_manifest_id,
                "hyperparameters": proposal.hyperparameters,
                "failure_cluster_ids": proposal.failure_cluster_ids,
                "proposal_hash": p_hash,
                "status": ProposalStatus.ACCEPTED.value,
                "job_id": job_id,
                "submitted_at": now.isoformat(),
            }

            if col is not None:
                col.insert_one(doc)
            self._accepted_proposals.append((proposal, p_hash, job_id))

            logger.info(
                "[ProposalService] Proposal %s ACCEPTED for task %s -> assigned job %s",
                pid, proposal.task, job_id
            )
            return ProposalResult(
                proposal_id=pid,
                status=ProposalStatus.ACCEPTED,
                job_id=job_id,
                reason="Proposal accepted within all safety and capacity bounds.",
            )

    async def submit_proposal_to_training(
        self,
        proposal: RetrainingProposal,
        durable_service: Any = None,
    ) -> ProposalResult:
        """Evaluates proposal and submits real durable job to Jetson when accepted."""
        result = self.evaluate_proposal(proposal)
        if result.status != ProposalStatus.ACCEPTED:
            return result

        if durable_service is not None:
            t_lower = proposal.task.lower()
            if "gliner" in t_lower:
                base_model = "gliner"
            elif "cnn" in t_lower:
                base_model = "cnn"
            elif "rnn" in t_lower:
                base_model = "rnn"
            else:
                base_model = proposal.task

            try:
                durable_job = await durable_service.submit_job(
                    task=proposal.task,
                    base_model_id=base_model,
                    dataset_manifest_id=proposal.dataset_manifest_id,
                    hyperparameters=proposal.hyperparameters,
                )
                result.job_id = durable_job.job_id

                # Update persistent proposal record with actual durable job_id
                col = self._get_collection()
                if col is not None:
                    col.update_one(
                        {"proposal_id": proposal.proposal_id},
                        {"$set": {"job_id": durable_job.job_id}},
                    )
            except Exception as e:
                logger.error("[ProposalService] Submission failed for %s: %s; reconciling capacity", proposal.proposal_id, e)
                self.reconcile_failed_submission(proposal.proposal_id, proposal.task)
                return ProposalResult(
                    proposal_id=proposal.proposal_id,
                    status=ProposalStatus.REJECTED,
                    reason=f"Durable submission failed: {e}",
                )

        return result

    def get_proposal(self, proposal_id: str) -> RetrainingProposal | None:
        """Retrieve stored proposal by ID."""
        col = self._get_collection()
        if col is not None:
            doc = col.find_one({"proposal_id": proposal_id})
            if doc:
                return RetrainingProposal(
                    task=doc["task"],
                    candidate_name=doc["candidate_name"],
                    dataset_manifest_id=doc["dataset_manifest_id"],
                    hyperparameters=doc["hyperparameters"],
                    failure_cluster_ids=doc.get("failure_cluster_ids", []),
                    proposal_id=doc["proposal_id"],
                    submitted_at=datetime.fromisoformat(doc["submitted_at"]) if "submitted_at" in doc else datetime.now(timezone.utc),
                )
        return self._proposals.get(proposal_id)

