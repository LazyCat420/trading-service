"""The junior's MANDATORY market_context write, closed the same way as the
quant's signals: derived from the artifact when the agent skipped the tool step.

Measured 2026-08-26 (whiteboard_entries vs v3_agent_telemetry): 0/48 desks
carried market_context before 08-19, 54/62 (87%) since — a residual ~1-in-8
miss on the one section every downstream desk is told to read. The artifact's
`key_findings` is the promised content almost verbatim, so the fallback is a
selection, not a summary.

Unlike the quant hook, this one must NOT fire when the agent DID write:
write_section versions-and-supersedes, so an unconditional post would replace
the agent's own words with a mechanical digest.
"""

from types import SimpleNamespace

from app.v3 import agent_runner
from app.v3.agent_runner import _persist_junior_market_context


class _WhiteboardSpy:
    def __init__(self, existing=None):
        self.existing = existing
        self.writes = []

    async def get_section(self, ticker, cycle_id, section):
        return self.existing

    async def write_section(self, **kwargs):
        self.writes.append(kwargs)
        return "wb_test"


def _desk():
    return SimpleNamespace(ticker="TEST")


_ARTIFACT = {
    "key_findings": [
        "Q2 revenue +18% YoY ($4.2B vs $3.6B, 10-Q)",
        "Short interest doubled to 8.1% of float (FINRA)",
        "CFO resigned 08-20 with no successor named (8-K)",
        "A fourth finding that must be dropped by the top-3 cut",
    ],
    "catalyst_call": {
        "direction": "BEARISH",
        "catalyst": "CFO resignation into the Q3 print",
        "already_priced_in": False,
    },
}


async def test_absent_section_gets_derived_post(monkeypatch):
    spy = _WhiteboardSpy(existing=None)
    import app.agents.whiteboard as wb_mod
    monkeypatch.setattr(wb_mod, "whiteboard", spy)

    await _persist_junior_market_context(_desk(), "cycle-test", _ARTIFACT)

    assert len(spy.writes) == 1
    w = spy.writes[0]
    assert w["section"] == "market_context"
    assert w["author_agent"] == "v3_junior_analyst"
    text = w["content"]["text"]
    assert "Q2 revenue +18%" in text
    assert "BEARISH" in text and "CFO resignation" in text
    # top-3 cut holds
    assert "fourth finding" not in text
    # provenance is explicit — a reader can tell a derived post from the
    # agent's own prose
    assert w["content"]["derived_from_artifact"] is True


async def test_existing_section_is_never_superseded(monkeypatch):
    """The agent wrote it itself (the 87% case) — the fallback must not
    replace the agent's words with a digest."""
    spy = _WhiteboardSpy(existing={"id": "wb_real", "content": {"text": "agent prose"}})
    import app.agents.whiteboard as wb_mod
    monkeypatch.setattr(wb_mod, "whiteboard", spy)

    await _persist_junior_market_context(_desk(), "cycle-test", _ARTIFACT)

    assert spy.writes == []


async def test_no_stub_on_empty_artifact(monkeypatch):
    """Same doctrine as the quant's signals guard: an absent section reads as
    'the junior had nothing'; an empty one reads as data."""
    spy = _WhiteboardSpy(existing=None)
    import app.agents.whiteboard as wb_mod
    monkeypatch.setattr(wb_mod, "whiteboard", spy)

    await _persist_junior_market_context(
        _desk(), "cycle-test", {"key_findings": [], "catalyst_call": {}}
    )

    assert spy.writes == []


async def test_priced_in_flag_survives_derivation(monkeypatch):
    """`already_priced_in` is the half of the catalyst call that changes what
    a reader does with it — dropping it would turn 'correct but absorbed'
    into a fresh-looking edge."""
    spy = _WhiteboardSpy(existing=None)
    import app.agents.whiteboard as wb_mod
    monkeypatch.setattr(wb_mod, "whiteboard", spy)

    art = {
        "key_findings": ["One finding"],
        "catalyst_call": {
            "direction": "BULLISH",
            "catalyst": "buyback announced",
            "already_priced_in": True,
        },
    }
    await _persist_junior_market_context(_desk(), "cycle-test", art)

    assert "(already priced in)" in spy.writes[0]["content"]["text"]


def test_hook_is_pinned_to_the_junior():
    """The quant persist hooks once ran after EVERY agent and posted another
    agent's artifact under the quant's name (see
    TestQuantPersistenceRunsOnlyForTheQuant). Pin this hook's call site the
    same way: it must sit under an explicit v3_junior_analyst guard."""
    import inspect
    import re

    src = inspect.getsource(agent_runner)
    idx = src.find("await _persist_junior_market_context(")
    assert idx != -1, "the fallback is never called from the runner at all"
    head = src[:idx]
    guard_start = head.rfind("if agent_name == ")
    assert guard_start != -1, "the call has no agent_name guard before it"
    guard = src[guard_start:idx]
    names = set(re.findall(r'"(v3_[a-z_]+)"', guard))
    assert names == {"v3_junior_analyst"}, (
        f"the guard covers {sorted(names)}, not the junior alone"
    )
