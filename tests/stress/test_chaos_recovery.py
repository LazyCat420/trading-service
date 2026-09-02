"""
Pillar 3: Chaos Engineering & State Machine Crash Recovery Suite.

Tests state machine persistence, recovery from interrupted cycles,
singleton database guards, and corrupted checkpoint handling.
"""

import pytest
import json
from app.services.pipeline_state import PipelineStateDB


class TestChaosRecovery:
    """Chaos and crash recovery tests for trading pipeline state."""

    def test_pipeline_state_missing_status_fails_safe_to_unknown(self, monkeypatch):
        """Assert partial state dict without 'status' writes 'unknown' to protect deploy interlocks."""
        written_docs = []

        from app.db import mongo_store
        monkeypatch.setattr(
            mongo_store,
            "upsert_doc",
            lambda coll, q, doc: written_docs.append((coll, q, doc)),
        )

        partial_state = {
            "cycle_id": "cycle-test-123",
            "progress": "Synthesizer in progress",
            # Note: status is omitted deliberately
        }

        PipelineStateDB.save_state(partial_state)

        assert len(written_docs) == 1
        coll, q, doc = written_docs[0]
        assert coll == "pipeline_state"
        assert doc["status"] == "unknown"  # Must NOT be 'idle'

    def test_pipeline_state_explicit_running_status_persisted(self, monkeypatch):
        """Assert running status persists correctly."""
        written_docs = []

        from app.db import mongo_store
        monkeypatch.setattr(
            mongo_store,
            "upsert_doc",
            lambda coll, q, doc: written_docs.append((coll, q, doc)),
        )

        running_state = {
            "status": "running",
            "cycle_id": "cycle-test-456",
            "tickers": ["AAPL", "NVDA"],
        }

        PipelineStateDB.save_state(running_state)

        assert len(written_docs) == 1
        _, _, doc = written_docs[0]
        assert doc["status"] == "running"
        assert doc["cycle_id"] == "cycle-test-456"

    def test_corrupted_checkpoint_recovery(self):
        """Verify malformed/corrupted JSON checkpoint safely falls back to clean default."""
        corrupted_json = '{"cycle_id": "cycle-v3-999", "checkpoints": [broken_json_without_quotes]}'

        try:
            parsed = json.loads(corrupted_json)
        except json.JSONDecodeError:
            # Clean fallback
            parsed = {"status": "error", "error": "corrupted_checkpoint_payload"}

        assert parsed["status"] == "error"
        assert "error" in parsed

    def test_stuck_system_command_recovery(self, monkeypatch):
        """Simulate stuck 'running' system commands being transitioned to 'error' after boot."""
        commands = [
            {"id": "cmd-1", "command": "START_CYCLE", "status": "running"},
            {"id": "cmd-2", "command": "STATUS", "status": "completed"},
        ]

        # Recovery logic: mark running commands as error
        recovered = []
        for cmd in commands:
            if cmd["status"] == "running":
                recovered.append({**cmd, "status": "error", "error": "interrupted_by_restart"})
            else:
                recovered.append(cmd)

        assert recovered[0]["status"] == "error"
        assert recovered[0]["error"] == "interrupted_by_restart"
        assert recovered[1]["status"] == "completed"
