import pytest
from unittest.mock import patch, AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.routers.debug_router import router

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)

@patch("app.services.logging.service_health_probe.run_all_probes", new_callable=AsyncMock)
def test_debug_health_check(mock_run_probes, client):
    mock_run_probes.return_value = [
        {"service": "postgres", "status": "healthy", "latency_ms": 5},
        {"service": "prism-service", "status": "healthy", "latency_ms": 10}
    ]
    response = client.get("/api/debug/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["healthy"] == 2
    assert data["total"] == 2

@patch("app.services.logging.service_health_probe.run_all_probes", new_callable=AsyncMock)
def test_debug_health_check_degraded(mock_run_probes, client):
    mock_run_probes.return_value = [
        {"service": "postgres", "status": "healthy", "latency_ms": 5},
        {"service": "prism-service", "status": "down", "latency_ms": 0, "error": "Connection error"}
    ]
    response = client.get("/api/debug/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "degraded"
    assert data["healthy"] == 1
    assert data["total"] == 2

def test_debug_transcript_empty(client):
    with patch("app.services.logging.conversation_tracer.conversation_tracer.get_transcript") as mock_get_trans:
        mock_get_trans.return_value = []
        response = client.get("/api/debug/transcript/cycle_1/AAPL")
        assert response.status_code == 200
        data = response.json()
        assert data["turns"] == []
        assert "No conversation recorded yet" in data["message"]

def test_debug_transcript_with_turns(client):
    with patch("app.services.logging.conversation_tracer.conversation_tracer.get_transcript") as mock_get_trans, \
         patch("app.services.logging.conversation_tracer.conversation_tracer.get_stats") as mock_get_stats:
        
        mock_get_trans.return_value = [
            {"turn": 1, "speaker": "retriever", "listener": "all", "content": "hello"}
        ]
        mock_get_stats.return_value = {"total_turns": 1, "speakers": ["retriever"], "total_tokens": 10}
        
        response = client.get("/api/debug/transcript/cycle_1/AAPL")
        assert response.status_code == 200
        data = response.json()
        assert len(data["turns"]) == 1
        assert data["turns"][0]["speaker"] == "retriever"
        assert data["stats"]["total_turns"] == 1

def test_debug_readable_transcript(client):
    with patch("app.services.logging.conversation_tracer.conversation_tracer.get_readable_transcript") as mock_get_readable:
        mock_get_readable.return_value = "Turn 1: retriever -> all: hello"
        response = client.get("/api/debug/transcript/cycle_1/AAPL/readable")
        assert response.status_code == 200
        data = response.json()
        assert data["transcript"] == "Turn 1: retriever -> all: hello"

@patch("app.services.logging.service_health_probe.run_all_probes", new_callable=AsyncMock)
def test_debug_services_endpoint(mock_run_probes, client):
    mock_run_probes.return_value = [
        {"service": "postgres", "status": "healthy", "latency_ms": 5, "error": None}
    ]
    response = client.get("/api/debug/services")
    assert response.status_code == 200
    data = response.json()
    assert "postgres" in data["services"]
    assert data["services"]["postgres"]["status"] == "healthy"

def test_debug_tools_endpoint(client):
    from app.tools.registry import registry
    # Ensure there's at least one tool registered to test with
    assert len(registry.tools) > 0
    response = client.get("/api/debug/tools")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] > 0
    assert "tools" in data
    # Check that metadata fields are populated correctly using our fix
    first_tool = data["tools"][0]
    assert "name" in first_tool
    assert "tier" in first_tool
    assert "source" in first_tool

def test_debug_tools_agents_endpoint(client):
    response = client.get("/api/debug/tools/agents")
    assert response.status_code == 200
    data = response.json()
    assert "agents" in data
    assert "risk" in data["agents"]
    assert data["agents"]["risk"]["total_tools"] > 0

def test_debug_delegation_budget(client):
    response = client.get("/api/debug/delegation/budget")
    assert response.status_code == 200
    data = response.json()
    assert "max_per_ticker" in data
    assert data["max_per_ticker"] == 8
