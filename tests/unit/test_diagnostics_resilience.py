"""Unit tests for Step 03: Diagnostics Availability and Control Plane Resilience.

Verifies:
1. Mode resolution failure in diagnostics returns bounded DEGRADED telemetry with default_mode="UNKNOWN",
   while execution resolver strictly fails closed (raises ControlPlaneConfigurationError) without silent fallback.
2. Complete database unavailability returns DEGRADED telemetry with deployed commit SHA intact and
   database-backed sections marked UNAVAILABLE without turning unknown counts to zero or heartbeats to ALIVE.
3. Isolated failure in a single section (e.g., outbox or reconciliation) degrades only that section
   without aborting the remaining metrics.
4. Query timeout / slow query is caught and bounded with TIMEOUT error category.
5. Error messages strictly sanitize secrets and raw connection strings.
6. FastAPI endpoint returns HTTP 200 with metrics_status="DEGRADED" and diagnostic body intact.
"""

import os
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.trading.control_plane import (
    ControlPlaneConfigurationError,
    ControlPlaneMode,
    get_control_plane_operational_metrics,
    resolve_control_plane_mode,
)


def test_invalid_mode_diagnostics_returns_degraded_and_resolver_fails_closed(monkeypatch):
    """When CONTROL_PLANE_MODE is invalid, diagnostics returns DEGRADED while resolver fails closed."""
    monkeypatch.setenv("CONTROL_PLANE_MODE", "CORRUPTED_MODE")

    # 1. Resolver must strictly fail closed - NEVER silently fall back to OBSERVE
    with pytest.raises(ControlPlaneConfigurationError) as exc_info:
        resolve_control_plane_mode()
    assert "CORRUPTED_MODE" in str(exc_info.value)

    # 2. Diagnostics must remain available and return bounded DEGRADED telemetry
    fake_db = MagicMock()
    fake_db.__getitem__.return_value.find.return_value = []
    fake_db.__getitem__.return_value.find_one.return_value = None
    fake_db.__getitem__.return_value.count_documents.return_value = 0
    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: fake_db)
    monkeypatch.setattr("app.trading.outbox.repository.get_outbox_metrics", lambda: {})

    metrics = get_control_plane_operational_metrics()
    assert metrics["metrics_status"] == "DEGRADED"
    assert metrics["default_mode"] == "UNKNOWN"
    assert "mode_resolution" in metrics["degraded_sections"]
    assert metrics["mode_resolution"]["status"] == "UNAVAILABLE"
    assert metrics["mode_resolution"]["error_category"] == "CONFIGURATION_ERROR"
    assert "deployed_commit_sha" in metrics


def test_database_unavailable_returns_degraded_without_falsifying_data(monkeypatch):
    """When MongoDB is completely down, diagnostics returns DEGRADED with commit SHA intact.

    Unknown counts must NOT be turned to zero, and heartbeats must NOT be ALIVE.
    """
    monkeypatch.setenv("GIT_COMMIT_SHA", "test-sha-12345")
    monkeypatch.setenv("CONTROL_PLANE_MODE", "OBSERVE")

    # Simulate database connection acquisition raising PyMongo / connection error
    def mock_get_doc_db():
        raise RuntimeError("Connection to 10.0.0.16:27017 failed: connection refused")

    monkeypatch.setattr("app.db.mongo_store.get_doc_db", mock_get_doc_db)
    monkeypatch.setattr(
        "app.trading.outbox.repository.get_outbox_metrics",
        lambda: (_ for _ in ()).throw(RuntimeError("DB unreachable")),
    )

    metrics = get_control_plane_operational_metrics()

    assert metrics["metrics_status"] == "DEGRADED"
    assert metrics["deployed_commit_sha"] == "test-sha-12345"
    assert "worker_heartbeats" in metrics["degraded_sections"]
    assert "outbox" in metrics["degraded_sections"]
    assert "reconciliation" in metrics["degraded_sections"]
    assert "outcome_evaluation" in metrics["degraded_sections"]

    # Must NOT turn counts into zero!
    outcome_sec = metrics["outcome_evaluation"]
    assert outcome_sec["status"] == "UNAVAILABLE"
    assert outcome_sec["total_decisions"] is None
    assert outcome_sec["mature_decisions"] is None
    assert outcome_sec["coverage_pct"] is None

    # Heartbeats must NOT report ALIVE
    hb_sec = metrics["worker_heartbeats"]
    assert hb_sec["status"] == "UNAVAILABLE"
    assert "ALIVE" not in str(hb_sec)


