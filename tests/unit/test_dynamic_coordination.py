import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from app.agents.whiteboard import whiteboard
from app.tools.agent_tools import request_peer_analysis, escalate_to_pm
from app.v3.shared_desk import SharedDesk, DeskPhase
from app.v3.orchestrator import run_v3_pipeline

@pytest.mark.asyncio
async def test_whiteboard_subscription():
    # Test subscribe / unsubscribe and event notifications by directly patching whiteboard's get_db
    events = []
    
    async def sub_cb(evt):
        events.append(evt)
        
    whiteboard.subscribe(sub_cb)
    
    try:
        with patch("app.agents.whiteboard.get_db") as mock_get_db:
            mock_conn = MagicMock()
            mock_transaction = MagicMock()
            mock_conn.transaction.return_value = mock_transaction
            mock_get_db.return_value.__enter__.return_value = mock_conn
            
            # Mock database select (empty section) then insert returning ID 123
            mock_conn.execute.return_value.fetchone.side_effect = [None, (123,)]
            
            # Trigger update
            await whiteboard.write_section(
                ticker="AAPL",
                cycle_id="test-cycle",
                section="test_section",
                content="test content",
                author_agent="test_agent"
            )
            
            assert len(events) == 1
            assert events[0]["type"] == "whiteboard_update"
            assert events[0]["section"] == "test_section"
            assert events[0]["author"] == "test_agent"
            
            # Test annotation notification
            events.clear()
            mock_conn.execute.return_value.fetchone.side_effect = [("AAPL", "test_section", "test-cycle")]
            
            await whiteboard.annotate(
                entry_id=123,
                agent="annotator_agent",
                note="annotated note"
            )
            
            assert len(events) == 1
            assert events[0]["type"] == "whiteboard_annotation"
            assert events[0]["section"] == "test_section"
            assert events[0]["author"] == "annotator_agent"
            assert events[0]["note"] == "annotated note"
            
    finally:
        whiteboard.unsubscribe(sub_cb)


@pytest.mark.asyncio
async def test_request_peer_analysis_tool():
    # Test tool adds pending tasks to the task_queue section on the whiteboard
    with patch("app.agents.whiteboard.whiteboard.get_section") as mock_get_sec, \
         patch("app.agents.whiteboard.whiteboard.write_section") as mock_write_sec:
         
        # Mock whiteboard section load: no existing tasks
        mock_get_sec.return_value = {"content": {"tasks": []}}
        
        await request_peer_analysis(ticker="AAPL", target_agent="quant_analyst", query="check valuation")
        
        # Verify write section is called with appended task
        mock_write_sec.assert_called_once()
        args = mock_write_sec.call_args[1]
        assert args["section"] == "task_queue"
        assert args["content"]["tasks"][0]["target_agent"] == "quant_analyst"
        assert args["content"]["tasks"][0]["query"] == "check valuation"
        assert args["content"]["tasks"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_escalate_to_pm_tool():
    # Test escalate to PM tool writes to 'escalation' whiteboard section
    with patch("app.agents.whiteboard.whiteboard.write_section") as mock_write_sec:
         
        await escalate_to_pm(ticker="AAPL", reason="Huge catalyst")
        
        mock_write_sec.assert_called_once()
        args = mock_write_sec.call_args[1]
        assert args["section"] == "escalation"
        assert args["content"]["escalated"] is True
        assert args["content"]["reason"] == "Huge catalyst"
