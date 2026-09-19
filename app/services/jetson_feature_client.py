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
import math
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

    # Explicit Jetson Orin server capability characteristics & known constraints
    CAPABILITY_CONCURRENT_VERSIONS: bool = False  # Jetson hosts exactly 1 active champion per task in GPU memory
    CAPABILITY_DYNAMIC_MODEL_HOTSWAP: bool = False  # Per-request historical version swapping unsupported
    CAPABILITY_IDEMPOTENT_SUBMISSION: bool = True  # Idempotency keys supported via header & payload

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

    def _headers(
        self,
        trace_id: str | None = None,
        span_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
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
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
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
        expected_version: str | None = None,
        model_id: str | None = None,
        timeout: float | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> dict[str, Any]:
        """
        GLiNER Inference: Extracts entity and event spans from source documents.
        Chunks document batches > 45 to adhere to Jetson's 50 items batch constraint.
        Enforces expected_version consistency if supplied, rejecting version drift.
        """
        if not documents:
            return {"result": {"entities": []}, "documents_processed": 0}

        def _verify_version(resp_obj: dict[str, Any]) -> None:
            if not expected_version:
                return
            actual_version = (
                resp_obj.get("model_version")
                or resp_obj.get("model_id")
                or (resp_obj.get("result", {}) if isinstance(resp_obj.get("result"), dict) else {}).get("model_version")
                or (resp_obj.get("result", {}) if isinstance(resp_obj.get("result"), dict) else {}).get("model_id")
            )
            if actual_version and actual_version != expected_version:
                raise FeatureServiceResponseError(
                    message=f"Model version mismatch for GLiNER: expected '{expected_version}', got '{actual_version}'",
                    error_code="VERSION_MISMATCH",
                    status_code=409,
                    details={"expected_version": expected_version, "actual_version": actual_version},
                )

        chunk_size = 45
        if len(documents) <= chunk_size:
            payload: dict[str, Any] = {
                "documents": documents,
                "labels": labels or DEFAULT_GLINER_LABELS,
                "threshold": threshold,
            }
            if expected_version:
                payload["expected_version"] = expected_version
            if model_id:
                payload["model_id"] = model_id
            resp = await self._post_with_resilience(
                endpoint="/v1/features/entities",
                payload=payload,
                timeout=timeout,
                trace_id=trace_id,
                span_id=span_id,
            )
            _verify_version(resp)
            return resp

        all_entities = []
        combined_resp: dict[str, Any] | None = None
        for i in range(0, len(documents), chunk_size):
            chunk = documents[i : i + chunk_size]
            payload = {
                "documents": chunk,
                "labels": labels or DEFAULT_GLINER_LABELS,
                "threshold": threshold,
            }
            if expected_version:
                payload["expected_version"] = expected_version
            if model_id:
                payload["model_id"] = model_id
            resp = await self._post_with_resilience(
                endpoint="/v1/features/entities",
                payload=payload,
                timeout=timeout,
                trace_id=trace_id,
                span_id=span_id,
            )
            _verify_version(resp)
            if combined_resp is None:
                combined_resp = dict(resp)
            ents = resp.get("result", {}).get("entities", [])
            all_entities.extend(ents)

        if combined_resp:
            combined_resp["result"] = {"entities": all_entities}
            return combined_resp
        return {"result": {"entities": []}}

    @staticmethod
    def _parse_iso_ts(ts_val: Any) -> float:
        """Parses ISO timestamp or numeric epoch into float timestamp."""
        if isinstance(ts_val, (int, float)):
            return float(ts_val)
        try:
            import datetime
            cleaned = str(ts_val).replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(cleaned)
            return dt.timestamp()
        except Exception:
            return 0.0

    async def classify_market_regime(
        self,
        instrument_id: str,
        bar_interval: str,
        window_end: str,
        lookback_bars: int,
        ohlcv: list[Any],
        price_source: str = "pinned-provider",
        adjustment_policy: str = "adjusted",
        expected_version: str | None = None,
        model_id: str | None = None,
        timeout: float | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Market CNN Inference: Classifies market regime from normalized lookback tensor.
        Rejects incomplete OHLCV (< 30 bars), zero prices, future bars past window_end, and NaN/inf values.
        Enforces expected_version consistency if supplied.
        """
        if not ohlcv or len(ohlcv) < 30:
            raise ValueError(
                f"Incomplete OHLCV data for {instrument_id}: {len(ohlcv) if ohlcv else 0} bars provided, minimum 30 required. Zero-padding is rejected."
            )

        # Future bar leakage check against window_end
        if window_end:
            we_ts = self._parse_iso_ts(window_end)
            if we_ts > 0.0:
                for idx, bar in enumerate(ohlcv):
                    if isinstance(bar, dict):
                        b_ts_raw = bar.get("timestamp") or bar.get("time") or bar.get("date")
                        if b_ts_raw:
                            b_ts = self._parse_iso_ts(b_ts_raw)
                            if b_ts > we_ts + 1.0:
                                raise ValueError(
                                    f"Future bar leakage detected in {instrument_id}: bar {idx} timestamp {b_ts_raw} > window_end {window_end}"
                                )

        formatted_ohlcv = []
        for idx, bar in enumerate(ohlcv):
            if isinstance(bar, dict):
                vals = [bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close"), bar.get("volume")]
                if any(v is None for v in vals):
                    raise ValueError(f"Bar {idx} in OHLCV for {instrument_id} is missing required values: {bar}")
                float_vals = [float(x) for x in vals]
            elif isinstance(bar, (list, tuple)):
                if len(bar) < 5:
                    raise ValueError(f"Bar {idx} in OHLCV has fewer than 5 elements: {bar}")
                float_vals = [float(x) for x in bar[:5]]
            else:
                raise ValueError(f"Bar {idx} in OHLCV is of invalid type: {type(bar)}")

            # Validate finite and strictly positive prices
            for p in float_vals[:4]:
                if math.isnan(p) or math.isinf(p) or p <= 0.0:
                    raise ValueError(f"Bar {idx} has invalid non-finite or non-positive price: {p}")
            if math.isnan(float_vals[4]) or math.isinf(float_vals[4]) or float_vals[4] < 0.0:
                raise ValueError(f"Bar {idx} has invalid non-finite or negative volume: {float_vals[4]}")

            formatted_ohlcv.append(float_vals)

        input_hash = self.compute_input_hash(formatted_ohlcv)
        payload = {
            "instrument_id": instrument_id,
            "bar_interval": bar_interval,
            "window_end": window_end,
            "lookback_bars": lookback_bars,
            "ohlcv": formatted_ohlcv,
            "feature_version": "1",
            "price_source": price_source,
            "adjustment_policy": adjustment_policy,
            "input_hash": input_hash,
        }
        if expected_version:
            payload["expected_version"] = expected_version
        if model_id:
            payload["model_id"] = model_id

        resp = await self._post_with_resilience(
            endpoint="/v1/features/market-regime",
            payload=payload,
            timeout=timeout,
            trace_id=trace_id,
            span_id=span_id,
        )

        res = resp.get("result", {})
        if expected_version:
            actual_version = (
                resp.get("model_version")
                or resp.get("model_id")
                or (res.get("model_version") if isinstance(res, dict) else None)
                or (res.get("model_id") if isinstance(res, dict) else None)
            )
            if actual_version and actual_version != expected_version:
                raise FeatureServiceResponseError(
                    message=f"Model version mismatch for market CNN: expected '{expected_version}', got '{actual_version}'",
                    error_code="VERSION_MISMATCH",
                    status_code=409,
                    details={"expected_version": expected_version, "actual_version": actual_version},
                )

        # Validate response window binding & hash integrity
        resp_hash = resp.get("input_hash") or res.get("input_hash")
        if resp_hash and resp_hash != input_hash:
            raise FeatureServiceResponseError(
                f"Input hash mismatch in market regime response for {instrument_id}: expected {input_hash}, got {resp_hash}",
                error_code="STALE_OR_MISMATCHED_OUTPUT",
            )
        resp_inst = resp.get("instrument_id") or res.get("instrument_id")
        if resp_inst and resp_inst.upper() != instrument_id.upper():
            raise FeatureServiceResponseError(
                f"Instrument ID mismatch in market regime response: expected {instrument_id}, got {resp_inst}",
                error_code="STALE_OR_MISMATCHED_OUTPUT",
            )

        return resp

    async def predict_forecast(
        self,
        instrument_id: str,
        bar_interval: str,
        lookback_bars: int,
        cutoff: str | None = None,
        sequence: list[list[float]] | None = None,
        ohlcv: list[Any] | None = None,
        feature_schema: dict[str, Any] | None = None,
        adjustment_policy: str = "adjusted",
        horizon_bars: int = 5,
        preprocessing_version: str = "1",
        expected_version: str | None = None,
        model_id: str | None = None,
        timeout: float | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Timeseries RNN Inference: Predicts return quantiles and volatility uncertainty.
        Validates minimum sequence length (>= 25), non-finite checks, future bar cutoff bounds, and quantile monotonicity.
        Enforces expected_version consistency if supplied.
        """
        # Future bar leakage check against cutoff
        if cutoff and ohlcv:
            c_ts = self._parse_iso_ts(cutoff)
            if c_ts > 0.0:
                for idx, bar in enumerate(ohlcv):
                    if isinstance(bar, dict):
                        b_ts_raw = bar.get("timestamp") or bar.get("time") or bar.get("date")
                        if b_ts_raw:
                            b_ts = self._parse_iso_ts(b_ts_raw)
                            if b_ts > c_ts + 1.0:
                                raise ValueError(
                                    f"Future bar leakage detected in {instrument_id}: bar {idx} timestamp {b_ts_raw} > cutoff {cutoff}"
                                )

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

        if formatted_seq is None or len(formatted_seq) < 25:
            raise ValueError(
                f"Incomplete sequence data for {instrument_id}: {len(formatted_seq) if formatted_seq else 0} steps provided, minimum 25 required."
            )

        for idx, step in enumerate(formatted_seq):
            for v in step:
                if math.isnan(float(v)) or math.isinf(float(v)):
                    raise ValueError(f"Step {idx} in forecast sequence has non-finite value: {v}")

        input_hash = self.compute_input_hash(formatted_seq or [])
        payload = {
            "instrument_id": instrument_id,
            "bar_interval": bar_interval,
            "lookback_bars": lookback_bars,
            "sequence": formatted_seq or [],
            "adjustment_policy": adjustment_policy,
            "horizon_bars": horizon_bars,
            "preprocessing_version": preprocessing_version,
            "input_hash": input_hash,
        }
        if cutoff:
            payload["cutoff"] = cutoff
        if feature_schema:
            payload["feature_schema"] = feature_schema
        if expected_version:
            payload["expected_version"] = expected_version
        if model_id:
            payload["model_id"] = model_id

        resp = await self._post_with_resilience(
            endpoint="/v1/features/forecast",
            payload=payload,
            timeout=timeout,
            trace_id=trace_id,
            span_id=span_id,
        )

        res = resp.get("result", {})
        if expected_version:
            actual_version = (
                resp.get("model_version")
                or resp.get("model_id")
                or (res.get("model_version") if isinstance(res, dict) else None)
                or (res.get("model_id") if isinstance(res, dict) else None)
            )
            if actual_version and actual_version != expected_version:
                raise FeatureServiceResponseError(
                    message=f"Model version mismatch for timeseries RNN: expected '{expected_version}', got '{actual_version}'",
                    error_code="VERSION_MISMATCH",
                    status_code=409,
                    details={"expected_version": expected_version, "actual_version": actual_version},
                )

        # Validate response window binding & hash integrity
        res = resp.get("result", {})
        resp_hash = resp.get("input_hash") or res.get("input_hash")
        if resp_hash and resp_hash != input_hash:
            raise FeatureServiceResponseError(
                f"Input hash mismatch in forecast response for {instrument_id}: expected {input_hash}, got {resp_hash}",
                error_code="STALE_OR_MISMATCHED_OUTPUT",
            )
        resp_inst = resp.get("instrument_id") or res.get("instrument_id")
        if resp_inst and resp_inst.upper() != instrument_id.upper():
            raise FeatureServiceResponseError(
                f"Instrument ID mismatch in forecast response: expected {instrument_id}, got {resp_inst}",
                error_code="STALE_OR_MISMATCHED_OUTPUT",
            )

        # Monotonicity check on return quantiles: p10 <= p50 <= p90
        quantiles = res.get("return_quantiles") or res.get("quantiles", {})
        p10 = quantiles.get("p10")
        p50 = quantiles.get("p50")
        p90 = quantiles.get("p90")
        if p10 is not None and p50 is not None and p90 is not None:
            if not (float(p10) <= float(p50) <= float(p90)):
                logger.error(
                    "[JetsonFeatureClient] Crossed quantiles detected: p10=%s, p50=%s, p90=%s for %s",
                    p10, p50, p90, instrument_id,
                )
                resp["status"] = "DEGRADED"

        return resp

    # -------------------------------------------------------------------------
    # Training & Model Lifecycle Management (Port 8002)
    # -------------------------------------------------------------------------

    async def submit_training_job(
        self,
        task: str,
        base_model_id: str,
        dataset_manifest_id: str | None = None,
        label_schema_version: str = "1",
        evaluation_suite_id: str | None = None,
        hyperparameters: dict[str, Any] | None = None,
        requested_by: str | None = None,
        proposal_id: str | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Submits an asynchronous training job to the Jetson Feature Platform with optional idempotency key."""
        url = f"{self.base_url}/v1/training/jobs"
        headers = self._headers(idempotency_key=idempotency_key)
        payload: dict[str, Any] = {
            "task": task,
            "base_model_id": base_model_id,
            "label_schema_version": label_schema_version,
        }
        if dataset_manifest_id is not None:
            payload["dataset_manifest_id"] = dataset_manifest_id
        if evaluation_suite_id is not None:
            payload["evaluation_suite_id"] = evaluation_suite_id
        if hyperparameters is not None:
            payload["hyperparameters"] = hyperparameters
        if requested_by is not None:
            payload["requested_by"] = requested_by
        if proposal_id is not None:
            payload["proposal_id"] = proposal_id
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key

        req_timeout = timeout or self.timeout
        async with httpx.AsyncClient(timeout=req_timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.is_success:
                return resp.json()
            raise FeatureServiceResponseError(
                message=f"Failed to submit training job: {resp.text}",
                status_code=resp.status_code,
                error_code="TRAINING_SUBMISSION_ERROR",
            )

    async def list_training_jobs(self, timeout: float | None = None) -> list[dict[str, Any]]:
        """Retrieves all submitted training jobs from Jetson in chronological order."""
        url = f"{self.base_url}/v1/training/jobs"
        headers = self._headers()
        req_timeout = timeout or self.timeout
        async with httpx.AsyncClient(timeout=req_timeout) as client:
            resp = await client.get(url, headers=headers)
            if resp.is_success:
                return resp.json()
            raise FeatureServiceResponseError(
                message=f"Failed to list training jobs: {resp.text}",
                status_code=resp.status_code,
                error_code="TRAINING_LIST_ERROR",
            )

    async def get_training_job(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Retrieves current execution status, progress, metrics, and logs for a training job."""
        url = f"{self.base_url}/v1/training/jobs/{job_id}"
        headers = self._headers()
        req_timeout = timeout or self.timeout
        async with httpx.AsyncClient(timeout=req_timeout) as client:
            resp = await client.get(url, headers=headers)
            if resp.is_success:
                return resp.json()
            raise FeatureServiceResponseError(
                message=f"Failed to get training job {job_id}: {resp.text}",
                status_code=resp.status_code,
                error_code="TRAINING_STATUS_ERROR",
            )

    async def cancel_training_job(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Cancels a pending or running training job on Jetson."""
        url = f"{self.base_url}/v1/training/jobs/{job_id}/cancel"
        headers = self._headers()
        req_timeout = timeout or self.timeout
        async with httpx.AsyncClient(timeout=req_timeout) as client:
            resp = await client.post(url, headers=headers)
            if resp.is_success:
                return resp.json()
            raise FeatureServiceResponseError(
                message=f"Failed to cancel training job {job_id}: {resp.text}",
                status_code=resp.status_code,
                error_code="TRAINING_CANCEL_ERROR",
            )

    async def evaluate_candidate(self, candidate_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Runs the frozen test evaluation suite on an immutable candidate artifact."""
        url = f"{self.base_url}/v1/models/{candidate_id}/evaluate"
        headers = self._headers()
        req_timeout = timeout or max(self.timeout, 10.0)  # Evaluation can take up to 10s
        async with httpx.AsyncClient(timeout=req_timeout) as client:
            resp = await client.post(url, headers=headers)
            if resp.is_success:
                return resp.json()
            raise FeatureServiceResponseError(
                message=f"Failed to evaluate candidate model {candidate_id}: {resp.text}",
                status_code=resp.status_code,
                error_code="EVALUATION_ERROR",
            )

    async def promote_candidate(self, candidate_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Promotes candidate model through Jetson's policy gate to become active champion."""
        url = f"{self.base_url}/v1/models/{candidate_id}/promote"
        headers = self._headers()
        req_timeout = timeout or self.timeout
        async with httpx.AsyncClient(timeout=req_timeout) as client:
            resp = await client.post(url, headers=headers)
            if resp.is_success:
                return resp.json()
            raise FeatureServiceResponseError(
                message=f"Failed to promote candidate model {candidate_id}: {resp.text}",
                status_code=resp.status_code,
                error_code="PROMOTION_ERROR",
            )

    async def rollback_model(self, model_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Restores prior champion model after drift, timeout, or metric breach."""
        url = f"{self.base_url}/v1/models/{model_id}/rollback"
        headers = self._headers()
        req_timeout = timeout or self.timeout
        async with httpx.AsyncClient(timeout=req_timeout) as client:
            resp = await client.post(url, headers=headers)
            if resp.is_success:
                return resp.json()
            raise FeatureServiceResponseError(
                message=f"Failed to rollback model {model_id}: {resp.text}",
                status_code=resp.status_code,
                error_code="ROLLBACK_ERROR",
            )

    async def get_model_metrics(self, model_id: str, timeout: float | None = None) -> dict[str, Any]:
        """
        Retrieves evaluation metrics and evaluation timestamp for a registered or active model.
        First tries GET /v1/models/{model_id}/metrics.
        If that returns 404 or 405 (or FeatureServiceResponseError with 404/405), falls back to evaluate_candidate(model_id).
        Returns:
            {
                "model_id": str,
                "task": str,
                "metrics": dict[str, float],
                "sample_count": int,
                "evaluated_at": str (ISO-8601),
                "dataset_manifest_id": str | None,
            }
        """
        url = f"{self.base_url}/v1/models/{model_id}/metrics"
        headers = self._headers()
        req_timeout = timeout or self.timeout
        try:
            async with httpx.AsyncClient(timeout=req_timeout) as client:
                resp = await client.get(url, headers=headers)
                if resp.is_success:
                    data = resp.json()
                    if isinstance(data, dict):
                        return {
                            "model_id": data.get("model_id") or model_id,
                            "task": data.get("task") or "",
                            "metrics": data.get("metrics") or {},
                            "sample_count": data.get("sample_count") or data.get("samples") or 0,
                            "evaluated_at": data.get("evaluated_at") or data.get("timestamp") or "",
                            "dataset_manifest_id": data.get("dataset_manifest_id"),
                        }
        except (httpx.RequestError, FeatureServiceError):
            pass

        # Fallback: run holdout evaluation to obtain real metrics
        eval_resp = await self.evaluate_candidate(model_id, timeout=timeout)
        return {
            "model_id": eval_resp.get("model_id") or model_id,
            "task": eval_resp.get("task") or "",
            "metrics": eval_resp.get("metrics") or {},
            "sample_count": eval_resp.get("sample_count") or eval_resp.get("samples") or 0,
            "evaluated_at": eval_resp.get("evaluated_at") or eval_resp.get("timestamp") or "",
            "dataset_manifest_id": eval_resp.get("dataset_manifest_id"),
        }

    async def list_models(self, timeout: float | None = None) -> list[dict[str, Any]]:
        """Lists all registered models on Jetson Orin platform."""
        url = f"{self.base_url}/v1/models"
        headers = self._headers()
        req_timeout = timeout or self.timeout
        async with httpx.AsyncClient(timeout=req_timeout) as client:
            resp = await client.get(url, headers=headers)
            if resp.is_success:
                data = resp.json()
                return data.get("models", [])
            raise FeatureServiceResponseError(
                message=f"Failed to list models: {resp.text}",
                status_code=resp.status_code,
                error_code="LIST_MODELS_ERROR",
            )

    async def get_active_model(self, task: str, timeout: float | None = None) -> Optional[str]:
        """Returns model_id of active champion model for the given task."""
        models = await self.list_models(timeout=timeout)
        t_lower = task.lower()
        for m in models:
            if m.get("status") == "active":
                m_task = (m.get("task") or "").lower()
                m_id = (m.get("model_id") or "").lower()
                if t_lower in m_task or m_task in t_lower or t_lower in m_id:
                    return m.get("model_id")
        return None

    async def get_active_models(self, timeout: float | None = None) -> dict[str, dict[str, Any]]:
        """
        Discovers all currently active champion models across specialist tasks on Jetson.
        Returns mapping by canonical task name:
            {
                "gliner": {"model_id": ..., "model_version": ..., "task": "gliner", "status": "active"},
                "cnn": {"model_id": ..., "model_version": ..., "task": "market_cnn", "status": "active"},
                "rnn": {"model_id": ..., "model_version": ..., "task": "timeseries_rnn", "status": "active"},
            }
        """
        models = await self.list_models(timeout=timeout)
        active_map: dict[str, dict[str, Any]] = {}
        for m in models:
            if m.get("status") == "active":
                task_raw = (m.get("task") or "").lower()
                m_id = (m.get("model_id") or "").lower()
                if "gliner" in task_raw or "entity" in task_raw or "gliner" in m_id:
                    active_map["gliner"] = m
                elif "cnn" in task_raw or "regime" in task_raw or "cnn" in m_id:
                    active_map["cnn"] = m
                elif "rnn" in task_raw or "forecast" in task_raw or "volatility" in task_raw or "rnn" in m_id:
                    active_map["rnn"] = m
        return active_map



# Global singleton instance for trading-service cycle consumers
feature_client = JetsonFeatureClient()
