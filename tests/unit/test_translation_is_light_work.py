"""The foreign-feed translator is a collector, not a decision agent.

Measured on cycle-v3-1788479270 (2026-09-03), from prism's request ledger:

  * every translation went through `/agent` and carried ~25,900 input tokens
    for a ~50-token answer — prism attaches the MCP catalog (~21k) server-side
    and injects the persona's stored memories, of which there were ten
    near-duplicate "user is a financial translator" entries;
  * each call then triggered `memory:extract` + `memory:embed`, writing ANOTHER
    such memory. A news collector was accumulating agent memories about the
    headlines it read: 1,723 on the janitor persona;
  * mean latency 16.1 s against a 5 s `asyncio.wait_for`, so **59 translations
    "failed/timed out" in one cycle** while the box completed every one. The
    work was done, thrown away, and paid for;
  * `agent_id="translator"` is in no entry of AGENT_ID_MAP, so the ledger filed
    it under CUSTOM_SYSTEM_JANITOR_AGENT — which is why it looked like a
    janitor agent running during the news scrape.
"""
import asyncio
import inspect

import pytest

from app.collectors import news_collector as nc


class TestItUsesTheToolLessPath:
    def test_it_does_not_call_the_agent_endpoint(self):
        """By ast, not by grep: the docstring NAMES the rejected function, and a
        text search would read that explanation as the defect it warns about."""
        import ast

        tree = ast.parse(inspect.getsource(nc._translate_foreign_text).lstrip())
        called = {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        imported = {
            alias.name for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) for alias in n.names
        }
        assert "call_prism_agent" not in called | imported, (
            "/agent attaches the MCP catalog and the persona's memories "
            "server-side; a headline translation pays ~21k tokens for neither"
        )
        assert "chat_toolless" in called and "chat_toolless" in imported

    @pytest.mark.asyncio
    async def test_it_sends_the_prompt_to_the_resolved_box(self, monkeypatch):
        seen = {}

        async def fake_resolve(agent_name, **kw):
            seen["agent_name"] = agent_name
            return "nemotron35", "vllm"

        async def fake_chat(**kw):
            seen.update(kw)
            return {"response": "Is a rate hike still not ruled out?"}

        import app.services.prism_agent_caller as pac
        monkeypatch.setattr(pac, "resolve_default_model_for_agent", fake_resolve)
        monkeypatch.setattr(pac, "chat_toolless", fake_chat)

        out = await nc._translate_foreign_text(
            "Ist eine Zinserhöhung doch noch nicht ausgemacht?", "Handelsblatt DE")

        assert out == "Is a rate hike still not ruled out?"
        assert seen["agent_name"] == "translator"
        assert seen["provider"] == "vllm" and seen["model"] == "nemotron35"
        assert "Handelsblatt DE" in seen["user_prompt"]

    def test_the_name_routes_to_the_jetson(self):
        """COLLECTOR_KEYWORDS is what makes the box choice; pin the name."""
        import app.services.prism_agent_caller as pac
        src = inspect.getsource(nc._translate_foreign_text)
        assert '"translator"' in src
        assert pac.is_collector_agent("translator")


class TestTheDeadline:
    def test_the_budget_exceeds_the_measured_latency(self):
        assert nc._TRANSLATE_TIMEOUT_S >= 20.0, (
            "measured mean 16.1 s on the Jetson; a 5 s deadline discarded 59 "
            "completed translations in one cycle"
        )

    @pytest.mark.asyncio
    async def test_a_timeout_keeps_the_original_text(self, monkeypatch):
        async def fake_resolve(agent_name, **kw):
            return "nemotron35", "vllm"

        async def never(**kw):
            await asyncio.sleep(3600)

        import app.services.prism_agent_caller as pac
        monkeypatch.setattr(pac, "resolve_default_model_for_agent", fake_resolve)
        monkeypatch.setattr(pac, "chat_toolless", never)
        monkeypatch.setattr(nc, "_TRANSLATE_TIMEOUT_S", 0.01)

        original = "Ist eine Zinserhöhung doch noch nicht ausgemacht?"
        assert await nc._translate_foreign_text(original, "Handelsblatt DE") == original

    @pytest.mark.asyncio
    async def test_a_failure_keeps_the_original_text(self, monkeypatch):
        async def fake_resolve(agent_name, **kw):
            return "nemotron35", "vllm"

        async def boom(**kw):
            raise RuntimeError("connect timeout")

        import app.services.prism_agent_caller as pac
        monkeypatch.setattr(pac, "resolve_default_model_for_agent", fake_resolve)
        monkeypatch.setattr(pac, "chat_toolless", boom)

        original = "Das verändert die Kalkulation an den Märkten."
        assert await nc._translate_foreign_text(original, "Handelsblatt DE") == original

    @pytest.mark.asyncio
    async def test_an_unresolvable_box_keeps_the_original_text(self, monkeypatch):
        async def no_box(agent_name, **kw):
            import app.services.prism_agent_caller as pac
            raise pac.ModelUnavailableError("both boxes down")

        import app.services.prism_agent_caller as pac
        monkeypatch.setattr(pac, "resolve_default_model_for_agent", no_box)
        original = "Zwei hochrangige Notenbanker sprechen sich dagegen aus."
        assert await nc._translate_foreign_text(original, "Handelsblatt DE") == original

    @pytest.mark.asyncio
    async def test_short_text_never_reaches_a_box(self, monkeypatch):
        async def explode(agent_name, **kw):
            raise AssertionError("must not resolve a model for trivial text")

        import app.services.prism_agent_caller as pac
        monkeypatch.setattr(pac, "resolve_default_model_for_agent", explode)
        assert await nc._translate_foreign_text("DAX", "Handelsblatt DE") == "DAX"
        assert await nc._translate_foreign_text("", "Handelsblatt DE") == ""
