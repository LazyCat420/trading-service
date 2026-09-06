"""The panel called a healthy long agent turn a dead pipeline.

MEASURED 2026-09-06 by sampling the singleton every 15 s while
`cycle-v3-1788682529` ran: 25 of 101 samples showed `pipeline_state.updated_at`
older than the client's running-stale threshold (`RUNNING_STALE_THRESHOLD_MS =
300_000`, trading-client useCycleStatus.js), peaking at **522 s**, while
`v3_quant_analyst` and then `v3_bear_agent` were working normally and the cycle
went on to finish its desks.

`updated_at` advances only when `emit()` names a phase, and an agent's tool loop
emits nothing between "agent starting" and its artifact. The threshold is not
the defect — the missing heartbeat is.

Where to put it was measured, not guessed. All four of that cycle's >300 s gaps
contained tool calls (7, 6, 4 and 4), so the tool-result path sees them. Merging
the event and tool-call timelines:

    cycle-v3-1788682529   worst gap 522 s -> 323 s
    cycle-v3-1788660665             ...   -> 302 s
    cycle-v3-1788674782             ...   -> 242 s

One marginal residual survives — an LLM turn that calls no tool at all, which
is the TTFT stall shape the per-box cap attacks from the other side.
"""
from __future__ import annotations

import time
from unittest.mock import patch

from app.services.pipeline_state import PipelineStateDB


class TestTheHeartbeat:
    def setup_method(self):
        PipelineStateDB._last_heartbeat = 0.0

    def teardown_method(self):
        PipelineStateDB._last_heartbeat = 0.0

    def test_it_stamps_the_running_cycle(self):
        with patch.object(PipelineStateDB, "_last_heartbeat", 0.0), \
             patch("app.services.pipeline_state.mongo_store") as store:
            assert PipelineStateDB.heartbeat("cycle-v3-1788682529") is True
        coll, query, update = store.update_docs.call_args[0]
        assert coll == "pipeline_state"
        assert query["cycle_id"] == "cycle-v3-1788682529", (
            "a heartbeat must name its cycle, or a stale caller refreshes a "
            "cycle that has already moved on"
        )
        assert query["singleton_id"] == PipelineStateDB.SINGLETON_ID
        assert set(update["$set"]) == {"updated_at"}, (
            "the heartbeat is liveness only — it must not rewrite status, "
            "progress or phase from a partial view of the state"
        )

    def test_it_is_throttled(self):
        with patch("app.services.pipeline_state.mongo_store") as store:
            first = PipelineStateDB.heartbeat("c1")
            second = PipelineStateDB.heartbeat("c1")
            third = PipelineStateDB.heartbeat("c1")
        assert (first, second, third) == (True, False, False)
        assert store.update_docs.call_count == 1, (
            "one write per interval, not one per tool call"
        )

    def test_the_interval_is_well_inside_the_clients_threshold(self):
        """Derived, not transcribed: the client goes amber at 300 s, so the
        stamp has to be an order of magnitude faster than that."""
        assert PipelineStateDB.HEARTBEAT_MIN_INTERVAL_S <= 300 / 5

    def test_it_stamps_again_after_the_interval(self):
        with patch("app.services.pipeline_state.mongo_store") as store:
            assert PipelineStateDB.heartbeat("c1") is True
            PipelineStateDB._last_heartbeat = (
                time.monotonic() - PipelineStateDB.HEARTBEAT_MIN_INTERVAL_S - 1
            )
            assert PipelineStateDB.heartbeat("c1") is True
        assert store.update_docs.call_count == 2

    def test_no_cycle_id_is_a_no_op(self):
        with patch("app.services.pipeline_state.mongo_store") as store:
            assert PipelineStateDB.heartbeat("") is False
        store.update_docs.assert_not_called()

    def test_a_store_failure_never_reaches_the_caller(self):
        with patch("app.services.pipeline_state.mongo_store") as store:
            store.update_docs.side_effect = RuntimeError("mongo blip")
            assert PipelineStateDB.heartbeat("c1") is False


class TestTheToolPathBeatsIt:
    def test_recording_a_tool_call_stamps_the_cycle(self):
        from app.v3 import tool_telemetry

        PipelineStateDB._last_heartbeat = 0.0
        with patch.object(tool_telemetry, "mongo_store"), \
             patch.object(tool_telemetry, "_canary_check"), \
             patch.object(PipelineStateDB, "heartbeat") as beat:
            tool_telemetry.record_tool_call(
                "cycle-v3-1788682529", "v3_quant_analyst",
                tool_name="mcp__lazy-agent-service__screener_query",
                success=True, elapsed_ms=1200, ticker="EXLS",
            )
        beat.assert_called_once_with("cycle-v3-1788682529")

    def test_a_heartbeat_failure_does_not_lose_the_tool_row(self):
        from app.v3 import tool_telemetry

        with patch.object(tool_telemetry, "mongo_store") as store, \
             patch.object(tool_telemetry, "_canary_check"), \
             patch.object(PipelineStateDB, "heartbeat",
                          side_effect=RuntimeError("boom")):
            tool_telemetry.record_tool_call(
                "cycle-v3-1788682529", "v3_quant_analyst",
                tool_name="screener_query", success=True, elapsed_ms=1,
            )
        assert store.insert_docs.called or store.upsert_doc.called, (
            "the tool row must still be written when the heartbeat fails"
        )
