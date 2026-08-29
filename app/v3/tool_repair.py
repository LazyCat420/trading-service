"""Pre-flight repair of a tool call, before it burns a turn.

WHY THIS EXISTS
---------------
Every tool defect found in the 2026-07-29 harness audit was caught *after* the
fact, from telemetry, days later. The 2026-07-30 measurement says the same
failure is still running:

    2026-07-28   8 calls rejected for a missing required `ticker`
    2026-07-29   6
    2026-07-30   4

`get_sec_filings` sits at **26.3% failure over 14 days (n=57)**, and every one
of those failures is the same shape:

    args = {"author": "v3_junior_analyst", "content": "{\\"market_context\\": ..."}
                                                   ^ no ticker anywhere

The model emitted un-escaped JSON, so the real keys never survived parsing.
`lazycat/tool_registry.py` then drops every undeclared key
(`_filter_kwargs_to_schema`) and — only if that leaves a REQUIRED field unset —
rejects the call. So the rejection is never about the junk keys; it is about the
one field that got lost with them. Two error strings, one cause:

    "Malformed tool arguments. Required field(s) ['ticker'] were missing"   (schema path)
    "whiteboard_write() missing 1 required positional argument: 'ticker'"   (TypeError path,
        reached when nothing was dropped, so the validator never ran)

And the ticker was never actually unknown: the pipeline is running that ticker's
desk. The call failed for want of a value sitting in the caller's own scope.

WHAT THIS DOES NOT DO
---------------------
**It never blocks.** The hook returns None on every path. A pre-hook that can
refuse a call is a new failure mode, and the SDK does not wrap `on_tool_call` in
a try/except the way it wraps `on_tool_result` — an exception here would kill the
agent's turn. So every entry point below swallows its own errors.

**It never overwrites a value the model supplied.** A research agent comparing a
peer legitimately asks about a ticker that is not its own; silently rewriting
that to the desk ticker would turn good research into a wrong answer that looks
right. Absent only.

**It is fail-CLOSED.** Injection is limited to the allow-list below, not "any
tool whose schema requires a ticker". 29 tools require one, and they include
`buy_stock`, `sell_stock`, `add_to_watchlist` and `watch_ticker`. Completing a
malformed ORDER with a guessed ticker is not a repair, it is an invented trade —
a malformed order must fail. A new tool must be added here deliberately, having
been reasoned about, rather than inheriting repair by accident.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Tools where supplying the desk's own ticker for a missing required `ticker`
#: is unambiguously what the caller meant, and where being wrong costs a wasted
#: read rather than a position, an order, or a watchlist entry.
#:
#: Read-only research, plus the whiteboard: the whiteboard is the desk's own
#: scratchpad for exactly this ticker, and `whiteboard_write` is one of the two
#: tools measured failing this way.
#:
#: DELIBERATELY EXCLUDED — every one of these also requires `ticker`:
#:   buy_stock, sell_stock            place orders
#:   add_to_watchlist,
#:   remove_from_watchlist,
#:   watch_ticker                     mutate persistent watch state
#:   escalate_to_pm                   a workflow action, not a lookup
#:   save_trading_chart               persists an artifact
#:   run_equation, run_backtest       compute that persists results
REPAIRABLE_TICKER_TOOLS = frozenset({
    "forecast_volatility_garch",
    "get_congress_trades",
    "get_earnings_data",
    "get_finnhub_news",
    "get_finviz_fundamentals",
    "get_insider_trades",
    "get_institutional_holdings",
    "get_market_data",
    "get_options_flow",
    "get_polygon_price_history",
    "get_position_pnl",
    "get_sec_filings",
    "get_smart_money_signal",
    "get_technical_indicators",
    "get_ticker_summary",
    "get_upcoming_events",
    "whiteboard_read",
    "whiteboard_summarize",
    "whiteboard_write",
})

KIND_ARGS_REPAIRED = "TOOL_ARGS_REPAIRED_PRE_FLIGHT"


def bare_tool_name(tool_name: str) -> str:
    """`mcp__lazy-tool-service__get_sec_filings` -> `get_sec_filings`.

    Live calls arrive namespaced by the MCP server; `tool_schemas.json` and the
    allow-list above use bare names. Matching on the last segment keeps the
    allow-list readable and independent of which server routes the tool.
    """
    return (tool_name or "").rsplit("__", 1)[-1].strip()


def repair_tool_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    ticker: str = "",
    agent_name: str = "",
    cycle_id: str = "",
    record: bool = True,
) -> list[str]:
    """Repair `arguments` IN PLACE. Returns the names of the fields injected.

    In-place is the whole mechanism: the SDK hands this dict to the hook and
    then passes the same object to `execute_tool`, so a mutation here is what
    the tool actually receives. Returning the repair list (rather than mutating
    silently) is what makes the hook testable and what lets the caller record
    that it fired.
    """
    if not isinstance(arguments, dict):
        return []

    bare = bare_tool_name(tool_name)
    if bare not in REPAIRABLE_TICKER_TOOLS:
        return []

    ticker = (ticker or "").strip().upper()
    if not ticker:
        return []

    # Absent only — never correct a ticker the model chose on purpose.
    existing = arguments.get("ticker")
    if isinstance(existing, str) and existing.strip():
        return []
    if existing is not None and not isinstance(existing, str):
        return []

    arguments["ticker"] = ticker
    repaired = ["ticker"]

    logger.warning(
        "[ToolRepair] %s called %s with no ticker; injected %s "
        "(malformed model JSON — see app/v3/tool_repair.py)",
        agent_name or "?", bare, ticker,
    )
    if record:
        _record(bare, repaired, ticker=ticker, agent_name=agent_name,
                cycle_id=cycle_id, arg_keys=sorted(arguments)[:12])
    return repaired


def _record(bare: str, repaired: list[str], **detail: Any) -> None:
    """Log the repair where the invariant violations live.

    A repaired call is still a defect — the model produced arguments that did
    not match the schema — and the repair makes it invisible in the tool
    failure telemetry it used to show up in. Recording it keeps the upstream
    bug (bad JSON from a specific agent) measurable instead of merely
    survivable, and gives the hook a queryable proof that it FIRES:

        SELECT detail->>'tool', COUNT(*) FROM v3_invariant_violations
        WHERE kind = 'TOOL_ARGS_REPAIRED_PRE_FLIGHT' GROUP BY 1;
    """
    try:
        from app.v3.invariants import record_violation

        record_violation(
            KIND_ARGS_REPAIRED,
            ticker=str(detail.get("ticker") or ""),
            cycle_id=str(detail.get("cycle_id") or ""),
            tool=bare,
            fields=repaired,
            agent=detail.get("agent_name") or "",
            arg_keys=detail.get("arg_keys") or [],
        )
    except Exception as e:  # noqa: BLE001 — recording must never break a turn
        logger.debug("[ToolRepair] could not record repair (non-fatal): %s", e)


def make_pre_tool_hook(*, ticker: str = "", agent_name: str = "",
                       cycle_id: str = ""):
    """Build the `on_tool_call` hook the SDK's AgentHarness expects.

    Signature is `(tool_name, arguments) -> str | None`, where a non-None return
    BLOCKS the call and becomes its result. This hook returns None on every
    path, including on failure: it repairs, it never refuses.

    The SDK calls `on_tool_call` unguarded (lazycat/agent.py:320), unlike
    `on_tool_result` which it wraps — so the try/except here is load-bearing,
    not defensive habit. A raise would abort the agent's turn.
    """
    def _hook(tool_name: str, arguments: dict) -> None:
        try:
            repair_tool_arguments(
                tool_name, arguments,
                ticker=ticker, agent_name=agent_name, cycle_id=cycle_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("[ToolRepair] hook failed (non-fatal): %s", e)
        return None

    return _hook
