"""
DurableTrainingService — MongoDB-persisted training job lifecycle and state machine.

Provides durable, restart-recoverable orchestration for Jetson model training:
1. Idempotent submission with content-addressed deduplication.
2. Atomic admission gate respecting Jetson Orin capacity (training_max: 1).
3. Leased execution with periodic heartbeats and orphaned job recovery.
4. Fail-closed deterministic holdout evaluation and promotion gate.
5. Post-promotion active model verification.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import datetime
from enum import Enum
import hashlib
import json
import logging
import math
import time
from typing import Any, Optional
import uuid

from app.db import mongo_store
from app.services.jetson_feature_client import JetsonFeatureClient, feature_client
from app.services.jetson_training_orchestrator import (
    JetsonTrainingOrchestrator,
    PromotionDecision,
    PromotionVerificationError,
)

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    QUEUED = "QUEUED"
    ADMITTED = "ADMITTED"
    RUNNING = "RUNNING"
    EVALUATING = "EVALUATING"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


@dataclass
class TrainingJobRecord:
    job_id: str
    task: str
    base_model_id: str
    dataset_manifest_id: Optional[str]
    hyperparameters: dict[str, Any]
    idempotency_key: str
    status: JobStatus
    jetson_job_id: Optional[str] = None
    candidate_model_id: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    result: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value if isinstance(self.status, JobStatus) else str(self.status)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrainingJobRecord:
        raw = dict(data)
        raw.pop("_id", None)
        status_val = raw.get("status", JobStatus.QUEUED.value)
        status = status_val if isinstance(status_val, JobStatus) else JobStatus(str(status_val))
        return cls(
            job_id=raw.get("job_id", ""),
            task=raw.get("task", ""),
            base_model_id=raw.get("base_model_id", ""),
            dataset_manifest_id=raw.get("dataset_manifest_id"),
            hyperparameters=raw.get("hyperparameters", {}),
            idempotency_key=raw.get("idempotency_key", ""),
            status=status,
            jetson_job_id=raw.get("jetson_job_id"),
            candidate_model_id=raw.get("candidate_model_id"),
            lease_owner=raw.get("lease_owner"),
            lease_expires_at=raw.get("lease_expires_at"),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            result=raw.get("result"),
        )


class DurableTrainingService:
    """Manages persisted training jobs, concurrency admission, leases, and lifecycle transitions."""

    COLLECTION_NAME = "training_jobs"

    def __init__(
        self,
        db: Any = None,
        client: JetsonFeatureClient | None = None,
        worker_id: str | None = None,
        orchestrator: JetsonTrainingOrchestrator | None = None,
    ):
        self.db = db
        self.client = client or feature_client
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.orchestrator = orchestrator or JetsonTrainingOrchestrator(client=self.client)

    def _get_collection(self) -> Any:
        if self.db is not None:
            if hasattr(self.db, "get_collection"):
                return self.db.get_collection(self.COLLECTION_NAME)
            return self.db[self.COLLECTION_NAME]
        # Default to global mongo_store
        return mongo_store.db[self.COLLECTION_NAME]

    @staticmethod
    def compute_idempotency_key(task: str, manifest_id: str | None, hyperparams: dict[str, Any] | None) -> str:
        """Computes deterministic hash from job configuration."""
        raw = f"{task}:{manifest_id or ''}:{json.dumps(hyperparams or {}, sort_keys=True)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    async def submit_job(
        self,
        task: str,
        base_model_id: str,
        dataset_manifest_id: str | None = None,
        hyperparameters: dict[str, Any] | None = None,
    ) -> TrainingJobRecord:
        """Submits a training job with idempotency deduplication."""
        idem_key = self.compute_idempotency_key(task, dataset_manifest_id, hyperparameters)
        col = self._get_collection()

        # Check for existing active job with same idempotency key
        active_statuses = [
            JobStatus.SUBMITTED.value,
            JobStatus.QUEUED.value,
            JobStatus.ADMITTED.value,
            JobStatus.RUNNING.value,
            JobStatus.EVALUATING.value,
        ]
        existing = col.find_one({"idempotency_key": idem_key, "status": {"$in": active_statuses}})
        if existing:
            return TrainingJobRecord.from_dict(existing)

        job_id = f"job-{uuid.uuid4().hex[:12]}"
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        record = TrainingJobRecord(
            job_id=job_id,
            task=task,
            base_model_id=base_model_id,
            dataset_manifest_id=dataset_manifest_id,
            hyperparameters=hyperparameters or {},
            idempotency_key=idem_key,
            status=JobStatus.QUEUED,
            created_at=now_iso,
            updated_at=now_iso,
        )

        col.insert_one(record.to_dict())
        logger.info("[DurableTrainingService] Job %s queued (task=%s, idem=%s)", job_id, task, idem_key)
        return record

    async def get_job(self, job_id: str) -> Optional[TrainingJobRecord]:
        """Retrieves job by unique identifier."""
        col = self._get_collection()
        doc = col.find_one({"job_id": job_id})
        return TrainingJobRecord.from_dict(doc) if doc else None

    async def try_admit_next_job(self) -> bool:
        """
        Atomically admits the next queued job if Jetson and internal queue capacity allow.
        Jetson Orin allows training_max: 1.
        """
        col = self._get_collection()

        # 1. Check internal active jobs
        active_jobs = col.find({"status": {"$in": [JobStatus.ADMITTED.value, JobStatus.RUNNING.value, JobStatus.EVALUATING.value]}})
        if len(active_jobs) > 0:
            return False

        # 2. Check Jetson capacity
        try:
            health = await self.client.get_health()
            queue = health.get("queue", {})
            active = queue.get("training_active", 0)
            max_active = queue.get("training_max", 1)
            if active >= max_active:
                return False
        except Exception as e:
            logger.warning("[DurableTrainingService] Failed to check Jetson health: %s", e)
            return False

        # 3. Find oldest queued job
        queued = col.find({"status": JobStatus.QUEUED.value})
        if not queued:
            return False

        # Sort by created_at ascending
        sorted_queued = sorted(queued, key=lambda x: x.get("created_at", ""))
        target = sorted_queued[0]
        job_id = target["job_id"]

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        col.update_one(
            {"job_id": job_id, "status": JobStatus.QUEUED.value},
            {"$set": {"status": JobStatus.ADMITTED.value, "updated_at": now_iso}},
        )
        logger.info("[DurableTrainingService] Admitted job %s for execution", job_id)
        return True

    async def process_admitted_job(
        self,
        job_id: str,
        lease_seconds: float = 60.0,
        poll_interval_s: float = 2.0,
        max_poll_seconds: float = 300.0,
        champion_eval: dict[str, Any] | None = None,
        expected_champion_version: str | None = None,
    ) -> TrainingJobRecord:
        """
        Executes an admitted job with lease renewal, status polling, holdout evaluation,
        and fail-closed promotion.
        """
        col = self._get_collection()
        job = await self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        # 1. Acquire lease and mark RUNNING
        now = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (now + datetime.timedelta(seconds=lease_seconds)).isoformat()
        col.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": JobStatus.RUNNING.value,
                    "lease_owner": self.worker_id,
                    "lease_expires_at": expires_at,
                    "updated_at": now.isoformat(),
                }
            },
        )

        # 2. Submit to Jetson
        try:
            sub = await self.client.submit_training_job(
                task=job.task,
                base_model_id=job.base_model_id,
                dataset_manifest_id=job.dataset_manifest_id,
                hyperparameters=job.hyperparameters,
            )
            jetson_job_id = sub.get("job_id")
            col.update_one({"job_id": job_id}, {"$set": {"jetson_job_id": jetson_job_id}})
        except Exception as e:
            col.update_one(
                {"job_id": job_id},
                {"$set": {"status": JobStatus.FAILED.value, "result": {"error": str(e)}}},
            )
            return await self.get_job(job_id)

        # 3. Poll Jetson until complete with lease heartbeat
        start_time = time.monotonic()
        cand_model_id = None
        while time.monotonic() - start_time < max_poll_seconds:
            # Heartbeat lease
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            col.update_one(
                {"job_id": job_id},
                {"$set": {"lease_expires_at": (now_dt + datetime.timedelta(seconds=lease_seconds)).isoformat()}},
            )

            status_resp = await self.client.get_training_job(jetson_job_id)
            j_status = status_resp.get("status")

            if j_status == "completed":
                cand_model_id = status_resp.get("candidate_model_id")
                col.update_one({"job_id": job_id}, {"$set": {"candidate_model_id": cand_model_id}})
                break
            elif j_status in ("failed", "cancelled"):
                err_msg = status_resp.get("error_message") or f"Jetson job terminated with status {j_status}"
                col.update_one(
                    {"job_id": job_id},
                    {"$set": {"status": JobStatus.FAILED.value, "result": {"error": err_msg}}},
                )
                return await self.get_job(job_id)

            await asyncio.sleep(poll_interval_s)

        if not cand_model_id:
            col.update_one(
                {"job_id": job_id},
                {"$set": {"status": JobStatus.TIMEOUT.value, "result": {"error": "Polling timed out"}}},
            )
            return await self.get_job(job_id)

        # 4. Evaluate candidate
        col.update_one({"job_id": job_id}, {"$set": {"status": JobStatus.EVALUATING.value}})
        eval_resp = await self.client.evaluate_candidate(cand_model_id)

        # 5. Fail-closed promotion gate
        cand_metadata = {"candidate_model_id": cand_model_id, "task": job.task}
        passed, reason = self.orchestrator.evaluate_promotion_gate(
            candidate_metadata=cand_metadata,
            candidate_eval=eval_resp,
            champion_eval=champion_eval,
            expected_champion_version=expected_champion_version,
        )

        result_payload = {
            "metrics": eval_resp.get("metrics", eval_resp),
            "reason": reason,
            "decision": PromotionDecision.PROMOTED.value if passed else PromotionDecision.REJECTED.value,
        }

        if passed:
            await self.client.promote_candidate(cand_model_id)
            active_model = await self.client.get_active_model(job.task)
            if active_model != cand_model_id:
                col.update_one(
                    {"job_id": job_id},
                    {"$set": {"status": JobStatus.FAILED.value, "result": {"error": "Verification failed post-promotion"}}},
                )
                raise PromotionVerificationError(f"Active model for {job.task} is {active_model}, expected {cand_model_id}")

            col.update_one(
                {"job_id": job_id},
                {"$set": {"status": JobStatus.PROMOTED.value, "result": result_payload}},
            )
        else:
            col.update_one(
                {"job_id": job_id},
                {"$set": {"status": JobStatus.REJECTED.value, "result": result_payload}},
            )

        return await self.get_job(job_id)

    async def reconcile_stale_leases(self) -> int:
        """Finds running jobs whose worker lease has expired and reclaims or re-queues them."""
        col = self._get_collection()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        running_jobs = col.find({"status": JobStatus.RUNNING.value})

        reclaimed_count = 0
        for job in running_jobs:
            lease_exp = job.get("lease_expires_at")
            if lease_exp and lease_exp < now_iso:
                job_id = job["job_id"]
                col.update_one(
                    {"job_id": job_id},
                    {
                        "$set": {
                            "status": JobStatus.QUEUED.value,
                            "lease_owner": None,
                            "lease_expires_at": None,
                            "updated_at": now_iso,
                        }
                    },
                )
                reclaimed_count += 1
                logger.warning("[DurableTrainingService] Reclaimed stale job %s", job_id)

        return reclaimed_count
