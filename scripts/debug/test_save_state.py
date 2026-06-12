import sys
import asyncio
sys.path.insert(0, '/app')
from app.cycle.orchestration.state_manager import PipelineStateDB
import logging

logging.basicConfig(level=logging.DEBUG)

state = {
    "status": "starting",
    "cycle_id": "test_cycle_123",
    "progress": "test",
    "error": None,
    "phase": "starting",
    "operational_phase": "collecting",
    "step_count": 0,
    "total_steps": 10,
    "collect_flag": True,
    "analyze_flag": True,
    "trade_flag": True,
    "tickers": ["AAPL"]
}

try:
    print("Saving state...")
    PipelineStateDB.save_state(state)
    print("State saved successfully.")
except Exception as e:
    print("FAILED:", str(e))
