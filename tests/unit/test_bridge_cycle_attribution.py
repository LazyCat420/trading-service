"""The HTTP tool bridge must attribute its logs to the running cycle.

THE DEFECT THIS PINS. V3 agents execute inside prism-service, so every tool
they call arrives at `agent_tools_router.execute_tool`. lazy-tool forwards a
Prism conversation UUID where a cycle id belongs; `_UUID_RE` correctly refuses
it, and the cycle ContextVar was therefore never set on that path. Two readers
then disagreed about "the current cycle":

  * `registry` calls `current_cycle_id()`, which falls back to the live
    pipeline singleton — so `agent_tool_telemetry` got the right cycle id.
  * `DbLoggingHandler` must call `current_cycle_id_or_none()` (the warning
    inside `current_cycle_id` is itself a log record and re-enters the
    handler) — so every log line fell through to the literal 'system-log'.

Measured in cycle-v3-1786455000: 484 'system-log' rows against 405 attributed
ones, 431 of them `[scraper_client]` warnings raised inside `get_finnhub_news`.
"""
import pytest

from app.tools import tool_context as tc

PRISM_CONVERSATION_UUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
LIVE_CYCLE = "cycle-v3-1786455000"


@pytest.fixture(autouse=True)
def _clean_context():
    """Each test starts with no ambient cycle and leaves none behind."""
    tc.clear_tool_context()
    yield
    tc.clear_tool_context()


@pytest.fixture
def live_pipeline(monkeypatch):
    """Pretend a cycle is mid-flight, as it is whenever an agent calls a tool."""
    monkeypatch.setattr(tc, "_running_pipeline_cycle_id", lambda: LIVE_CYCLE)


def test_uuid_is_still_refused_as_a_cycle_id():
    """The guard that started this must not be weakened by the fix."""
    assert tc.resolve_cycle_id(PRISM_CONVERSATION_UUID) != PRISM_CONVERSATION_UUID
    with tc.tool_context(cycle_id=PRISM_CONVERSATION_UUID):
        assert tc.current_cycle_id_or_none() != PRISM_CONVERSATION_UUID


def test_resolve_falls_back_to_the_live_cycle_when_given_a_uuid(live_pipeline):
    """A UUID must resolve to the running cycle, not to nothing."""
    assert tc.resolve_cycle_id(PRISM_CONVERSATION_UUID) == LIVE_CYCLE


def test_resolve_prefers_a_real_candidate_over_the_singleton(live_pipeline):
    assert tc.resolve_cycle_id("cycle-v3-explicit") == "cycle-v3-explicit"


def test_resolve_returns_none_with_nothing_to_go_on(monkeypatch):
    monkeypatch.setattr(tc, "_running_pipeline_cycle_id", lambda: None)
    monkeypatch.delenv("CYCLE_ID", raising=False)
    assert tc.resolve_cycle_id(PRISM_CONVERSATION_UUID) is None
    assert tc.resolve_cycle_id(None) is None


def test_both_readers_agree_once_the_bridge_scopes_the_resolved_id(live_pipeline):
    """The regression itself: the log reader and the telemetry reader must match.

    Without the bridge resolving the id, `current_cycle_id_or_none()` returns
    None here and the log handler writes 'system-log' while `current_cycle_id()`
    returns the real cycle — the exact split that produced 484 misfiled rows.
    """
    # What the bridge now does with a forwarded Prism UUID.
    resolved = tc.resolve_cycle_id(PRISM_CONVERSATION_UUID)
    with tc.tool_context(cycle_id=resolved, ticker="ASIC", phase="agent_tool"):
        log_reader = tc.current_cycle_id_or_none()
        telemetry_reader = tc.current_cycle_id()

        assert log_reader == telemetry_reader == LIVE_CYCLE
        # The value the DbLoggingHandler would have written before the fix.
        assert log_reader != "system-log" and log_reader is not None


