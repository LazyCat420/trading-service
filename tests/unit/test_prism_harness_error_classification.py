"""A prism harness error that names a transport fault must keep its retry budget.

MEASURED 2026-09-05, cycle-v3-1788646388. GOOG's bull agent burned **1,238
seconds** and produced nothing:

    23:05:36  Prism stream error: Provider stream stalled: no data for 300s
    23:06:16  Prism stream error: Provider stream stalled: no data for 300s
    23:11:38  stall #3 -> attempt 1 fails as TRANSIENT, aresilient_call retries
    23:20:46  stall #4, inside prism's loop on iteration 3
    23:20:53  "Attempt 2/5 failed: RuntimeError: Prism harness error for
               v3_bull_agent (model GLM-5.3-Flash-EXL3): ⚠️ **Error:** The model
               provider encountered an error on iteration 3: `Provider stream
               stalled: no data received for 300s`"  [fatal]  final=True
    23:20:53  [RESILIENCE] all 5 attempts failed (last: fatal)
    23:20:53  [V3Runner] v3_bull_agent CRASHED for GOOG

Both siblings succeeded on the same stage of the same cycle (834 s, 869 s), so
this was the box, not the prompt or the ticker.

THE ASYMMETRY. A stall on the first token arrives as a stream error and is
TRANSIENT — five attempts. The SAME stall on iteration 3 arrives as prism's
injected `⚠️ **Error:**` assistant message; base_agent detects the marker and
raises a RuntimeError; `classify_exception` has no branch for it, so it is
FATAL, and `_should_stop` (FATAL and attempt > 1) ends the run at 2 of 5. One
fault, two retry budgets, decided by which iteration it happened on.

Our side owns the conversion (prism is read-only), so the fix is here: raise a
type the SDK has been told is retryable when the marker names a transport
cause, and keep the plain RuntimeError when it names something the model did.
"""
from __future__ import annotations

import pytest

from lazycat.resilience import FailureType, classify_exception

from app.agents.base_agent import (
    PrismTransientHarnessError,
    classify_prism_harness_error,
)

# Verbatim from cycle-v3-1788646388, pipeline_events phase="recovery".
GOOG_BULL_23_20_53 = (
    "⚠️ **Error:** The model provider encountered an error on iteration 3: "
    "`Provider stream stalled: no data received for 300s`. The conversation "
    "has been preserved."
)


class TestTheLiveSpecimen:
    def test_the_stall_is_transient(self):
        exc = classify_prism_harness_error("v3_bull_agent", "GLM-5.3-Flash-EXL3",
                                           GOOG_BULL_23_20_53)
        assert isinstance(exc, PrismTransientHarnessError)
        assert classify_exception(exc) is FailureType.TRANSIENT

    def test_it_would_have_kept_all_five_attempts(self):
        """`_should_stop` is FATAL-and-attempt>1. A TRANSIENT classification
        means the run is not abandoned at attempt 2."""
        from lazycat.resilience import _should_stop

        exc = classify_prism_harness_error("v3_bull_agent", "GLM", GOOG_BULL_23_20_53)
        assert not _should_stop(exc, classify_exception(exc), attempt=2)

    def test_the_message_still_names_the_agent_the_model_and_the_cause(self):
        exc = classify_prism_harness_error("v3_bull_agent", "GLM-5.3-Flash-EXL3",
                                           GOOG_BULL_23_20_53)
        text = str(exc)
        assert "v3_bull_agent" in text
        assert "GLM-5.3-Flash-EXL3" in text
        assert "stalled" in text


class TestWhatStaysFatal:
    @pytest.mark.parametrize(
        "head,why",
        [
            ("⚠️ **Error:** context window is critically full",
             "a real refusal: retrying sends the same oversized prompt"),
            ("⚠️ **Error:** the requested model does not exist",
             "not found is not transient"),
            ("Summarizing progress so far", "a harness state, not a transport fault"),
        ],
    )
    def test_a_non_transport_marker_stays_fatal(self, head, why):
        exc = classify_prism_harness_error("v3_bull_agent", "GLM", head)

        assert not isinstance(exc, PrismTransientHarnessError), why
        assert type(exc) is RuntimeError
        assert classify_exception(exc) is FailureType.FATAL, why

    def test_an_unknown_marker_defaults_to_fatal(self):
        """Fail closed: an unrecognised error must not silently earn five
        retries of an expensive 24k-token prompt."""
        exc = classify_prism_harness_error("a", "m", "⚠️ **Error:** something new")
        assert classify_exception(exc) is FailureType.FATAL


class TestTheTransportVocabulary:
    @pytest.mark.parametrize(
        "phrase",
        [
            "Provider stream stalled: no data received for 300s",
            "Request timed out after 300000ms",
            "upstream connect error or disconnect/reset before headers",
            "502 Bad Gateway",
            "503 Service Unavailable",
            "504 Gateway Timeout",
            "socket hang up",
            "ECONNRESET",
        ],
    )
    def test_every_transport_phrase_we_have_seen_is_transient(self, phrase):
        exc = classify_prism_harness_error("a", "m", f"⚠️ **Error:** {phrase}")
        assert classify_exception(exc) is FailureType.TRANSIENT, phrase

    def test_matching_is_case_insensitive(self):
        exc = classify_prism_harness_error("a", "m", "⚠️ **Error:** PROVIDER STREAM STALLED")
        assert classify_exception(exc) is FailureType.TRANSIENT


class TestTheTypeIsRegisteredAtImport:
    def test_importing_base_agent_registers_the_name(self):
        """The SDK matches on the class NAME, so the registration has to have
        happened by the time any agent runs. Importing base_agent is what every
        caller does first."""
        from lazycat.resilience import RETRYABLE_EXCEPTION_NAMES

        assert "PrismTransientHarnessError" in RETRYABLE_EXCEPTION_NAMES

    def test_the_raise_site_uses_the_classifier(self):
        """The seam, by AST: base_agent must not construct a bare RuntimeError
        for the harness-marker case again."""
        import ast
        import inspect
        import pathlib

        from app.agents import base_agent

        src = pathlib.Path(inspect.getsourcefile(base_agent)).read_text()
        tree = ast.parse(src)

        bare = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id == "RuntimeError"
            and any(
                isinstance(a, ast.JoinedStr)
                and "Prism harness error" in ast.unparse(a)
                for a in node.exc.args
            )
        ]
        assert not bare, (
            f"base_agent still raises a bare RuntimeError for a prism harness "
            f"marker at line(s) {bare} — a stall raised that way is classified "
            "FATAL and the run ends at attempt 2 of 5"
        )
