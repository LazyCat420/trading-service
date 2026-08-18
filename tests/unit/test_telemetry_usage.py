import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from fastapi import FastAPI
from app.routers.agent_tools_router import router, ToolUsagePayload

def test_tool_usage_payload_validation():
    # Verify the payload structure
    payload = ToolUsagePayload(
        tool_name="test_tool",
        agent_name="test_agent",
        ticker="AAPL",
        cycle_id="cycle_123",
        success=True,
        execution_ms=150,
        service_source="lazy-tool-service"
    )
    assert payload.tool_name == "test_tool"
    assert payload.agent_name == "test_agent"
    assert payload.ticker == "AAPL"
    assert payload.cycle_id == "cycle_123"
    assert payload.success is True
    assert payload.execution_ms == 150
    assert payload.service_source == "lazy-tool-service"

@patch("app.services.logging.tool_logging.log_tool_call")
def test_report_tool_usage_endpoint(mock_log_tool_call):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    
    # Send request without auth token (should return 403 or 401)
    response = client.post(
        "/api/v1/agent-tools/usage",
        json={
            "tool_name": "test_tool",
            "agent_name": "test_agent",
            "ticker": "AAPL",
            "cycle_id": "cycle_123",
            "success": True,
            "execution_ms": 150,
            "service_source": "lazy-tool-service"
        }
    )
    assert response.status_code in (401, 403)
    
    # Send request with correct auth header
    with patch("app.routers.agent_tools_router.settings") as mock_settings:
        mock_settings.API_SERVER_KEY = "test-key"
        response = client.post(
            "/api/v1/agent-tools/usage",
            headers={"Authorization": "Bearer test-key"},
            json={
                "tool_name": "test_tool",
                "agent_name": "test_agent",
                "ticker": "AAPL",
                "cycle_id": "cycle_123",
                "success": True,
                "execution_ms": 150,
                "service_source": "lazy-tool-service"
            }
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        
        mock_log_tool_call.assert_called_once_with(
            tool_name="test_tool",
            agent_name="test_agent",
            ticker="AAPL",
            cycle_id="cycle_123",
            success=True,
            execution_ms=150,
            error_message=None,
            service_source="lazy-tool-service"
        )