def test_single_failed_section_isolation(monkeypatch):
    """Failure in one metric section (e.g. outbox) must not abort the other sections."""
    fake_db = MagicMock()
    now_doc = MagicMock()

    # Worker heartbeats returns valid cursor
    fake_cursor = [
        {
            "worker": "outbox_worker",
            "last_heartbeat": None,
            "status": "RUNNING",
        }
    ]
    fake_db.__getitem__.return_value.find.return_value = fake_cursor
    fake_db.__getitem__.return_value.find_one.return_value = None
    fake_db.__getitem__.return_value.count_documents.return_value = 5

    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: fake_db)
    monkeypatch.setenv("CONTROL_PLANE_MODE", "OBSERVE")

    # Injected failure only in outbox
    def failing_outbox_metrics():
        raise RuntimeError("Outbox table lock or query failure")

    monkeypatch.setattr("app.trading.outbox.repository.get_outbox_metrics", failing_outbox_metrics)

    metrics = get_control_plane_operational_metrics()

    assert metrics["metrics_status"] == "DEGRADED"
    assert "outbox" in metrics["degraded_sections"]
    assert metrics["outbox"]["status"] == "UNAVAILABLE"
    assert metrics["outbox"]["error_category"] in ("QUERY_ERROR", "INTERNAL_ERROR")

    # Other sections must still succeed!
    assert "worker_heartbeats" not in metrics["degraded_sections"]
    assert "reconciliation" not in metrics["degraded_sections"]
    assert "outcome_evaluation" not in metrics["degraded_sections"]
    assert metrics["outcome_evaluation"]["total_decisions"] == 5


def test_slow_query_timeout_handling(monkeypatch):
    """Slow query / execution timeout must return TIMEOUT category and not crash diagnostics."""
    fake_db = MagicMock()

    class TimeoutError(Exception):
        pass

    def timeout_find(*args, **kwargs):
        raise TimeoutError("operation exceeded time limit: 5000ms")

    fake_db.__getitem__.return_value.find.side_effect = timeout_find
    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: fake_db)
    monkeypatch.setenv("CONTROL_PLANE_MODE", "OBSERVE")

    metrics = get_control_plane_operational_metrics()

    assert metrics["metrics_status"] == "DEGRADED"
    assert "worker_heartbeats" in metrics["degraded_sections"]
    assert metrics["worker_heartbeats"]["error_category"] == "TIMEOUT"


def test_sanitization_of_secrets_and_connection_strings(monkeypatch):
    """Error messages in diagnostic telemetry must strictly scrub credentials and connection strings."""
    import secrets
    test_token = f"auth_{secrets.token_hex(16)}"
    test_user = f"user_{secrets.token_hex(8)}"
    raw_uri = f"mongodb://{test_user}:{test_token}@10.0.0.16:27017/trading_bot?authSource=admin"

    def leaking_get_doc_db():
        raise RuntimeError(f"Failed to connect to cluster using uri {raw_uri}: socket closed")

    monkeypatch.setattr("app.db.mongo_store.get_doc_db", leaking_get_doc_db)

    metrics = get_control_plane_operational_metrics()

    # Serialize entire metrics payload to string
    import json
    metrics_str = json.dumps(metrics)

    assert test_token not in metrics_str
    assert "mongodb://" not in metrics_str
    assert test_user not in metrics_str


def test_fastapi_control_plane_metrics_endpoint_http_semantics(monkeypatch):
    """FastAPI endpoint returns HTTP 200 with X-Metrics-Status header and degraded body."""
    from cycle_main import create_app

    app = create_app()
    client = TestClient(app)

    # 1. Healthy test
    with patch("app.trading.control_plane.get_control_plane_operational_metrics") as mock_metrics:
        mock_metrics.return_value = {
            "metrics_status": "HEALTHY",
            "deployed_commit_sha": "abc1234",
            "default_mode": "OBSERVE",
            "degraded_sections": [],
        }
        res = client.get("/control-plane/metrics")
        assert res.status_code == 200
        assert res.headers.get("X-Metrics-Status") == "HEALTHY"
        assert res.json()["metrics_status"] == "HEALTHY"

    # 2. Degraded test
    with patch("app.trading.control_plane.get_control_plane_operational_metrics") as mock_metrics:
        mock_metrics.return_value = {
            "metrics_status": "DEGRADED",
            "deployed_commit_sha": "abc1234",
            "default_mode": "UNKNOWN",
            "degraded_sections": ["database", "outbox"],
        }
        res = client.get("/control-plane/metrics")
        assert res.status_code == 200
        assert res.headers.get("X-Metrics-Status") == "DEGRADED"
        assert res.json()["metrics_status"] == "DEGRADED"
        assert "degraded_sections" in res.json()
