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
        # Default to global mongo_store doc db
        try:
            return mongo_store.get_doc_db()[self.COLLECTION_NAME]
        except Exception:
            if hasattr(mongo_store, "db") and mongo_store.db is not None:
                return mongo_store.db[self.COLLECTION_NAME]
            raise

    def ensure_indexes(self) -> None:
        """Ensures MongoDB unique and query indexes on training_jobs collection."""
        try:
            col = self._get_collection()
            if hasattr(col, "create_index"):
                active_statuses = [
                    JobStatus.SUBMITTED.value,
                    JobStatus.QUEUED.value,
                    JobStatus.ADMITTED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.EVALUATING.value,
                ]
                # Unique index on idempotency_key for active jobs
                try:
                    col.create_index(
                        [("idempotency_key", 1)],
                        unique=True,
                        partialFilterExpression={"status": {"$in": active_statuses}},
                        name="uniq_active_idempotency_key",
                    )
                except Exception as ex:
                    logger.debug("[DurableTrainingService] Index uniq_active_idempotency_key: %s", ex)

                # Unique index on task + dataset_manifest_id for active jobs
                try:
                    col.create_index(
                        [("task", 1), ("dataset_manifest_id", 1)],
                        unique=True,
                        partialFilterExpression={
                            "dataset_manifest_id": {"$type": "string"},
                            "status": {"$in": active_statuses},
                        },
                        name="uniq_active_task_manifest",
                    )
                except Exception as ex:
                    logger.debug("[DurableTrainingService] Index uniq_active_task_manifest: %s", ex)

                try:
                    col.create_index([("job_id", 1)], unique=True, name="uniq_job_id")
                except Exception as ex:
                    logger.debug("[DurableTrainingService] Index uniq_job_id: %s", ex)
        except Exception as e:
            logger.warning("[DurableTrainingService] ensure_indexes failed: %s", e)

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
        """Submits a training job with idempotency and task-manifest deduplication."""
        idem_key = self.compute_idempotency_key(task, dataset_manifest_id, hyperparameters)
        col = self._get_collection()

        # Check for existing active job with same idempotency key OR task + manifest
        active_statuses = [
            JobStatus.SUBMITTED.value,
            JobStatus.QUEUED.value,
            JobStatus.ADMITTED.value,
            JobStatus.RUNNING.value,
            JobStatus.EVALUATING.value,
        ]
        query_clauses: list[dict[str, Any]] = [{"idempotency_key": idem_key}]
        if dataset_manifest_id:
            query_clauses.append({"task": task, "dataset_manifest_id": dataset_manifest_id})

        existing = col.find_one({"$or": query_clauses, "status": {"$in": active_statuses}})
        if existing:
            logger.info(
                "[DurableTrainingService] Job already queued/active for task=%s, manifest=%s (job_id=%s). Deduplicating.",
                task, dataset_manifest_id, existing.get("job_id")
            )
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

        try:
            col.insert_one(record.to_dict())
            logger.info("[DurableTrainingService] Job %s queued (task=%s, idem=%s)", job_id, task, idem_key)
            return record
        except Exception as e:
            # Handle DuplicateKeyError or race condition
            is_dup = "duplicatekey" in e.__class__.__name__.lower() or "duplicate key" in str(e).lower()
            if is_dup:
                logger.info("[DurableTrainingService] Duplicate key hit on insert for idem=%s; fetching existing job", idem_key)
                existing_after_race = col.find_one({"$or": query_clauses, "status": {"$in": active_statuses}})
                if existing_after_race:
                    return TrainingJobRecord.from_dict(existing_after_race)
            raise

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

        # 1. Check internal active jobs using count_documents (NOT len(find(...)))
        active_filter = {"status": {"$in": [JobStatus.ADMITTED.value, JobStatus.RUNNING.value, JobStatus.EVALUATING.value]}}
        if hasattr(col, "count_documents"):
            active_count = col.count_documents(active_filter)
        else:
            active_count = sum(1 for _ in col.find(active_filter))

        if active_count > 0:
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

        # 3. Atomically admit oldest queued job
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if hasattr(col, "find_one_and_update"):
            target = col.find_one_and_update(
                {"status": JobStatus.QUEUED.value},
                {"$set": {"status": JobStatus.ADMITTED.value, "updated_at": now_iso}},
                sort=[("created_at", 1)],
            )
            if not target:
                return False
            job_id = target["job_id"]
        else:
            # Fallback for simple dict mock
            queued = [d for d in col.find({"status": JobStatus.QUEUED.value})]
            if not queued:
                return False
            sorted_queued = sorted(queued, key=lambda x: x.get("created_at", ""))
            target = sorted_queued[0]
            job_id = target["job_id"]
            res = col.update_one(
                {"job_id": job_id, "status": JobStatus.QUEUED.value},
                {"$set": {"status": JobStatus.ADMITTED.value, "updated_at": now_iso}},
            )
            if hasattr(res, "modified_count") and res.modified_count == 0:
                return False

        # 4. Anti-race safety gate: ensure we didn't admit concurrently above capacity
        if hasattr(col, "count_documents"):
            post_count = col.count_documents(active_filter)
        else:
            post_count = sum(1 for _ in col.find(active_filter))

        if post_count > 1:
            # Multi-worker tie-breaker: keep the oldest active job, revert any newer concurrent admission
            oldest_active = col.find_one(active_filter, sort=[("created_at", 1)]) if hasattr(col, "find_one") else None
            if oldest_active and oldest_active.get("job_id") != job_id:
                # Revert this admission to prevent queue overflow
                col.update_one(
                    {"job_id": job_id, "status": JobStatus.ADMITTED.value},
                    {"$set": {"status": JobStatus.QUEUED.value, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}},
                )
                return False
            elif oldest_active and oldest_active.get("job_id") == job_id:
                # We won the race as the oldest job; keep admission
                pass
            else:
                col.update_one(
                    {"job_id": job_id, "status": JobStatus.ADMITTED.value},
                    {"$set": {"status": JobStatus.QUEUED.value, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}},
                )
                return False

        logger.info("[DurableTrainingService] Admitted job %s for execution", job_id)
        return True

    async def execute_leased_job(self, job_id: str, **kwargs: Any) -> TrainingJobRecord:
        """Convenience alias for process_admitted_job for external callers."""
        return await self.process_admitted_job(job_id, **kwargs)

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
        Executes an admitted job with conditional lease acquisition, heartbeat renewal,
        resilient status polling, holdout evaluation, and fail-closed promotion.
        """
        col = self._get_collection()
        job = await self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        terminal_statuses = (
            JobStatus.PROMOTED.value,
            JobStatus.REJECTED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
            JobStatus.TIMEOUT.value,
        )
        if job.status.value in terminal_statuses:
            raise ValueError(f"Cannot process job {job_id}: job is in terminal state {job.status.value}")

        # 1. Conditionally acquire lease restricting to valid active lifecycle states
        now = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now.isoformat()
        expires_at = (now + datetime.timedelta(seconds=lease_seconds)).isoformat()
        next_status = JobStatus.EVALUATING.value if job.status == JobStatus.EVALUATING else JobStatus.RUNNING.value

        acquire_res = col.update_one(
            {
                "job_id": job_id,
                "status": {"$in": [JobStatus.ADMITTED.value, JobStatus.RUNNING.value, JobStatus.EVALUATING.value]},
                "$or": [
                    {"status": JobStatus.ADMITTED.value},
                    {"lease_owner": None},
                    {"lease_expires_at": {"$lt": now_iso}},
                    {"lease_owner": self.worker_id},
                ],
            },
            {
                "$set": {
                    "status": next_status,
                    "lease_owner": self.worker_id,
                    "lease_expires_at": expires_at,
                    "updated_at": now_iso,
                }
            },
        )
        if hasattr(acquire_res, "modified_count") and acquire_res.modified_count == 0:
            recheck = await self.get_job(job_id)
            if recheck and recheck.status.value in terminal_statuses:
                raise ValueError(f"Cannot acquire lease for job {job_id}: job transitioned to terminal state {recheck.status.value}")
            raise RuntimeError(f"Failed to acquire lease for job {job_id}: lease held by another worker")

        # 2. Check if candidate_model_id already exists (interrupted in EVALUATING)
        cand_model_id = job.candidate_model_id
        jetson_job_id = job.jetson_job_id

        if not cand_model_id:
            # Submit to Jetson if remote job was not already submitted
            if not jetson_job_id:
                try:
                    submit_kwargs: dict[str, Any] = {
                        "task": job.task,
                        "base_model_id": job.base_model_id,
                        "dataset_manifest_id": job.dataset_manifest_id,
                        "hyperparameters": job.hyperparameters,
                    }
                    if job.hyperparameters and "proposal_id" in job.hyperparameters:
                        submit_kwargs["proposal_id"] = job.hyperparameters["proposal_id"]

                    sub = await self.client.submit_training_job(**submit_kwargs)
                    jetson_job_id = sub.get("job_id")
                    col.update_one(
                        {"job_id": job_id, "lease_owner": self.worker_id},
                        {"$set": {"jetson_job_id": jetson_job_id}},
                    )
                except Exception as e:
                    col.update_one(
                        {"job_id": job_id, "lease_owner": self.worker_id},
                        {"$set": {"status": JobStatus.FAILED.value, "result": {"error": str(e)}}},
                    )
                    return await self.get_job(job_id)
            else:
                logger.info("[DurableTrainingService] Resuming existing remote Jetson job %s for job %s", jetson_job_id, job_id)

            # 3. Poll Jetson until complete with lease heartbeat
            start_time = time.monotonic()
            while time.monotonic() - start_time < max_poll_seconds:
                now_dt = datetime.datetime.now(datetime.timezone.utc)
                hb_res = col.update_one(
                    {
                        "job_id": job_id,
                        "lease_owner": self.worker_id,
                        "status": {"$ne": JobStatus.CANCELLED.value},
                    },
                    {"$set": {"lease_expires_at": (now_dt + datetime.timedelta(seconds=lease_seconds)).isoformat()}},
                )
                if hasattr(hb_res, "modified_count") and hb_res.modified_count == 0:
                    cancelled_chk = await self.get_job(job_id)
                    if cancelled_chk and cancelled_chk.status == JobStatus.CANCELLED:
                        logger.info("[DurableTrainingService] Job %s was cancelled during execution", job_id)
                        return cancelled_chk
                    raise RuntimeError(f"Lost lease ownership during execution of job {job_id}")

                status_resp = await self.client.get_training_job(jetson_job_id)
                j_status = status_resp.get("status")

                if j_status == "completed":
                    cand_model_id = status_resp.get("candidate_model_id")
                    col.update_one(
                        {"job_id": job_id, "lease_owner": self.worker_id},
                        {"$set": {"candidate_model_id": cand_model_id}},
                    )
                    break
                elif j_status in ("failed", "cancelled"):
                    err_msg = status_resp.get("error", f"Jetson training reported {j_status}")
                    col.update_one(
                        {"job_id": job_id, "lease_owner": self.worker_id},
                        {"$set": {"status": JobStatus.FAILED.value, "result": {"error": err_msg}}},
                    )
                    return await self.get_job(job_id)

                await asyncio.sleep(poll_interval_s)

            if not cand_model_id:
                col.update_one(
                    {"job_id": job_id, "lease_owner": self.worker_id},
                    {"$set": {"status": JobStatus.TIMEOUT.value, "result": {"error": "Training poll timeout"}}},
                )
                return await self.get_job(job_id)
        else:
            logger.info("[DurableTrainingService] Job %s has candidate_model_id=%s; resuming evaluation directly", job_id, cand_model_id)

        # 4. Evaluate Candidate on Holdout
        col.update_one(
            {"job_id": job_id, "lease_owner": self.worker_id},
            {"$set": {"status": JobStatus.EVALUATING.value, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}},
        )

        try:
            eval_resp = await self.client.evaluate_candidate(cand_model_id)
            metrics = eval_resp.get("metrics", {})
        except Exception as e:
            col.update_one(
                {"job_id": job_id, "lease_owner": self.worker_id},
                {"$set": {"status": JobStatus.FAILED.value, "result": {"error": f"Evaluation failed: {e}"}}},
            )
            return await self.get_job(job_id)

        # 5. Promotion Gatekeeper
        cand_metadata = {
            "task": job.task,
            "candidate_model_id": cand_model_id,
        }
        cand_eval_payload = dict(eval_resp)
        if "model_id" not in cand_eval_payload:
            cand_eval_payload["model_id"] = cand_model_id
        if "dataset_manifest_id" not in cand_eval_payload and job.dataset_manifest_id:
            cand_eval_payload["dataset_manifest_id"] = job.dataset_manifest_id

        should_promote, reason = self.orchestrator.evaluate_promotion_gate(
            candidate_metadata=cand_metadata,
            candidate_eval=cand_eval_payload,
            champion_eval=champion_eval,
            expected_champion_version=expected_champion_version,
            require_champion_eval=(champion_eval is not None),
        )

        result_payload = {
            "candidate_model_id": cand_model_id,
            "metrics": metrics,
            "promotion_gate": "PASSED" if should_promote else "FAILED",
            "reason": reason,
        }

        if should_promote:
            # CRITICAL PRE-PROMOTION RECHECK: Recheck ownership and cancellation before promotion
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            now_str = now_dt.isoformat()
            pre_check = col.find_one({"job_id": job_id})
            if not pre_check:
                raise RuntimeError(f"Job {job_id} disappeared before promotion")

            if pre_check.get("status") == JobStatus.CANCELLED.value:
                logger.warning("[DurableTrainingService] Job %s was cancelled before promotion; aborting promotion", job_id)
                return await self.get_job(job_id)

            if pre_check.get("lease_owner") != self.worker_id:
                raise RuntimeError(f"Cannot promote candidate for job {job_id}: lease held by {pre_check.get('lease_owner')}")

            lease_exp_str = pre_check.get("lease_expires_at")
            if lease_exp_str and lease_exp_str < now_str:
                raise RuntimeError(f"Cannot promote candidate for job {job_id}: lease expired at {lease_exp_str}")

            # Atomically lock and refresh lease for promotion
            refresh_res = col.update_one(
                {
                    "job_id": job_id,
                    "status": {"$ne": JobStatus.CANCELLED.value},
                    "lease_owner": self.worker_id,
                    "lease_expires_at": {"$gte": now_str},
                },
                {"$set": {"lease_expires_at": (now_dt + datetime.timedelta(seconds=lease_seconds)).isoformat()}},
            )
            if hasattr(refresh_res, "modified_count") and refresh_res.modified_count == 0:
                cancelled_check = col.find_one({"job_id": job_id})
                if cancelled_check and cancelled_check.get("status") == JobStatus.CANCELLED.value:
                    logger.warning("[DurableTrainingService] Job %s was cancelled before promotion lock", job_id)
                    return await self.get_job(job_id)
                raise RuntimeError(f"Lost lease ownership before promotion for job {job_id}")

            try:
                promo_resp = await self.client.promote_candidate(cand_model_id)
                result_payload["promotion_response"] = promo_resp
            except Exception as e:
                col.update_one(
                    {"job_id": job_id, "lease_owner": self.worker_id},
                    {"$set": {"status": JobStatus.FAILED.value, "result": {"error": f"Promotion request failed: {e}"}}},
                )
                return await self.get_job(job_id)

            # Post-promotion verification: active model must match candidate
            active_info = await self.client.get_active_model(job.task)
            active_model = active_info.get("model_id") if isinstance(active_info, dict) else str(active_info)
            if active_model != cand_model_id:
                col.update_one(
                    {"job_id": job_id, "lease_owner": self.worker_id},
                    {"$set": {"status": JobStatus.FAILED.value, "result": {"error": "Verification failed post-promotion"}}},
                )
                raise PromotionVerificationError(f"Active model for {job.task} is {active_model}, expected {cand_model_id}")

            col.update_one(
                {"job_id": job_id, "lease_owner": self.worker_id},
                {"$set": {"status": JobStatus.PROMOTED.value, "result": result_payload}},
            )
        else:
            col.update_one(
                {"job_id": job_id, "lease_owner": self.worker_id},
                {"$set": {"status": JobStatus.REJECTED.value, "result": result_payload}},
            )

        return await self.get_job(job_id)

    async def reconcile_stale_leases(self) -> int:
        """Finds jobs whose worker lease has expired across ADMITTED, RUNNING, and EVALUATING states."""
        col = self._get_collection()
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now_dt.isoformat()
        active_jobs = [
            j for j in col.find({
                "status": {"$in": [JobStatus.ADMITTED.value, JobStatus.RUNNING.value, JobStatus.EVALUATING.value]}
            })
        ]

        reclaimed_count = 0
        for job in active_jobs:
            job_id = job["job_id"]
            status = job.get("status")
            lease_exp = job.get("lease_expires_at")
            lease_owner = job.get("lease_owner")
            created_at = job.get("created_at")
            updated_at = job.get("updated_at")

            is_stale = False
            if lease_exp and lease_exp < now_iso:
                is_stale = True
            elif status == JobStatus.ADMITTED.value and not lease_owner:
                # ADMITTED without owner for > 60s is considered stale
                ref_time = updated_at or created_at
                if ref_time:
                    try:
                        ref_dt = datetime.datetime.fromisoformat(ref_time)
                        if (now_dt - ref_dt).total_seconds() > 60.0:
                            is_stale = True
                    except Exception:
                        pass

            if not is_stale:
                continue

            if status == JobStatus.ADMITTED.value:
                # Interrupted during ADMITTED: revert to QUEUED for fresh capacity admission
                col.update_one(
                    {"job_id": job_id, "status": JobStatus.ADMITTED.value},
                    {
                        "$set": {
                            "status": JobStatus.QUEUED.value,
                            "lease_owner": None,
                            "lease_expires_at": None,
                            "updated_at": now_iso,
                        }
                    },
                )
            elif status == JobStatus.RUNNING.value:
                if job.get("jetson_job_id"):
                    # Remote job submitted: keep RUNNING, clear lease for resumption
                    col.update_one(
                        {"job_id": job_id, "lease_expires_at": lease_exp},
                        {
                            "$set": {
                                "lease_owner": None,
                                "lease_expires_at": None,
                                "updated_at": now_iso,
                            }
                        },
                    )
                else:
                    # Worker crashed before submitting to Jetson: revert to QUEUED
                    col.update_one(
                        {"job_id": job_id, "lease_expires_at": lease_exp},
                        {
                            "$set": {
                                "status": JobStatus.QUEUED.value,
                                "lease_owner": None,
                                "lease_expires_at": None,
                                "updated_at": now_iso,
                            }
                        },
                    )
            elif status == JobStatus.EVALUATING.value:
                # Remote job completed, evaluation interrupted: keep EVALUATING, clear lease
                col.update_one(
                    {"job_id": job_id, "lease_expires_at": lease_exp},
                    {
                        "$set": {
                            "lease_owner": None,
                            "lease_expires_at": None,
                            "updated_at": now_iso,
                        }
                    },
                )

            reclaimed_count += 1
            logger.warning("[DurableTrainingService] Reclaimed stale %s job %s", status, job_id)

        return reclaimed_count

    async def cancel_job(self, job_id: str) -> Optional[TrainingJobRecord]:
        """Cancels a job locally and calls Jetson /v1/training/jobs/{job_id}/cancel if remote job exists."""
        col = self._get_collection()
        job = await self.get_job(job_id)
        if not job:
            return None

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if job.jetson_job_id:
            try:
                if hasattr(self.client, "cancel_training_job"):
                    await self.client.cancel_training_job(job.jetson_job_id)
                elif hasattr(self.client, "_post_with_resilience"):
                    await self.client._post_with_resilience(f"/v1/training/jobs/{job.jetson_job_id}/cancel", payload={})
            except Exception as e:
                logger.warning("[DurableTrainingService] Remote cancellation notice failed for %s: %s", job.jetson_job_id, e)

        col.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": JobStatus.CANCELLED.value,
                    "updated_at": now_iso,
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
            },
        )
        logger.info("[DurableTrainingService] Job %s marked CANCELLED", job_id)
        return await self.get_job(job_id)

    async def start_worker_loop(self, poll_interval_seconds: float = 5.0, shutdown_event: Any = None):
        """Continuous background worker loop for durable training orchestration."""
        logger.info("[DurableTrainingService] Starting worker loop (worker_id=%s, interval=%.1fs)", self.worker_id, poll_interval_seconds)
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                logger.info("[DurableTrainingService] Worker loop received shutdown signal")
                break
            try:
                # 1. Reconcile stale leases
                await self.reconcile_stale_leases()

                # 2. Try admitting next queued job
                await self.try_admit_next_job()

                # 3. Find any job currently admitted/running/evaluating under this worker (or available)
                col = self._get_collection()
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                target_job = col.find_one({
                    "status": {"$in": [JobStatus.ADMITTED.value, JobStatus.RUNNING.value, JobStatus.EVALUATING.value]},
                    "$or": [
                        {"lease_owner": self.worker_id},
                        {"lease_owner": None},
                        {"lease_expires_at": {"$lt": now_iso}},
                    ],
                })
                if target_job:
                    jid = target_job["job_id"]
                    try:
                        await self.process_admitted_job(jid)
                    except Exception as ex:
                        logger.error("[DurableTrainingService] Execution failed for job %s: %s", jid, ex)

            except asyncio.CancelledError:
                logger.info("[DurableTrainingService] Worker loop cancelled")
                break
            except Exception as e:
                logger.error("[DurableTrainingService] Error in worker loop: %s", e)

            try:
                await asyncio.sleep(poll_interval_seconds)
            except asyncio.CancelledError:
                break
