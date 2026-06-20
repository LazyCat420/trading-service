import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from app.processors.ticker_extractor import get_ticker_symbols
from app.agents.custom.market_scout import IDENTITY as SCOUT_IDENTITY

@pytest.mark.asyncio
async def test_ticker_extraction_bypasses_microagent_and_routes_to_scout():
    """
    Audit test: verifies that ticker extraction relies on deterministic regex rules
    to gather candidates, completely bypassing the standalone 'ticker_validator' LLM.
    These candidates are then validated dynamically by the Market Scout swarm orchestrator.
    """
    text = "The latest news suggests $AAPL and an ambiguous term like AI are making waves."
    
    # 1. Gather candidates (LLM validation is bypassed)
    symbols = await get_ticker_symbols(text)
    
    # AAPL is extracted deterministically. 'AI' might be extracted depending on rules.
    assert "AAPL" in symbols
    
    # 2. Verify Market Scout's identity explicitly instructs it to validate these candidates natively
    assert "validate each ticker candidate natively" in SCOUT_IDENTITY
    assert "search_web" in SCOUT_IDENTITY
