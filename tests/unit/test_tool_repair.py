"""Pre-flight tool-argument repair.

The failure being repaired is measured, not hypothetical: 18 calls rejected for
a missing required `ticker` over 2026-07-28..30, with `get_sec_filings` at 26.3%
failure over 14 days. The payloads in `test_repairs_the_real_production_payloads`
are copied verbatim from `agent_traces`.

The tests that matter most here are the ones asserting what this does NOT do.
Injection is fail-closed, because 29 tools require a `ticker` and four of them
place orders or mutate watch state. "Repair a malformed buy order by guessing
the ticker" is not a repair.
"""

from __future__ import annotations

import json
import os

import pytest

from app.v3 import tool_repair
from app.v3.tool_repair import (
    REPAIRABLE_TICKER_TOOLS,
    bare_tool_name,
    make_pre_tool_hook,
    repair_tool_arguments,
)


def _repair(tool, args, ticker="NVDA", **kw):
    """Repair without touching the database."""
    kw.setdefault("record", False)
    return repair_tool_arguments(tool, args, ticker=ticker, **kw)


# ── What it repairs ──────────────────────────────────────────────────────


def test_injects_the_missing_ticker():
    args = {"action": "facts"}
    out = _repair("get_sec_filings", args)

    assert out == ["ticker"]
    assert args["ticker"] == "NVDA"


def test_repairs_through_the_mcp_namespace():
    """Live calls arrive namespaced; the allow-list holds bare names."""
    args = {"action": "facts"}

    assert _repair("mcp__lazy-tool-service__get_sec_filings", args) == ["ticker"]
    assert args["ticker"] == "NVDA"


def test_repairs_the_real_production_payloads():
    """Verbatim from agent_traces — the calls that actually failed.

    Both shapes lost `ticker` to un-escaped JSON in `content`, which is why the
    junk-key filter dropped it: the key never parsed as a key.
    """
    # Top-level keys verbatim from the failing rows: author, content, section.
    # `section` IS present — the Python signature is
    # whiteboard_write(ticker, section, content, author="") and the TypeError
    # named exactly one missing argument, which is how we know `ticker` was the
    # only casualty. Omitting `section` from this fixture would make the test
    # pass for the wrong reason.
    junior = {
        "author": "v3_junior_analyst",
        "section": "market_context",
        "content": json.dumps({"market_context": {"ticker": "SOFI",
                                                  "price": "$15.25"}}),
    }
    assert _repair("mcp__lazy-tool-service__whiteboard_write", junior,
                   ticker="SOFI") == ["ticker"]
    assert junior["ticker"] == "SOFI"

    fundamental = {"action": "facts", "form_type": "10-K"}
    assert _repair("mcp__lazy-tool-service__get_sec_filings", fundamental,
                   ticker="HCA") == ["ticker"]
    assert fundamental["ticker"] == "HCA"


def test_repair_is_in_place_because_that_is_the_mechanism():
    """The SDK passes this same dict to execute_tool after the hook returns."""
    args = {}
    same = args
    _repair("get_market_data", args)

    assert same is args
    assert args["ticker"] == "NVDA"


def test_ticker_is_normalised():
    args = {}
    _repair("get_market_data", args, ticker="  nvda  ")

    assert args["ticker"] == "NVDA"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_replaces_a_blank_ticker(blank):
    """An empty string fails the schema exactly as a missing key does."""
    args = {"ticker": blank}
    assert _repair("get_sec_filings", args) == ["ticker"]
    assert args["ticker"] == "NVDA"


# ── What it must NOT do: fail-closed safety ──────────────────────────────


@pytest.mark.parametrize("tool", [
    "buy_stock", "sell_stock",              # place orders
    "add_to_watchlist", "remove_from_watchlist", "watch_ticker",
    "escalate_to_pm", "request_peer_analysis",
    "save_trading_chart", "run_equation", "run_backtest",
])
def test_never_repairs_a_state_changing_tool(tool):
    """All ten require `ticker`. A malformed order must FAIL, not be completed.

    Guessing the ticker for `buy_stock` does not recover a lost call; it invents
    a trade nobody asked for.
    """
    args = {"size_pct": 0.1}
    assert _repair(tool, args) == []
    assert "ticker" not in args


def test_an_unknown_tool_is_not_repaired():
    """Fail closed: a tool added later must be reasoned about, not inherit this."""
    args = {}
    assert _repair("some_new_tool_added_next_month", args) == []
    assert args == {}


