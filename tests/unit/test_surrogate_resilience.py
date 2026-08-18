"""Surrogate sanitisation in the RLM audit trail.

This used to patch `rlm_audit.get_db` and inspect `db.execute` params for lone
surrogates. `app/services/rlm_audit.py` writes through `mongo_store` now and
imports no `get_db`, so the patch intercepted nothing: the mock's execute count
came from a MagicMock that was never called by the code, the surrogate check
looped over an empty list, and the real write went to the live store.

It patches `mongo_store` now and sweeps every value in every document handed to
`upsert_doc`/`insert_docs` — which covers strictly more than the old param
scan, because the blob content and the audit record both pass through it.
"""
import pytest
from unittest.mock import MagicMock, patch

from app.services.rlm_audit import log_rlm_audit_trail


def _strings(obj):
    """Every string reachable in a document the module handed to Mongo."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def test_log_rlm_audit_trail_sanitizes_surrogates():
    """Verify that log_rlm_audit_trail sanitizes surrogates and doesn't throw UnicodeEncodeError."""
    store = MagicMock()

    surrogate_text = "Analysis report with surrogates \ud83d\udcbb"

    with patch("app.services.rlm_audit.mongo_store", store):
        try:
            log_rlm_audit_trail(
                cycle_id="test-cycle",
                bot_id="test-bot",
                ticker="AAPL",
                context=surrogate_text,
                trading_system_prompt="System prompt \ud83d",
                active_model="model",
                response_text=surrogate_text,
                tokens_used=100,
                execution_time=1.0,
                completion_tokens=50
            )
        except UnicodeEncodeError as e:
            pytest.fail(f"log_rlm_audit_trail raised UnicodeEncodeError: {e}")

        # Two context_blobs upserts (context + system prompt) and one audit row.
        assert store.upsert_doc.call_count == 2
        assert store.insert_docs.call_count == 1

        # Ensure every string the module wrote is free of surrogates.
        written = list(store.upsert_doc.call_args_list) + list(store.insert_docs.call_args_list)
        assert written, "nothing was written — the sanitisation check would be vacuous"
        checked = 0
        for call in written:
            for s in _strings(call.args):
                checked += 1
                assert "\ud83d" not in s
                assert "\udcbb" not in s
        assert checked > 0
