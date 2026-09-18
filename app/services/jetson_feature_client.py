"""
Dedicated client for the Jetson Feature Platform (Port 8002).

Communicates with specialized non-LLM models (GLiNER, Market CNN, Timeseries RNN).
Preserves architectural separation from conversational LLMs and chat completion routers.
Provides bounded timeouts, bounded retries, circuit breaking, response-schema
validation, and telemetry lineage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Standard domain taxonomy for GLiNER news/filing entity & event extraction
DEFAULT_GLINER_LABELS: dict[str, str] = {
    "ticker": "public equity ticker symbol",
    "company": "publicly traded company or issuer",
    "executive": "company officer or director",
    "financial_metric": "reported financial measure",
    "financial_metric_value": "numeric financial value",
    "earnings_date": "earnings or reporting date",
    "guidance_period": "future reporting period",
    "analyst_action": "upgrade, downgrade, initiation, or target change",
    "corporate_action": "buyback, acquisition, offering, split, dividend",
    "regulatory_event": "government, legal, or regulatory action",
    "macro_indicator": "inflation, rates, employment, growth, etc.",
    "event_trigger": "specific market-relevant event",
}


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class FeatureServiceError(Exception):
    """Base exception for all feature platform client failures."""
    pass


class FeatureServiceCircuitOpenError(FeatureServiceError):
    """Raised when request is blocked due to open circuit breaker."""
    pass


class FeatureServiceTimeoutError(FeatureServiceError):
    """Raised when feature service call exceeds configured timeout."""
    pass


class FeatureServiceConnectionError(FeatureServiceError):
    """Raised when connection to feature service fails."""
    pass


class FeatureServiceResponseError(FeatureServiceError):
    """Raised when feature service returns a typed or unexpected error."""

    def __init__(self, message: str, error_code: str = "INTERNAL_ERROR", status_code: int = 500, details: Any = None):
        super().__init__(f"[{error_code}] {message} (HTTP {status_code})")
        self.error_code = error_code
        self.status_code = status_code
        self.details = details


@dataclass
class CircuitBreaker:
    trip_count: int = 3
    reset_seconds: float = 60.0
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0

    def can_execute(self) -> bool:
        now = time.monotonic()
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if now - self.last_failure_time >= self.reset_seconds:
                logger.info("[CircuitBreaker] Transitioning from OPEN to HALF_OPEN (probing)")
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN: allow a single probe request
        return True

    def record_success(self) -> None:
        if self.state != CircuitState.CLOSED:
            logger.info("[CircuitBreaker] Request succeeded; resetting circuit to CLOSED")
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.trip_count:
            if self.state != CircuitState.OPEN:
                logger.warning(
                    "[CircuitBreaker] Tripped to OPEN (%d consecutive failures); cooling down for %.1fs",
                    self.failure_count, self.reset_seconds,
                )
            self.state = CircuitState.OPEN


class JetsonFeatureClient:
    """Async client interfacing with Jetson Feature Platform on port 8002."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        trip_count: int | None = None,
        reset_seconds: float | None = None,
        shadow_mode: bool | None = None,
    ):
        self.base_url = (base_url or getattr(settings, "JETSON_FEATURES_URL", "http://10.0.0.30:8002")).rstrip("/")
        self.api_key = api_key if api_key is not None else getattr(settings, "JETSON_FEATURES_API_KEY", "")
        self.timeout = timeout if timeout is not None else float(getattr(settings, "JETSON_FEATURE_TIMEOUT_SECONDS", 5.0))
        self.max_retries = max_retries if max_retries is not None else int(getattr(settings, "JETSON_FEATURE_MAX_RETRIES", 2))
        
        trip = trip_count if trip_count is not None else int(getattr(settings, "JETSON_FEATURE_CIRCUIT_BREAKER_TRIP_COUNT", 3))
        reset_sec = reset_seconds if reset_seconds is not None else float(getattr(settings, "JETSON_FEATURE_CIRCUIT_BREAKER_RESET_SECONDS", 60.0))
        self.circuit_breaker = CircuitBreaker(trip_count=trip, reset_seconds=reset_sec)
        self.shadow_mode = shadow_mode if shadow_mode is not None else getattr(settings, "JETSON_FEATURE_SHADOW_MODE", True)

        self._capabilities_cache: dict[str, Any] | None = None
        self._capabilities_cached_at: float = 0.0
        self._capabilities_ttl_s: float = 300.0  # 5 minutes

    async def __aenter__(self) -> JetsonFeatureClient:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    def build_lineage_record(
        self,
        cycle_id: str,
        decision_id: str | None,
        instrument_id: str,
        source_id: str,
        response_envelope: dict[str, Any],
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Builds a standardized feature lineage document ready for MongoDB persistence."""
        import datetime
        import uuid
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return {
            "feature_id": f"feat-{uuid.uuid4().hex[:12]}",
            "cycle_id": cycle_id or "",
            "decision_id": decision_id or None,
            "instrument_id": instrument_id.upper() if instrument_id else "",
            "source_id": source_id,
            "document_id": source_id,
            "model_id": response_envelope.get("model_id", "unknown"),
            "model_version": response_envelope.get("model_version", "v1"),
            "schema_version": response_envelope.get("schema_version", "1"),
            "input_hash": response_envelope.get("input_hash", ""),
            "created_at": now_utc,
            "latency_ms": response_envelope.get("latency_ms", 0),
            "mode": mode or ("shadow" if self.shadow_mode else "active"),
            "payload": response_envelope.get("result", {}),
        }

    def _headers(self, trace_id: str | None = None, span_id: str | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        if span_id:
            headers["X-Span-Id"] = span_id
        return headers

    @staticmethod
    def compute_input_hash(data: Any) -> str:
        """Computes deterministic SHA-256 hash of payload."""
        serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    async def _post_with_resilience(
        self,
        endpoint: str,
        payload: dict[str, Any],
        timeout: float | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> dict[str, Any]:
        """Executes POST request protected by circuit breaker, timeout, and bounded retries."""
        if not self.circuit_breaker.can_execute():
            raise FeatureServiceCircuitOpenError(
                f"Feature service circuit breaker is OPEN (cooling down for {self.circuit_breaker.reset_seconds}s)"
            )

        req_timeout = timeout or self.timeout
        url = f"{self.base_url}{endpoint}"
        headers = self._headers(trace_id=trace_id, span_id=span_id)
        attempts = 0
        last_exc: Exception | None = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                async with httpx.AsyncClient(timeout=req_timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                
                if resp.is_success:
                    self.circuit_breaker.record_success()
                    data = resp.json()
                    return self._validate_response_envelope(data)

                # Handle non-2xx response
                error_body: dict[str, Any] = {}
                try:
                    error_body = resp.json()
                except Exception:
                    error_body = {"detail": resp.text}

                error_code = error_body.get("error_code") or error_body.get("code") or "INTERNAL_ERROR"
                detail = error_body.get("detail") or error_body.get("message") or resp.text

                # 4xx errors (client errors) do NOT retry
                if 400 <= resp.status_code < 500:
                    self.circuit_breaker.record_failure()
                    raise FeatureServiceResponseError(
                        message=str(detail),
                        error_code=str(error_code),
                        status_code=resp.status_code,
                        details=error_body,
                    )

                # 5xx error: transient candidate for retry
                if attempts <= self.max_retries:
                    backoff = 0.25 * (2 ** (attempts - 1))
                    logger.warning(
                        "[JetsonFeatureClient] %s failed with HTTP %d (%s); retrying in %.2fs (attempt %d/%d)",
                        endpoint, resp.status_code, error_code, backoff, attempts, self.max_retries,
                    )
                    await asyncio.sleep(backoff)
                    continue

                self.circuit_breaker.record_failure()
                raise FeatureServiceResponseError(
                    message=str(detail),
                    error_code=str(error_code),
                    status_code=resp.status_code,
                    details=error_body,
                )

            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_exc = FeatureServiceConnectionError(f"Connection to {url} failed: {e}")
                if attempts <= self.max_retries:
                    backoff = 0.25 * (2 ** (attempts - 1))
                    await asyncio.sleep(backoff)
                    continue
                self.circuit_breaker.record_failure()
                raise last_exc from e

            except httpx.TimeoutException as e:
                last_exc = FeatureServiceTimeoutError(f"Request to {url} timed out after {req_timeout}s: {e}")
                if attempts <= self.max_retries:
                    backoff = 0.25 * (2 ** (attempts - 1))
                    await asyncio.sleep(backoff)
                    continue
                self.circuit_breaker.record_failure()
                raise last_exc from e

            except (FeatureServiceResponseError, FeatureServiceCircuitOpenError):
                raise
            except Exception as e:
                self.circuit_breaker.record_failure()
                raise FeatureServiceError(f"Unexpected feature service failure: {e}") from e

        if last_exc:
            raise last_exc
        raise FeatureServiceError("Max retries exceeded without response")

    def _validate_response_envelope(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validates that inference response adheres to the frozen envelope contract."""
        if not isinstance(data, dict):
            raise FeatureServiceResponseError("Response is not a valid JSON object", "INVALID_RESPONSE_FORMAT")
        
        # Verify required envelope fields
        required_fields = ("request_id", "model_id", "model_version", "schema_version", "input_hash", "result")
        missing = [f for f in required_fields if f not in data]
        if missing:
            logger.warning("[JetsonFeatureClient] Response envelope missing expected fields: %s", missing)
            # Retain compatibility: if "result" exists or data is directly results, allow fallback
            if "result" not in data:
                data = {"result": data, "model_id": "unknown", "request_id": "compat", "schema_version": "1"}

        return data

    async def get_health(self) -> dict[str, Any]:
        """Queries /health for liveness, readiness, GPU availability, and queue state."""
        url = f"{self.base_url}/health"
        headers = self._headers()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.is_success:
                    return resp.json()
                return {"status": "degraded", "http_status": resp.status_code, "detail": resp.text}
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}

    async def get_capabilities(self, force_refresh: bool = False) -> dict[str, Any]:
        """Queries /v1/capabilities with in-memory TTL caching."""
        now = time.monotonic()
        if not force_refresh and self._capabilities_cache and (now - self._capabilities_cached_at < self._capabilities_ttl_s):
            return self._capabilities_cache

        url = f"{self.base_url}/v1/capabilities"
        headers = self._headers()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.is_success:
                    self._capabilities_cache = resp.json()
                    self._capabilities_cached_at = now
                    return self._capabilities_cache
                raise FeatureServiceResponseError(f"Failed to fetch capabilities: {resp.text}", status_code=resp.status_code)
        except Exception as e:
            logger.warning("[JetsonFeatureClient] Capabilities lookup failed: %s", e)
            if self._capabilities_cache:
                return self._capabilities_cache  # Return stale if available
            raise

    async def extract_entities(
        self,
        documents: list[dict[str, Any]],
        labels: dict[str, str] | None = None,
        threshold: float = 0.75,
        timeout: float | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> dict[str, Any]:
        """
        GLiNER Inference: Extracts entity and event spans from source documents.

        Documents input:
        [
          {
            "document_id": "finnhub:12345",
            "text": "...",
            "source_url": "https://...",
            "published_at": "2026-09-18T12:00:00Z"
          }
        ]
        """
        payload = {
            "documents": documents,
            "labels": labels or DEFAULT_GLINER_LABELS,
            "threshold": threshold,
        }
        return await self._post_with_resilience(
            endpoint="/v1/features/entities",
            payload=payload,
            timeout=timeout,
            trace_id=trace_id,
            span_id=span_id,
        )

    async def classify_market_regime(
        self,
        instrument_id: str,
        bar_interval: str,
        window_end: str,
        lookback_bars: int,
        ohlcv: list[Any],
        price_source: str = "pinned-provider",
        adjustment_policy: str = "adjusted",
        timeout: float | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Market CNN Inference: Classifies market regime from normalized lookback tensor.
        Accepts ohlcv as either list of lists [[o,h,l,c,v], ...] or list of dicts.
        """
        formatted_ohlcv = []
        for bar in ohlcv:
            if isinstance(bar, dict):
                formatted_ohlcv.append([
                    float(bar.get("open", 0.0)),
                    float(bar.get("high", 0.0)),
                    float(bar.get("low", 0.0)),
                    float(bar.get("close", 0.0)),
                    float(bar.get("volume", 0.0)),
                ])
            elif isinstance(bar, (list, tuple)):
                formatted_ohlcv.append([float(x) for x in bar])
            else:
                formatted_ohlcv.append(bar)

        payload = {
            "instrument_id": instrument_id,
            "bar_interval": bar_interval,
            "window_end": window_end,
            "lookback_bars": lookback_bars,
            "ohlcv": formatted_ohlcv,
            "feature_version": "1",
            "price_source": price_source,
            "adjustment_policy": adjustment_policy,
            "input_hash": self.compute_input_hash(formatted_ohlcv),
        }
        return await self._post_with_resilience(
            endpoint="/v1/features/market-regime",
            payload=payload,
            timeout=timeout,
            trace_id=trace_id,
            span_id=span_id,
        )

    async def predict_forecast(
        self,
        instrument_id: str,
        bar_interval: str,
        lookback_bars: int,
        cutoff: str | None = None,
        sequence: list[list[float]] | None = None,
        ohlcv: list[Any] | None = None,
        feature_schema: dict[str, Any] | None = None,
        timeout: float | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Timeseries RNN Inference: Predicts return quantiles and volatility uncertainty.
        Accepts sequence or ohlcv tensor of historical lookback features.
        """
        formatted_seq = sequence
        if formatted_seq is None and ohlcv is not None:
            formatted_seq = []
            for item in ohlcv:
                if isinstance(item, dict):
                    formatted_seq.append([
                        float(item.get("open", 0.0)),
                        float(item.get("high", 0.0)),
                        float(item.get("low", 0.0)),
                        float(item.get("close", 0.0)),
                        float(item.get("volume", 0.0)),
                    ])
                elif isinstance(item, (list, tuple)):
                    formatted_seq.append([float(x) for x in item])
                else:
                    formatted_seq.append(item)

        payload = {
            "instrument_id": instrument_id,
            "bar_interval": bar_interval,
            "lookback_bars": lookback_bars,
            "sequence": formatted_seq or [],
            "input_hash": self.compute_input_hash(formatted_seq or []),
        }
        if cutoff:
            payload["cutoff"] = cutoff
        if feature_schema:
            payload["feature_schema"] = feature_schema

        return await self._post_with_resilience(
            endpoint="/v1/features/forecast",
            payload=payload,
            timeout=timeout,
            trace_id=trace_id,
            span_id=span_id,
        )


# Global singleton instance for trading-service cycle consumers
feature_client = JetsonFeatureClient()
