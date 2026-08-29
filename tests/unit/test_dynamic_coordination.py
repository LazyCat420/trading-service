import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from app.agents.whiteboard import whiteboard
from app.tools.agent_tools import escalate_to_pm
from app.v3.shared_desk import SharedDesk, DeskPhase
from app.v3.orchestrator import run_v3_pipeline

@pytest.mark.asyncio
async def test_whiteboard_subscription():
    # Subscribe / unsubscribe and event notifications, with the whiteboard's
    # Mongo layer stubbed. This used to patch `app.agents.whiteboard.get_db`,
    # a symbol the module no longer imports: the mock intercepted nothing and
    # both writes below landed in the live database.
    events = []

    async def sub_cb(evt):
        events.append(evt)

    whiteboard.subscribe(sub_cb)

    try:
        query = MagicMock()
        store = MagicMock()
        with patch("app.agents.whiteboard.mongo_query", query), \
             patch("app.agents.whiteboard.mongo_store", store):
            # No existing version of this section → a v1 insert.
            query.find_row.return_value = None

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

            # The write itself, structurally: collection, scope, and the v1
            # document — the old SQL-free mock asserted none of this.
            collection, docs = store.insert_docs.call_args[0][:2]
            assert collection == "whiteboard_entries"
            assert docs[0]["ticker"] == "AAPL"
            assert docs[0]["cycle_id"] == "test-cycle"
            assert docs[0]["section"] == "test_section"
            assert docs[0]["author_agent"] == "test_agent"
            assert docs[0]["version"] == 1
            assert docs[0]["content"] == {"text": "test content"}

            # Test annotation notification. `annotate` resolves the entry via
            # find_row, which returns a TUPLE in the requested column order:
            # (ticker, section, cycle_id).
            events.clear()
            store.insert_docs.reset_mock()
            query.find_row.return_value = ("AAPL", "test_section", "test-cycle")

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

            collection, docs = store.insert_docs.call_args[0][:2]
            assert collection == "whiteboard_annotations"
            assert docs[0]["entry_id"] == 123
            assert docs[0]["ticker"] == "AAPL"
            assert docs[0]["section"] == "test_section"
            assert docs[0]["cycle_id"] == "test-cycle"
            assert docs[0]["author_agent"] == "annotator_agent"
            assert docs[0]["note"] == "annotated note"

    finally:
        whiteboard.unsubscribe(sub_cb)


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
