"""A stopped cycle wrote its summary and never asked for its reflection.

MEASURED 2026-09-06. cycle-v3-1788668370 was stopped on purpose after AVGO's
desk BOUGHT (2.2973 @ $357.92, conf 72) and while LULU's bear agent was mid-turn.
Twenty minutes later: no `autoresearch_reports` row, no "autoresearch enqueued"
log line, 0 AUTORESEARCH rows in `system_commands` for the cycle. The enqueue
lived only in the done tail of `_run_all_v3`; STOP raises CancelledError through
the ticker loop and the handler (a775040) called `_persist_summary("stopped", …)`
as a STATEMENT — the dict it returned, exactly what the enqueue needs, was
discarded. The error path was the same. So a stopped cycle that bought never got
`recovery_stats`, and check #4's fix (ed62fa1) could not be proven on it.

Measured BEFORE writing this (E2): `run_autoresearch` driven offline against the
verbatim summary with every pymongo write recorded — overall 90.6, no
degenerate anomaly (historical scoring took over), `decision_outcomes` for AVGO
only, a clean report. So no consumer gate is needed; what the consumer cannot
tell is that the cycle was partial, and `tickers_final` claims LULU finished.

Fixture below is the verbatim `cycle_run_summaries.summary_json` of that cycle.
"""
from __future__ import annotations

import ast
import inspect
import json
from unittest.mock import patch

from app.services import pipeline_service as ps
from app.services.pipeline_service import (
    enqueue_autoresearch,
    partial_summary_fields,
)

CID = "cycle-v3-1788668370"
# verbatim summary_json, trimmed to the fields under test
STOPPED_SUMMARY = json.loads(
    '{"status": "stopped", "tickers_requested": ["AVGO", "LULU"], "tickers": ["AVGO", "LULU"], '
    '"tickers_final": ["AVGO", "LULU"], "counts_source": "store", "buy_count": 1, "sell_count": 0, '
    '"hold_count": 0, "trade_attempted": 1, "trade_executed": 1, "trade_failed": 0, '
    '"analysis_results_count": 1, "primary_failure_reason": "Cycle stopped/cancelled", "trigger_type": "v3"}'
)
# what recover_ticker_results_from_store rebuilt for it: AVGO only, LULU had no row
RECOVERED = [{"ticker": "AVGO", "action": "BUY", "trade_attempted": True, "trade_executed": True, "trade_failed": False}]


class TestPartialFields:
    def test_a_stopped_cycle_is_partial_and_names_only_the_desks_that_finished(self):
        f = partial_summary_fields("stopped", ["AVGO", "LULU"], RECOVERED, counts_source="store")
        assert f["partial"] is True
        assert f["tickers_final"] == ["AVGO"]
        assert f["tickers"] == ["AVGO"]

    def test_an_errored_cycle_is_partial_too(self):
        f = partial_summary_fields("error", ["AVGO", "LULU"], RECOVERED, counts_source="store")
        assert f["partial"] is True and f["tickers_final"] == ["AVGO"]

    def test_a_done_cycle_is_not_partial_and_keeps_its_list(self):
        f = partial_summary_fields("done", ["AVGO", "LULU"], [{"ticker": "AVGO"}, {"ticker": "LULU"}], counts_source="gather")
        assert f["partial"] is False
        assert f["tickers_final"] == ["AVGO", "LULU"]

    def test_gather_counts_never_narrow_even_when_a_desk_returned_none(self):
        """Only store-recovered counts know which desks finished; a gather list
        with a None entry is a desk that ran and failed, not one that never ran."""
        f = partial_summary_fields("done", ["AVGO", "LULU"], [{"ticker": "AVGO"}, None], counts_source="gather")
        assert f["tickers_final"] == ["AVGO", "LULU"]


class TestTheEnqueue:
    def test_one_upsert_one_shape(self):
        with patch("app.services.pipeline_service.mongo_store") as ms:
            job = enqueue_autoresearch(CID, dict(STOPPED_SUMMARY, partial=True, tickers_final=["AVGO"]))
        assert job and job.startswith("job_")
        ms.upsert_doc.assert_called_once()
        coll, flt, doc = ms.upsert_doc.call_args.args
        assert coll == "system_commands" and flt == {"id": job}
        assert doc["command_type"] == "AUTORESEARCH" and doc["status"] == "pending"
        assert isinstance(doc["payload"], dict), "payload must be a dict — a JSON string is unreadable to the poller (eval_worker)"
        assert doc["payload"]["cycle_id"] == CID
        assert doc["payload"]["cycle_summary"]["status"] == "stopped"
        assert doc["payload"]["cycle_summary"]["partial"] is True
        assert doc["payload"]["cycle_summary"]["tickers_final"] == ["AVGO"]

    def test_no_summary_no_enqueue(self):
        with patch("app.services.pipeline_service.mongo_store") as ms:
            assert enqueue_autoresearch(CID, None) is None
        ms.upsert_doc.assert_not_called()

    def test_a_store_failure_never_raises_into_the_cycle_tail(self):
        with patch("app.services.pipeline_service.mongo_store") as ms:
            ms.upsert_doc.side_effect = RuntimeError("mongo down")
            assert enqueue_autoresearch(CID, dict(STOPPED_SUMMARY)) is None


class TestEveryTailAsksForItsReflection:
    """Derived from the source of the cycle runner, not transcribed: the three
    terminal handlers — done, CancelledError, Exception — must each reach
    enqueue_autoresearch. The stopped path is the one that was missing."""

    def _tails(self) -> dict[str, str]:
        src = inspect.getsource(ps.PipelineService)
        i_done = src.index('_persist_summary("done"')
        # anchor on the CYCLE's own handlers by their log text, not on the first
        # `except CancelledError:` after the done tail — a background-task
        # handler sits in between and the first draft of this test sliced it.
        i_cancel = src.rindex("except asyncio.CancelledError:", 0, src.index("V3 Cycle CANCELLED", i_done))
        i_err = src.rindex("except Exception as e:", 0, src.index("V3 Cycle failed", i_cancel))
        i_end = src.index("finally:", i_err)
        return {"done": src[i_done:i_cancel], "stopped": src[i_cancel:i_err], "error": src[i_err:i_end]}

    def test_the_done_tail_still_enqueues(self):
        assert "enqueue_autoresearch(" in self._tails()["done"]

    def test_the_stopped_tail_enqueues(self):
        assert "enqueue_autoresearch(" in self._tails()["stopped"]

    def test_the_error_tail_enqueues(self):
        assert "enqueue_autoresearch(" in self._tails()["error"]

    def test_the_summary_is_bound_not_discarded_on_the_stopped_path(self):
        assert "= _persist_summary(" in self._tails()["stopped"], "the returned summary is what the enqueue needs"

    def test_the_closure_applies_the_partial_fields(self):
        src = inspect.getsource(ps.PipelineService)
        i = src.index("def _persist_summary(")
        body = src[i : src.index("def _started_at_or_fallback", i)]
        assert "partial_summary_fields(" in body
