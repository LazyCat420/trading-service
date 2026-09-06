"""
Adaptive Concurrency Controller — Dynamic LLM call throttling tied to vLLM /metrics.

Reads real-time hardware state from vLLM's Prometheus /metrics endpoint
(via the VLLMEndpoint objects that poll every 5s) and dynamically adjusts
how many parallel LLM requests callers can fire.

Three vLLM signals drive the limit:
    1. gpu_cache_usage_perc  — KV cache pressure (0.0–1.0)
    2. num_requests_waiting  — server-side queue depth (requests backed up)
    3. num_requests_running  — requests being processed right now

Decision matrix:
    ┌─────────────────────────┬──────────────┬──────────────────────────────┐
    │ Condition               │ Limit        │ Rationale                    │
    ├─────────────────────────┼──────────────┼──────────────────────────────┤
    │ waiting > running       │ MIN (8)      │ vLLM is backed up            │
    │ cache > 80%             │ MIN (8)      │ KV cache about to evict      │
    │ cache > 60%             │ interpolated │ Moderate pressure            │
    │ waiting == 0, cache<60% │ MAX (16)     │ Plenty of room               │
    └─────────────────────────┴──────────────┴──────────────────────────────┘

Usage:
    from app.services.adaptive_concurrency import concurrency_controller

    results = await concurrency_controller.gather(tasks, label="data_janitor")

This is a drop-in replacement for ``asyncio.gather()`` with automatic
back-pressure. The limit re-evaluates every 5 seconds.
"""

import os
import asyncio
import logging
import time
from typing import Any
from contextlib import asynccontextmanager

from app.config import settings

logger = logging.getLogger(__name__)

# ── Configurable bounds (via .env) ───────────────────────────────────
_MIN = getattr(settings, "ADAPTIVE_MIN_CONCURRENCY", 1)
_MAX = getattr(settings, "ADAPTIVE_MAX_CONCURRENCY", 4)

# How often (seconds) the controller re-evaluates the limit.
_REEVALUATE_INTERVAL = 5.0

#: Per-box ceiling on requests we let RUN at once, whatever the KV tiers say.
#: MEASURED 2026-09-06 (E1a/E1a-2 on gold-spark, GLM): prefills alone serialise
#: at ~22 s per 25k prompt (4 in flight -> first token in 31/57/76/91 s), but
#: beside two neighbours mid-DECODE a fresh 25k prompt waited 160-165 s for its
#: first token at 38% KV — prefill is starved by decode, and prism's watchdog
#: aborts at 300 s. 16 such stalls in three days, each a full agent restart.
#: Set from the E1a-2 curve; env-overridable for the next measurement.
MAX_RUNNING_PER_BOX = int(os.environ.get("MAX_RUNNING_PER_BOX", "2"))


# The total active token budget allowed on the cluster
_MAX_TOKEN_BUDGET = getattr(settings, "ADAPTIVE_MAX_TOKEN_BUDGET", 128000)

