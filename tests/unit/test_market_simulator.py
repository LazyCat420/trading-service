import pytest
import json
from unittest.mock import patch, MagicMock
from app.cognition.ontology.market_simulator import MarketSimulator

@pytest.fixture
def mock_db():
    with patch("app.cognition.ontology.market_simulator.get_db") as mock:
        yield mock

@pytest.fixture
def mock_llm_chat():
    with patch("app.cognition.ontology.market_simulator.llm.chat") as mock:
        yield mock

@pytest.fixture
def mock_spreading_activation():
    with patch("app.cognition.ontology.market_simulator.BrainGraph.spreading_activation") as mock:
        yield mock

@pytest.mark.asyncio
async def test_simulate_market_opinion_success(mock_db, mock_llm_chat, mock_spreading_activation):
    # Mock spreading activation to return some mock nodes
    mock_spreading_activation.return_value = {
        "nodes": [
            {"id": "AAPL", "type": "Asset", "label": "Apple Inc."},
            {"id": "person_tim_cook", "type": "Person", "label": "CEO Tim Cook"},
            {"id": "theme_ai", "type": "Theme", "label": "AI / ML"}
        ]
    }

    # Mock DB connection
    mock_conn = MagicMock()
    mock_db.return_value.__enter__.return_value = mock_conn
    mock_conn.execute.return_value.fetchone.return_value = (1,)  # Exist check

    # Mock LLM calls (1st: Persona Generator, 2nd: Debate Simulator)
    mock_response_1 = MagicMock()
    mock_response_1.content = json.dumps({
        "personas": [
            {
                "id": "person_tim_cook",
                "name": "CEO Tim Cook",
                "role": "CEO of Apple",
                "bias": "bullish",
                "concerns": "iPhone sales and AI updates"
            },
            {
                "id": "theme_ai",
                "name": "AI / ML",
                "role": "Core tech sector theme",
                "bias": "bullish",
                "concerns": "Adoption rates"
            }
        ]
    })

    mock_response_2 = MagicMock()
    mock_response_2.content = json.dumps({
        "transcript": [
            {"round": 1, "speaker_id": "person_tim_cook", "statement": "We are bullish on AI integration."}
        ],
        "relationships": [
            {
                "source_id": "person_tim_cook",
                "target_id": "AAPL",
                "relation": "SUPPORTS",
                "weight": 0.9,
                "reason": "Cook reaffirms support for Apple product cycles."
            }
        ]
    })

    mock_llm_chat.side_effect = [mock_response_1, mock_response_2]

    # Run simulator with a cycle_id
    res = await MarketSimulator.simulate_market_opinion("AAPL", topic_context="Apple announces new AI features.", cycle_id="cycle-456")

    # Assertions
    assert res["status"] == "success"
    assert len(res["personas"]) == 2
    assert len(res["transcript"]) == 1
    assert res["relationships_updated"] == 1

    # Verify database updates
    assert mock_conn.execute.called
    
    # Verify transcript table insert was called with the correct cycle_id and payload
    transcript_calls = [
        call for call in mock_conn.execute.call_args_list
        if "INSERT INTO simulation_transcripts" in call[0][0]
    ]
    assert len(transcript_calls) == 1
    assert transcript_calls[0][0][1][0] == "AAPL"
    assert transcript_calls[0][0][1][1] == "cycle-456"
    assert "We are bullish on AI integration." in transcript_calls[0][0][1][2]

@pytest.mark.asyncio
async def test_simulate_market_opinion_no_nodes(mock_db, mock_spreading_activation):
    # Mock spreading activation returning empty
    mock_spreading_activation.return_value = {"nodes": []}
    
    res = await MarketSimulator.simulate_market_opinion("AAPL")
    assert res["status"] == "skipped"
    assert res["reason"] == "no_nodes"
