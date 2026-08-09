"""The transcript and the operator's channel (app/v3/agent_chat.py)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.v3.agent_chat import (
    MESSAGE_CHARS,
    chat_line_for,
    directive_block,
    emit_agent_message,
)


class _Emitter:
    """Captures emit(...) calls the way the orchestrator makes them."""

    def __init__(self):
        self.calls = []

    def __call__(self, phase, step, detail, status=None, data=None):
        self.calls.append({"phase": phase, "step": step, "detail": detail,
                           "status": status, "data": data or {}})


class TestTheMessageSurvivesTheRelay:
    """trading-client's SSE relay strips `content`, `full_text`,
    `response_text` and friends from event.data before forwarding. A
    transcript posted under one of those keys arrives EMPTY at the browser and
    reads as an agent that said nothing."""

    STRIPPED_BY_THE_RELAY = {
        "raw_response", "system_prompt", "user_prompt", "context",
        "full_text", "prompt_text", "response_text", "content",
        "html", "body", "articles", "raw_html",
    }

    def test_the_text_is_not_under_a_stripped_key(self):
        emit = _Emitter()
        emit_agent_message(emit, speaker="Bear", ticker="DE", text="Entry is poor at spot.")

        data = emit.calls[0]["data"]
        assert data["message"] == "Entry is poor at spot."
        assert not (self.STRIPPED_BY_THE_RELAY & set(data)), (
            "the relay would strip this key and the operator would see an "
            "empty chat bubble"
        )

    def test_the_kind_is_what_the_widget_filters_on(self):
        emit = _Emitter()
        emit_agent_message(emit, speaker="Bull", ticker="DE", text="Cheap at 12x.")
        assert emit.calls[0]["data"]["kind"] == "agent_message"


class TestTheMessageIsBounded:
    def test_a_long_narrative_is_truncated(self):
        emit = _Emitter()
        emit_agent_message(emit, speaker="Judge", ticker="DE", text="x" * 5000)
        msg = emit.calls[0]["data"]["message"]
        assert len(msg) <= MESSAGE_CHARS
        assert msg.endswith("…")

    def test_an_empty_message_is_not_emitted(self):
        """A blank bubble is a claim that the agent said nothing."""
        emit = _Emitter()
        emit_agent_message(emit, speaker="Bull", ticker="DE", text="   \n ")
        assert emit.calls == []


class TestAnObserverNeverBreaksTheCycle:
    def test_a_failing_emit_is_swallowed(self):
        def boom(*a, **k):
            raise RuntimeError("event bus down")

        emit_agent_message(boom, speaker="Bull", ticker="DE", text="hello")


class TestChatLineExtraction:
    def test_the_bull_speaks(self):
        line = chat_line_for("bull_argument", {
            "summary": "Beat streak intact.", "confidence": 71,
        })
        assert line["speaker"] == "Bull"
        assert line["role"] == "debate"
        assert line["text"] == "Beat streak intact."
        assert line["confidence"] == 71

    def test_stance_is_read_from_the_field_the_desk_acts_on(self):
        """Not inferred from the prose — the Board reads `winner`, so the
        transcript must show `winner`."""
        assert chat_line_for("debate_judge", {
            "summary": "The bear's entry objection stands.",
            "winner": "bear", "final_confidence": 64,
        })["stance"] == "bear"

        assert chat_line_for("fundamental_report", {
            "summary": "Margins stable.", "thesis_direction": "NEUTRAL",
        })["stance"] == "NEUTRAL"

    def test_final_confidence_is_used_when_confidence_is_absent(self):
        assert chat_line_for("bull_defense", {
            "summary": "Conceding the entry point.", "final_confidence": 58,
        })["confidence"] == 58

    def test_the_bears_substitute_rides_along(self):
        line = chat_line_for("bear_rebuttal", {
            "summary": "Own DE instead.",
            "preferred_alternative": {"status": "NAMED", "ticker": "DE",
                                      "reason": "cheaper entry"},
        })
        assert line["extra"]["preferred_alternative"] == {
            "status": "NAMED", "ticker": "DE",
        }

    def test_a_declined_substitute_is_still_shown(self):
        """DECLINED is a real answer and must not look like silence."""
        line = chat_line_for("bear_rebuttal", {
            "summary": "Nothing better on the board.",
            "preferred_alternative": {"status": "DECLINED", "ticker": None},
        })
        assert line["extra"]["preferred_alternative"]["status"] == "DECLINED"


class TestSectionsWithoutANarrativeAreSilent:
    @pytest.mark.parametrize("section,artifact", [
        ("regime_classification", {"regime": "DEEP_DISCOUNT"}),
        ("bull_argument", {"claims": []}),          # no summary
        ("bull_argument", {"summary": "   "}),      # blank summary
        ("not_a_section", {"summary": "hello"}),
    ])
    def test_no_line(self, section, artifact):
        assert chat_line_for(section, artifact) is None

    def test_a_non_dict_artifact_is_silent(self):
        assert chat_line_for("bull_argument", "just a string") is None
        assert chat_line_for("bull_argument", None) is None


class TestTheDirectiveBlock:
    def test_no_directives_renders_nothing(self):
        """An empty header reads to the model as 'the operator said nothing',
        which is a message it never sent."""
        assert directive_block([]) == ""

    def test_a_directive_is_rendered_with_its_standing(self):
        block = directive_block([{"directive": "Check the 2031 Broadcom contract."}])
        assert "OPERATOR DIRECTIVE" in block
        assert "Check the 2031 Broadcom contract." in block

    def test_the_agent_is_told_it_may_refuse(self):
        """A directive that cannot be refused is a way to talk the desk into a
        trade the data does not support."""
        block = directive_block([{"directive": "Buy it."}])
        assert "say so in your artifact" in block

    def test_a_blank_directive_contributes_no_bullet(self):
        block = directive_block([{"directive": "   "}, {"directive": "Real one."}])
        assert block.count("- ") == 1


class TestDirectiveInjectionIntoTheRun:
    """The end the operator actually experiences: type a sentence, and the
    next agent's prompt carries it."""

    class _Agent:
        AGENT_NAME = "v3_bear_agent"
        ARTIFACT_TYPE = "bear_rebuttal"
        TOOL_WHITELIST = ["get_market_data"]
        SYSTEM_PROMPT = "You are the bear."

    def _desk(self):
        from app.v3.shared_desk import SharedDesk
        d = SharedDesk(ticker="DE", cycle_id="cycle-test")
        d.cycle_metadata = {"ticker": "DE", "agent_locale": "default"}
        return d

    @pytest.mark.asyncio
    async def test_a_pending_directive_reaches_the_prompt_and_is_consumed(self):
        from app.v3.agent_runner import run_v3_agent

        calls = []
        consumed = []

        async def _run(**kwargs):
            calls.append(kwargs)
            return {"response": '{"summary": "ok", "rebuttals": [], '
                                '"independent_risks": [], "confidence": 60}',
                    "tokens_used": 10, "loops_used": 1, "stop_reason": "completed"}

        with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)), \
             patch("app.v3.agent_chat.pending_directives",
                   return_value=[{"id": 7, "directive": "Name a substitute you would actually own."}]), \
             patch("app.v3.agent_chat.mark_directives_consumed",
                   side_effect=lambda ids, consumed_by: consumed.append((ids, consumed_by))):
            await run_v3_agent(desk=self._desk(), agent_module=self._Agent,
                               cycle_id="cycle-test", bot_id="b1")

        prompt = calls[0]["user_prompt"]
        assert "OPERATOR DIRECTIVE" in prompt
        assert "Name a substitute you would actually own." in prompt
        assert consumed == [([7], "v3_bear_agent")], (
            "a directive that is not retired becomes standing policy nobody "
            "remembers setting"
        )

    @pytest.mark.asyncio
    async def test_no_directive_adds_no_section(self):
        from app.v3.agent_runner import run_v3_agent

        calls = []

        async def _run(**kwargs):
            calls.append(kwargs)
            return {"response": '{"summary": "ok", "rebuttals": [], '
                                '"independent_risks": [], "confidence": 60}',
                    "tokens_used": 10, "loops_used": 1, "stop_reason": "completed"}

        with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)), \
             patch("app.v3.agent_chat.pending_directives", return_value=[]):
            await run_v3_agent(desk=self._desk(), agent_module=self._Agent,
                               cycle_id="cycle-test", bot_id="b1")

        assert "OPERATOR DIRECTIVE" not in calls[0]["user_prompt"]

    @pytest.mark.asyncio
    async def test_a_directive_lookup_failure_does_not_kill_the_run(self):
        """The channel is optional; the cycle is not."""
        from app.v3.agent_runner import run_v3_agent
        from app.v3.shared_desk import PhaseOutcome

        async def _run(**kwargs):
            return {"response": '{"summary": "ok", "rebuttals": [], '
                                '"independent_risks": [], "confidence": 60}',
                    "tokens_used": 10, "loops_used": 1, "stop_reason": "completed"}

        with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)), \
             patch("app.v3.agent_chat.pending_directives",
                   side_effect=RuntimeError("db down")):
            outcome = await run_v3_agent(desk=self._desk(), agent_module=self._Agent,
                                         cycle_id="cycle-test", bot_id="b1")

        assert outcome != PhaseOutcome.AGENT_ERROR
