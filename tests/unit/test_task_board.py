import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from contextlib import contextmanager
from app.agents.task_board import TaskBoard

@pytest.fixture(autouse=True)
def mock_taskboard_db(monkeypatch, mock_db):
    """Patch the get_db function inside app.agents.task_board to yield mock_db."""
    @contextmanager
    def fake_get_db():
        # Ensure execute returns mock_db so chaining works: db.execute(...).fetchone()
        mock_db.execute.return_value = mock_db
        yield mock_db
    monkeypatch.setattr("app.agents.task_board.get_db", fake_get_db)
    return mock_db

@pytest.mark.asyncio
async def test_post_finding_new(mock_taskboard_db):
    """Test posting a finding when no previous findings exist (sequence starts at 1)."""
    tb = TaskBoard()
    
    # Configure mock db fetchone to return None (no max id)
    mock_taskboard_db.fetchone.return_value = None
    
    finding_id = await tb.post_finding(
        source_agent="fundamental_agent",
        content="Strong revenue growth of 25% YoY",
        ticker="AAPL",
        cycle_id="cycle-2026-06",
        category="fact",
        confidence=80
    )
    
    assert finding_id == "f-0001"
    
    # Check SELECT was called to find max finding_id
    mock_taskboard_db.execute.assert_any_call(
        "SELECT COALESCE(MAX(CAST(SUBSTRING(finding_id FROM 3) AS INTEGER)), 0) "
        "FROM taskboard_findings WHERE cycle_id = %s AND ticker = %s",
        ["cycle-2026-06", "AAPL"]
    )
    
    # Check INSERT was called with f-0001
    mock_taskboard_db.execute.assert_any_call(
        "INSERT INTO taskboard_findings "
        "(finding_id, cycle_id, ticker, source_agent, content, category, confidence, responses) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        ["f-0001", "cycle-2026-06", "AAPL", "fundamental_agent", "Strong revenue growth of 25% YoY", "fact", 80, "[]"]
    )

@pytest.mark.asyncio
async def test_post_finding_increment(mock_taskboard_db):
    """Test posting a finding when findings already exist (sequence increments)."""
    tb = TaskBoard()
    
    # Simulate max finding_id being f-0005
    mock_taskboard_db.fetchone.return_value = (5,)
    
    finding_id = await tb.post_finding(
        source_agent="sentiment_agent",
        content="Bullish social sentiment spikes",
        ticker="TSLA",
        cycle_id="cycle-2026-06"
    )
    
    assert finding_id == "f-0006"

@pytest.mark.asyncio
async def test_get_findings(mock_taskboard_db):
    """Test retrieving findings with category and exclude filters."""
    tb = TaskBoard()
    
    mock_taskboard_db.fetchall.return_value = [
        ("f-0001", "fundamental_agent", "High growth", "fact", 90, "[]"),
        ("f-0002", "risk_agent", "High debt", "risk", 70, '[{"replier": "quant_agent", "content": "noted"}]')
    ]
    
    findings = await tb.get_findings(
        ticker="AAPL",
        cycle_id="cycle-2026-06",
        category="fact",
        exclude_agent="risk_agent",
        limit=10
    )
    
    assert len(findings) == 2
    assert findings[0]["id"] == "f-0001"
    assert findings[0]["source_agent"] == "fundamental_agent"
    assert findings[0]["category"] == "fact"
    assert findings[0]["responses"] == []
    
    assert findings[1]["id"] == "f-0002"
    assert findings[1]["responses"] == [{"replier": "quant_agent", "content": "noted"}]
    
    # Verify the SQL query contains category and exclude_agent filters
    called_sql = mock_taskboard_db.execute.call_args[0][0]
    called_params = mock_taskboard_db.execute.call_args[0][1]
    
    assert "AND category = %s" in called_sql
    assert "AND source_agent != %s" in called_sql
    assert called_params == ["cycle-2026-06", "AAPL", "fact", "risk_agent", 10]