def test_never_overwrites_a_ticker_the_model_chose():
    """Peer research is legitimate: the desk is NVDA, the question is about AMD.

    Overwriting would convert good research into a confident wrong answer.
    """
    args = {"ticker": "AMD"}
    assert _repair("get_finviz_fundamentals", args, ticker="NVDA") == []
    assert args["ticker"] == "AMD"


def test_no_context_ticker_means_no_repair():
    args = {}
    assert _repair("get_sec_filings", args, ticker="") == []
    assert args == {}


def test_a_non_string_ticker_is_left_alone():
    """Coercing a structured value would guess at the model's intent."""
    args = {"ticker": ["NVDA", "AMD"]}
    assert _repair("get_sec_filings", args) == []
    assert args["ticker"] == ["NVDA", "AMD"]


def test_non_dict_arguments_are_survived():
    assert _repair("get_sec_filings", None) == []
    assert _repair("get_sec_filings", "not a dict") == []


# ── The hook contract ────────────────────────────────────────────────────


def test_the_hook_never_blocks():
    """A non-None return BLOCKS the call in the SDK. This must never do that."""
    hook = make_pre_tool_hook(ticker="NVDA", agent_name="a", cycle_id="c")
    args = {"action": "facts"}

    assert hook("get_sec_filings", args) is None
    assert args["ticker"] == "NVDA"          # repaired all the same
    assert hook("buy_stock", {}) is None     # and still None when it declines


def test_the_hook_never_raises(monkeypatch):
    """`on_tool_call` is called UNGUARDED by the SDK (lazycat/agent.py:320).

    Unlike `on_tool_result`, there is no try/except around it, so a raise here
    would abort the agent's turn. This try/except is load-bearing.
    """
    def _boom(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(tool_repair, "repair_tool_arguments", _boom)
    hook = make_pre_tool_hook(ticker="NVDA")

    assert hook("get_sec_filings", {}) is None


def test_the_hook_records_the_repair(monkeypatch):
    """A repaired call is still a model defect; it must stay measurable.

    Once repaired it vanishes from the tool-failure telemetry it used to appear
    in, so without this record the upstream bad-JSON bug becomes invisible —
    the exact laundering the invariants module exists to prevent.
    """
    seen = []
    monkeypatch.setattr(
        "app.v3.invariants.record_violation",
        lambda kind, **d: seen.append({"kind": kind, **d}) or kind,
    )
    hook = make_pre_tool_hook(ticker="NVDA", agent_name="v3_fundamental_analyst",
                              cycle_id="cycle-1")
    hook("mcp__lazy-tool-service__get_sec_filings", {"action": "facts"})

    assert len(seen) == 1
    assert seen[0]["kind"] == tool_repair.KIND_ARGS_REPAIRED
    assert seen[0]["tool"] == "get_sec_filings"
    assert seen[0]["ticker"] == "NVDA"
    assert seen[0]["agent"] == "v3_fundamental_analyst"
    assert seen[0]["fields"] == ["ticker"]


def test_a_declined_tool_records_nothing(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "app.v3.invariants.record_violation",
        lambda kind, **d: seen.append(kind) or kind,
    )
    make_pre_tool_hook(ticker="NVDA")("buy_stock", {})

    assert seen == []


@pytest.mark.parametrize("name,bare", [
    ("mcp__lazy-tool-service__get_sec_filings", "get_sec_filings"),
    ("get_sec_filings", "get_sec_filings"),
    ("", ""),
])
def test_bare_tool_name(name, bare):
    assert bare_tool_name(name) == bare


# ── The allow-list must stay honest ──────────────────────────────────────


_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tool_schemas.json",
)


def _schemas():
    """tool_schemas.json is gitignored and absent from fresh worktrees.

    Skipping rather than failing is deliberate — a missing schema file is a
    known worktree trap, not a code defect — but the skip says which it is.
    """
    if not os.path.exists(_SCHEMA_PATH):
        pytest.skip(f"tool_schemas.json not present at {_SCHEMA_PATH}")
    with open(_SCHEMA_PATH) as fh:
        raw = json.load(fh)
    out = {}
    for item in raw:
        fn = item.get("function", item)
        name = fn.get("name")
        if name:
            out[name] = (fn.get("parameters") or {})
    return out