def test_unscoped_bridge_call_still_reproduces_the_old_split(live_pipeline):
    """Positive control: WITHOUT the resolution the two readers still diverge.

    If this ever stops diverging, the fix above is no longer what is doing the
    work and this suite would be passing for the wrong reason.
    """
    with tc.tool_context(cycle_id=PRISM_CONVERSATION_UUID, ticker="ASIC"):
        assert tc.current_cycle_id_or_none() is None      # → 'system-log'
        assert tc.current_cycle_id() == LIVE_CYCLE        # → correctly attributed


def test_tool_context_restores_the_previous_ticker(live_pipeline):
    """Scoped, not imperative: uvicorn reuses tasks between requests.

    `set_tool_context` had no teardown, so one request's ticker could survive
    into the next request's log rows.
    """
    with tc.tool_context(cycle_id=LIVE_CYCLE, ticker="META", phase="agent_tool"):
        assert tc.current_ticker() == "META"
        with tc.tool_context(ticker="ASIC", phase="agent_tool"):
            assert tc.current_ticker() == "ASIC"
        assert tc.current_ticker() == "META"


def test_phase_is_recorded_for_bridge_calls(live_pipeline):
    """These rows carried phase='unknown'; 'agent_tool' is the honest name."""
    with tc.tool_context(cycle_id=LIVE_CYCLE, ticker="ASIC", phase="agent_tool"):
        assert tc.current_phase() == "agent_tool"


# ── the ROUTER, not just the helper ─────────────────────────────────────
#
# Everything above proves `resolve_cycle_id` works. None of it proves the
# endpoint CALLS it — and the defect was never in the helper, it was in the
# one call site. These drive the real handler.

@pytest.mark.asyncio
async def test_endpoint_scopes_the_resolved_cycle_around_execution(live_pipeline, monkeypatch):
    """What a tool logging a warning mid-execution would be attributed to."""
    from app.routers import agent_tools_router as router_mod

    seen = {}

    async def _fake_execute(tool_call, **kwargs):
        # Exactly what DbLoggingHandler reads while the tool is running.
        seen["log_reader"] = tc.current_cycle_id_or_none()
        seen["phase"] = tc.current_phase()
        seen["ticker"] = tc.current_ticker()
        seen["passed_cycle_id"] = kwargs.get("cycle_id")
        return {"ok": True}

    from app.tools.registry import registry as _registry
    monkeypatch.setattr(_registry, "execute_tool_call", _fake_execute)

    payload = router_mod.ToolExecutePayload(
        tool_name="get_finnhub_news",
        arguments={"ticker": "ASIC"},
        agent_name="CUSTOM_V3_JUNIOR_ANALYST",
        ticker="ASIC",
        cycle_id=PRISM_CONVERSATION_UUID,   # what lazy-tool actually forwards
    )
    result = await router_mod.execute_tool(payload, token="test-key")

    assert result == {"ok": True}
    # The row that would have said 'system-log' 431 times.
    assert seen["log_reader"] == LIVE_CYCLE
    assert seen["phase"] == "agent_tool"
    assert seen["ticker"] == "ASIC"
    # And the telemetry writer is handed the same id, not the UUID.
    assert seen["passed_cycle_id"] == LIVE_CYCLE


@pytest.mark.asyncio
async def test_endpoint_leaves_no_context_behind(live_pipeline, monkeypatch):
    """uvicorn reuses tasks; a leaked ticker mislabels the NEXT request."""
    from app.routers import agent_tools_router as router_mod

    async def _fake_execute(tool_call, **kwargs):
        return {"ok": True}

    from app.tools.registry import registry as _registry
    monkeypatch.setattr(_registry, "execute_tool_call", _fake_execute)

    payload = router_mod.ToolExecutePayload(
        tool_name="get_finnhub_news", ticker="ASIC",
        cycle_id=PRISM_CONVERSATION_UUID,
    )
    await router_mod.execute_tool(payload, token="test-key")

    assert tc.current_ticker() is None
    assert tc.current_phase() is None