@pytest.mark.asyncio
async def test_request_investigation_new(mock_taskboard_db):
    """Test creating a new investigation request when none exist (sequence starts at 1)."""
    tb = TaskBoard()
    
    mock_taskboard_db.fetchone.return_value = None
    
    inv_id = await tb.request_investigation(
        requester="macro_agent",
        question="Verify interest rate impacts",
        ticker="AAPL",
        cycle_id="cycle-2026-06",
        target_agent="quant_agent"
    )
    
    assert inv_id == "inv-0001"
    
    # Check SELECT and INSERT calls
    mock_taskboard_db.execute.assert_any_call(
        "SELECT COALESCE(MAX(CAST(SUBSTRING(investigation_id FROM 5) AS INTEGER)), 0) "
        "FROM taskboard_investigations WHERE cycle_id = %s AND ticker = %s",
        ["cycle-2026-06", "AAPL"]
    )
    mock_taskboard_db.execute.assert_any_call(
        "INSERT INTO taskboard_investigations "
        "(investigation_id, cycle_id, ticker, requester, target_agent, question, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'open')",
        ["inv-0001", "cycle-2026-06", "AAPL", "macro_agent", "quant_agent", "Verify interest rate impacts"]
    )

@pytest.mark.asyncio
async def test_claim_investigation_success(mock_taskboard_db):
    """Test claiming an open investigation successfully."""
    tb = TaskBoard()
    
    # Mock finding target_agent and status: allows claim if open and target is '*' or claiming_agent
    mock_taskboard_db.fetchone.return_value = ("*", "open")
    
    success = await tb.claim_investigation(
        req_id="inv-0001",
        claiming_agent="quant_agent",
        ticker="AAPL",
        cycle_id="cycle-2026-06"
    )
    
    assert success is True
    mock_taskboard_db.execute.assert_any_call(
        "UPDATE taskboard_investigations SET status = 'claimed', claimed_by = %s "
        "WHERE cycle_id = %s AND ticker = %s AND investigation_id = %s",
        ["quant_agent", "cycle-2026-06", "AAPL", "inv-0001"]
    )

@pytest.mark.asyncio
async def test_claim_investigation_already_claimed(mock_taskboard_db):
    """Test claiming an investigation that is already claimed fails."""
    tb = TaskBoard()
    
    mock_taskboard_db.fetchone.return_value = ("*", "claimed")
    
    success = await tb.claim_investigation(
        req_id="inv-0001",
        claiming_agent="quant_agent",
        ticker="AAPL",
        cycle_id="cycle-2026-06"
    )
    
    assert success is False

@pytest.mark.asyncio
async def test_complete_investigation_success(mock_taskboard_db):
    """Test completing a claimed investigation."""
    tb = TaskBoard()
    
    mock_taskboard_db.fetchone.return_value = ("claimed",)
    
    success = await tb.complete_investigation(
        req_id="inv-0001",
        result="No major exposure found.",
        ticker="AAPL",
        cycle_id="cycle-2026-06"
    )
    
    assert success is True
    mock_taskboard_db.execute.assert_any_call(
        "UPDATE taskboard_investigations SET status = 'completed', result = %s "
        "WHERE cycle_id = %s AND ticker = %s AND investigation_id = %s",
        ["No major exposure found.", "cycle-2026-06", "AAPL", "inv-0001"]
    )

@pytest.mark.asyncio
async def test_get_open_investigations(mock_taskboard_db):
    """Test fetching open investigations with agent filters."""
    tb = TaskBoard()
    
    mock_taskboard_db.fetchall.return_value = [
        ("inv-0001", "macro_agent", "*", "What is CPI?"),
        ("inv-0002", "risk_agent", "quant_agent", "Analyze variance?"),
        ("inv-0003", "fundamental_agent", "sentiment_agent", "Fetch social sentiment?")
    ]
    
    # Filter for quant_agent
    open_invs = await tb.get_open_investigations(
        ticker="AAPL",
        cycle_id="cycle-2026-06",
        for_agent="quant_agent"
    )
    
    assert len(open_invs) == 2
    # Should contain inv-0001 (target *) and inv-0002 (target quant_agent)
    assert open_invs[0]["id"] == "inv-0001"
    assert open_invs[1]["id"] == "inv-0002"

@pytest.mark.asyncio
async def test_clear_board(mock_taskboard_db):
    """Test clearing the board deletes records."""
    tb = TaskBoard()
    
    tb.clear_board("AAPL", "cycle-2026-06")
    
    mock_taskboard_db.execute.assert_any_call(
        "DELETE FROM taskboard_findings WHERE cycle_id = %s AND ticker = %s",
        ["cycle-2026-06", "AAPL"]
    )
    mock_taskboard_db.execute.assert_any_call(
        "DELETE FROM taskboard_investigations WHERE cycle_id = %s AND ticker = %s",
        ["cycle-2026-06", "AAPL"]
    )