def test_every_allow_listed_tool_exists_and_requires_a_ticker():
    """Guards a typo, and a tool whose schema stopped requiring `ticker`.

    An entry that no longer matches a real tool is dead configuration that
    reads as coverage.
    """
    schemas = _schemas()
    missing = sorted(t for t in REPAIRABLE_TICKER_TOOLS if t not in schemas)
    assert not missing, f"allow-listed tools absent from tool_schemas.json: {missing}"

    not_required = sorted(
        t for t in REPAIRABLE_TICKER_TOOLS
        if "ticker" not in (schemas[t].get("required") or [])
    )
    assert not not_required, f"allow-listed but ticker not required: {not_required}"


def test_no_state_changing_tool_is_allow_listed():
    """The safety boundary, asserted rather than trusted to review."""
    forbidden = {
        "buy_stock", "sell_stock",
        "add_to_watchlist", "remove_from_watchlist", "watch_ticker",
        "escalate_to_pm", "request_peer_analysis",
        "save_trading_chart", "run_equation", "run_backtest",
    }
    assert not (REPAIRABLE_TICKER_TOOLS & forbidden)


def test_the_allow_list_is_a_strict_subset_of_ticker_requiring_tools():
    """Nothing is repaired that the schema would not have rejected anyway."""
    schemas = _schemas()
    requires_ticker = {
        n for n, p in schemas.items() if "ticker" in (p.get("required") or [])
    }
    assert REPAIRABLE_TICKER_TOOLS < requires_ticker


# ── Against the real validator, not a model of it ─────────────────────────


def _sdk_verdict(reg, tool: str, args: dict) -> tuple[bool, list[str]]:
    """Replay the SDK's own accept/reject sequence (lazycat/tool_registry.py).

    Mirrors the execute path: lowercase keys, drop undeclared keys via the
    registry's own `_filter_kwargs_to_schema`, then check what the registry's
    own `_schema_params` says is required. Returns (accepted, missing).
    """
    kwargs = {k.lower(): v for k, v in args.items()}
    kwargs, _dropped = reg._filter_kwargs_to_schema(tool, kwargs)
    _props, required = reg._schema_params(tool)
    missing = sorted(required - set(kwargs))
    return (not missing), missing


def test_the_repair_actually_satisfies_the_real_validator():
    """The claim under test is "this call would now succeed" — so ask the code
    that rejects it, not a re-implementation of that code.

    An earlier version of this check hand-rolled the schema lookup, silently
    failed to populate `registry.schemas`, and reported ACCEPTED for a payload
    production had rejected — a green result from a harness that was not
    measuring anything. Loading via the registry's OWN `load_from_json` is what
    makes this an oracle rather than a second opinion.

    Both production failure paths are represented:
      · schema path   — junk keys dropped, leaving `ticker` unset
      · TypeError path — nothing dropped, so the validator never ran
    """
    if not os.path.exists(_SCHEMA_PATH):
        pytest.skip("tool_schemas.json not present")

    from lazycat.tool_registry import ToolRegistry

    reg = ToolRegistry()
    reg.load_from_json(_SCHEMA_PATH)
    assert reg.schemas, "registry loaded no schemas — the oracle is blind"
    # Guard the oracle itself: if this lookup breaks, every verdict below
    # becomes a vacuous ACCEPTED.
    assert reg._schema_params("get_sec_filings")[1] == {"ticker"}

    cases = [
        ("get_sec_filings", {"action": "facts", "form_type": "10-K"}, "HCA", True),
        ("whiteboard_write", {"author": "v3_junior_analyst",
                              "section": "market_context",
                              "content": '{"market_context":{"ticker":"SOFI"}}'},
         "SOFI", True),
        # The safety boundary, measured the same way: still rejected after.
        ("buy_stock", {"size_pct": 0.1}, "HCA", False),
    ]
    for tool, args, ticker, should_repair in cases:
        accepted_before, missing = _sdk_verdict(reg, tool, args)
        assert not accepted_before, f"{tool} was already valid — fixture is stale"
        assert missing == ["ticker"], f"{tool}: unexpected missing {missing}"

        fixed = dict(args)
        repair_tool_arguments(tool, fixed, ticker=ticker, record=False)
        accepted_after, _ = _sdk_verdict(reg, tool, fixed)

        assert accepted_after is should_repair, (
            f"{tool}: expected accepted_after={should_repair}, got {accepted_after}"
        )