class AdaptiveConcurrencyController:
    """Dynamic concurrency limiter tied to vLLM /metrics hardware state."""

    def __init__(
        self,
        min_concurrency: int = _MIN,
        max_concurrency: int = _MAX,
        max_token_budget: int = _MAX_TOKEN_BUDGET,
    ):
        self.min_concurrency = max(1, min_concurrency)
        self.max_concurrency = max(self.min_concurrency, max_concurrency)
        self.max_token_budget = max(10000, max_token_budget)
        self._current_limit: int = self.max_concurrency
        self._last_eval: float = 0.0
        # Track per-label active counts for observability
        self._label_active: dict[str, int] = {}
        # Global concurrency tracking across all gathers
        self._active_tasks_count = 0
        self._active_tokens = 0
        self._cv = asyncio.Condition()

        # Per-box pools (2026-09-06). The global pool averaged the KV of every
        # endpoint and summed their running counts, so an idle Jetson raised the
        # limit of a saturated Gold Spark. Each box is judged on its own metrics
        # and capped at MAX_RUNNING_PER_BOX.
        self._box_active: dict[str, int] = {}
    async def _acquire_slot(self, tokens: int = 0, priority: int = 1, box: str | None = None):
        is_heavy = tokens >= 10000
        is_high_priority = priority == 0

        async with self._cv:
            while True:
                # 1. Request limit check.
                #
                # A box run must clear BOTH pools: its own box AND the global
                # one it is about to be counted in. The first version checked
                # only the box, so `_active_tasks_count` could pass
                # `current_limit` and the callers that never name a box (the
                # memory consolidator, briefings, VLLMClient.chat) queued
                # behind runs admitted without consulting the pool they filled.
                global_ok = self._active_tasks_count < self._maybe_update_limit()
                limit_ok = (
                    global_ok and self._box_active.get(box, 0) < self.limit_for(box)
                ) if box else global_ok
                # 2. Token budget check: heavy non-high priority requests must fit in budget
                # (or run immediately if no other tasks are running to prevent deadlock)
                budget_ok = True
                if is_heavy and not is_high_priority:
                    budget_ok = (self._active_tokens + tokens <= self.max_token_budget) or (self._active_tasks_count == 0)

                if limit_ok and budget_ok:
                    break
                await self._cv.wait()

            self._active_tasks_count += 1
            self._active_tokens += tokens
            if box:
                self._box_active[box] = self._box_active.get(box, 0) + 1
    async def _release_slot(self, tokens: int = 0, box: str | None = None):
        # Decrement synchronously to prevent leaking slots if cancelled during the await
        self._active_tasks_count = max(0, self._active_tasks_count - 1)
        self._active_tokens = max(0, self._active_tokens - tokens)
        if box:
            self._box_active[box] = max(0, self._box_active.get(box, 1) - 1)
        try:
            async with self._cv:
                self._cv.notify_all()
        except asyncio.CancelledError:
            pass

    # ── vLLM /metrics readers ────────────────────────────────────────

    def _read_endpoints(self) -> list:
        """Fetch live VLLMEndpoint objects from the vLLM client singleton.

        Returns an empty list if the client isn't initialized yet
        (safe default = full concurrency).
        """
        try:
            from app.services.prism_agent_caller import llm

            if hasattr(llm, "start_metrics_polling"):
                llm.start_metrics_polling()

            return [
                ep for ep in llm._endpoints.values()
                if ep.enabled and ep.model
            ]
        except Exception:
            return []

    def _avg_cache_usage(self) -> float:
        """Average KV cache usage (0.0–1.0) across all active endpoints."""
        endpoints = self._read_endpoints()
        if not endpoints:
            return 0.0
        return sum(ep.cache_usage for ep in endpoints) / len(endpoints)

    def _total_waiting(self) -> int:
        """Total requests currently waiting in vLLM server queues."""
        return sum(ep.requests_waiting for ep in self._read_endpoints())

    def _total_running(self) -> int:
        """Total requests currently being processed by vLLM servers."""
        return sum(ep.requests_running for ep in self._read_endpoints())

    def _total_capacity(self) -> int:
        """Sum of max_concurrent across all active endpoints."""
        return sum(ep.max_concurrent for ep in self._read_endpoints())

    # ── Limit calculation ────────────────────────────────────────────

    def _endpoint(self, box: str):
        for ep in self._read_endpoints():
            if getattr(ep, "name", None) == box:
                return ep
        return None

    def limit_for(self, box: str) -> int:
        """This box's limit from THIS box's metrics — the same KV tiers as the
        global figure, applied to one endpoint, then capped at
        MAX_RUNNING_PER_BOX. Unknown/unpolled box: the ceiling.

        The ceiling is a CEILING. `min_concurrency` is the floor of the GLOBAL
        pool — four in the shipped config (`ADAPTIVE_MIN_CONCURRENCY: int = 4`,
        app/config/config.py:104, written into the container's .env by the
        release script) — and a global floor must not lift a box above what
        that box was measured to sustain. The first version of this function
        ended every branch in `max(self.min_concurrency, ...)`, which made the
        real limit 4 per box and never 2: precisely the concurrency at which
        E1a-2 measured 284 s and 310 s to first token against prism's 300 s
        watchdog. It would have prevented none of the stalls it was written
        for, and no test caught it because they all built the controller with
        min_concurrency=1.
        """
        ceiling = max(1, MAX_RUNNING_PER_BOX)
        ep = self._endpoint(box)
        if ep is None:
            return ceiling
        # The per-box floor is the global floor clamped to the ceiling, so a
        # tight-cache tier still cannot return more than the box can take.
        floor = max(1, min(self.min_concurrency, ceiling))
        cap = min(int(getattr(ep, "max_concurrent", 0) or ceiling), ceiling)
        cap = max(floor, cap)
        waiting = int(getattr(ep, "requests_waiting", 0) or 0)
        running = int(getattr(ep, "requests_running", 0) or 0)
        if waiting > 0 and running > 0 and waiting >= running:
            return floor
        remaining = 1.0 - float(getattr(ep, "cache_usage", 0.0) or 0.0)
        if remaining <= 0.20:
            return floor
        if remaining <= 0.40:
            return max(floor, min(2, cap))
        if remaining >= 0.80:
            return cap
        ratio = (remaining - 0.40) / 0.40
        return max(floor, min(cap, floor + int(ratio * (cap - floor))))

    def _compute_limit(self) -> int:
        """Compute concurrency limit from live vLLM /metrics data.

        Calculates concurrency limit strictly based on the remaining KV cache percentage:
          - <= 20% remaining cache (>= 0.80 used): Drop allowed limit to min_concurrency (1)
          - <= 40% remaining cache (>= 0.60 used): Limit max concurrency to min(2, max_concurrency)
          - >= 80% remaining cache (<= 0.20 used): Allow up to max_concurrency
          - In between: Interpolate linearly between min_concurrency and max_concurrency
        """
        cache_pct = self._avg_cache_usage()
        waiting = self._total_waiting()
        running = self._total_running()
        capacity = self._total_capacity()

        # If queue backpressure is severe, drop to min immediately
        if waiting > 0 and running > 0 and waiting >= running:
            return self.min_concurrency

        remaining_cache = 1.0 - cache_pct

        # 1. Critical ceiling: if remaining cache is under 20%, force min_concurrency (1)
        if remaining_cache <= 0.20:
            return self.min_concurrency

        # 2. Moderate load cliff: if remaining cache is under 40%, cap at 2
        if remaining_cache <= 0.40:
            return max(self.min_concurrency, min(2, self.max_concurrency))

        # 3. Light load: if remaining cache is above 80%, return max_concurrency
        if remaining_cache >= 0.80:
            return self.max_concurrency

        # 4. Ramped interpolation: remaining cache between 40% and 80%
        # Maps 0.40 -> 0.0, 0.80 -> 1.0
        cache_ratio = (remaining_cache - 0.40) / 0.40
        span = self.max_concurrency - self.min_concurrency
        limit = self.min_concurrency + int(cache_ratio * span)
        return max(self.min_concurrency, min(self.max_concurrency, limit))

    def _maybe_update_limit(self) -> int:
        """Re-evaluate the limit if enough time has passed."""
        now = time.monotonic()
        if now - self._last_eval >= _REEVALUATE_INTERVAL:
            total_cap = self._total_capacity()
            min_bound = min(self.min_concurrency, total_cap) if total_cap > 0 else self.min_concurrency
            max_bound = min(self.max_concurrency, total_cap) if total_cap > 0 else self.max_concurrency

            raw_limit = self._compute_limit()
            new_limit = max(min_bound, min(max_bound, raw_limit))

            if new_limit != self._current_limit:
                cache_pct = self._avg_cache_usage() * 100
                waiting = self._total_waiting()
                running = self._total_running()
                logger.info(
                    "[CONCURRENCY] Limit adjusted %d → %d "
                    "(cache=%.1f%%, running=%d, waiting=%d, max_capacity=%d)",
                    self._current_limit,
                    new_limit,
                    cache_pct,
                    running,
                    waiting,
                    total_cap,
                )
            self._current_limit = new_limit
            self._last_eval = now
        return self._current_limit

    # ── Public API ───────────────────────────────────────────────────

    @property
    def current_limit(self) -> int:
        """Current concurrency limit (may be stale by up to REEVALUATE_INTERVAL)."""
        return self._current_limit

    @property
    def total_active(self) -> int:
        """Total tasks currently in-flight across all labels."""
        return sum(self._label_active.values())

    def status(self) -> dict:
        """Return a snapshot for monitoring dashboards / /monitor/concurrency."""
        endpoints = self._read_endpoints()
        per_endpoint = {}
        for ep in endpoints:
            per_endpoint[ep.name] = {
                "cache_pct": round(ep.cache_usage * 100, 1),
                "requests_running": ep.requests_running,
                "requests_waiting": ep.requests_waiting,
                "max_concurrent": ep.max_concurrent,
            }
        return {
            "current_limit": self._current_limit,
            "min": self.min_concurrency,
            "max": self.max_concurrency,
            "cache_avg_pct": round(self._avg_cache_usage() * 100, 1),
            "total_running_on_vllm": self._total_running(),
            "total_waiting_on_vllm": self._total_waiting(),
            "total_capacity": self._total_capacity(),
            "total_active_tasks": self.total_active,
            "per_label": dict(self._label_active),
            "per_endpoint": per_endpoint,
        }

    @asynccontextmanager
    async def track(self, label: str = "unknown", tokens: int = 0, priority: int = 1, box: str | None = None):
        """Use as an async context manager for individual tasks."""
        priority_val = priority.value if hasattr(priority, "value") else int(priority)
        await self._acquire_slot(tokens=tokens, priority=priority_val, box=box)
        self._label_active[label] = self._label_active.get(label, 0) + 1
        try:
            # We don't yield the slot explicitly since it's global, just yield control
            yield
        finally:
            self._label_active[label] = max(
                0, self._label_active.get(label, 1) - 1
            )
            if self._label_active.get(label, 0) == 0:
                self._label_active.pop(label, None)
            await self._release_slot(tokens=tokens, box=box)
    async def gather(
        self,
        tasks: list,
        *,
        label: str = "unknown",
        return_exceptions: bool = True,
        tokens: int | list[int] = 0,
        priority: int = 1,
    ) -> list[Any]:
        """Drop-in replacement for asyncio.gather with adaptive concurrency and token budget scheduling.

        Args:
            tasks: List of coroutines or awaitables.
            label: Human-readable label for logging (e.g. "data_janitor").
            return_exceptions: If True, exceptions are returned in the
                result list instead of being raised (same as asyncio.gather).
            tokens: Single estimated token size per task, or list of token sizes aligned with tasks.
            priority: Priority level (0=HIGH, 1=NORMAL, 2=LOW).

        Returns:
            List of results in the same order as the input tasks.
        """
        if not tasks:
            return []

        limit = self._maybe_update_limit()
        cache_pct = self._avg_cache_usage() * 100
        waiting = self._total_waiting()
        running = self._total_running()
        logger.info(
            "[CONCURRENCY] %s: dispatching %d tasks (limit=%d | "
            "vLLM: cache=%.0f%%, running=%d, waiting=%d)",
            label,
            len(tasks),
            limit,
            cache_pct,
            running,
            waiting,
        )

        results: list[Any] = [None] * len(tasks)
        errors: list[Exception | None] = [None] * len(tasks)

        async def _run(idx: int, coro):
            task_tokens = 0
            if isinstance(tokens, list) and len(tokens) > idx:
                task_tokens = tokens[idx]
            elif isinstance(tokens, int):
                task_tokens = tokens

            priority_val = priority.value if hasattr(priority, "value") else int(priority)
            await self._acquire_slot(tokens=task_tokens, priority=priority_val)
            self._label_active[label] = self._label_active.get(label, 0) + 1
            try:
                results[idx] = await coro
            except asyncio.CancelledError:
                # Do NOT swallow CancelledError even if return_exceptions is True.
                # If we swallow it, the outer orchestrator doesn't abort correctly.
                raise
            except Exception as e:
                if return_exceptions:
                    results[idx] = e
                else:
                    errors[idx] = e
            finally:
                self._label_active[label] = max(
                    0, self._label_active.get(label, 1) - 1
                )
                # Clean up zero-count labels
                if self._label_active.get(label, 0) == 0:
                    self._label_active.pop(label, None)
                await self._release_slot(tokens=task_tokens)

        await asyncio.gather(
            *[_run(i, t) for i, t in enumerate(tasks)],
            return_exceptions=True,  # Inner gather always catches
        )

        # If not return_exceptions, raise the first error encountered
        if not return_exceptions:
            for e in errors:
                if e is not None:
                    raise e

        return results


# ── Module-level singleton ───────────────────────────────────────────
# Lazy-initialized on first import. All subsystems share this instance.
concurrency_controller = AdaptiveConcurrencyController()
