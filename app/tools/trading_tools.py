import json
import logging
from app.tools.registry import registry, PermissionLevel
from app.trading.paper_trader import buy, sell
from app.trading.watchlist import add_ticker, remove_ticker
from app.tools.portfolio_tools import resolve_bot_id
from app.db import mongo_query

logger = logging.getLogger(__name__)


@registry.register(
    name="buy_stock",
    description="Execute a buy order for a stock ticker. Requires user confirmation.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker symbol (e.g., AAPL).",
            },
            "size_pct": {
                "type": "number",
                "description": "The percentage of available cash to use (e.g., 0.10 for 10%, 1.0 for 100%). Default is 0.10.",
            },
        },
        "required": ["ticker"],
    },
    tier=1,
    source="paper_trader",
    permission=PermissionLevel.WRITE,  # Paper trading — nothing is irreversible
)
async def buy_stock(ticker: str, size_pct: float = 0.10) -> str:
    """Execute a paper buy order."""
    logger.info(
        "[TradingTools] Executing buy order for %s (size: %.2f)", ticker, size_pct
    )
    # Ensure uppercase
    ticker = ticker.upper().strip()

    # THE single resolver (2026-07-25 audit). This inlined get_active_bot_id()
    # with an `except: settings.BOT_ID` fallback — behaviourally identical, but a
    # fourth copy of the resolution rule in a WRITE tool that places real orders.
    # A future change to bot resolution must not have to find this one.
    bot_id = resolve_bot_id()

    try:
        result = await buy(bot_id, ticker, size_pct)
        if "error" in result:
            return json.dumps({"status": "error", "message": result["error"]})
        return json.dumps({"status": "success", "trade": result})
    except Exception as e:
        logger.error("[TradingTools] Buy failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="sell_stock",
    description="Execute a sell order to close a position for a stock ticker. Requires user confirmation.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker symbol to sell (e.g., AAPL).",
            }
        },
        "required": ["ticker"],
    },
    tier=1,
    source="paper_trader",
    permission=PermissionLevel.WRITE,  # Paper trading — nothing is irreversible
)
async def sell_stock(ticker: str) -> str:
    """Execute a paper sell order (closes the entire position)."""
    logger.info("[TradingTools] Executing sell order for %s", ticker)
    ticker = ticker.upper().strip()
    # THE single resolver — see buy_stock above.
    bot_id = resolve_bot_id()

    try:
        result = await sell(bot_id, ticker)
        if "error" in result:
            return json.dumps({"status": "error", "message": result["error"]})
        return json.dumps({"status": "success", "trade": result})
    except Exception as e:
        logger.error("[TradingTools] Sell failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="add_to_watchlist",
    description="Add a stock ticker (e.g., from the Discovery list) to the user's active watchlist. Do NOT use this tool to add items TO discovery; discovery is automatic. Requires user confirmation.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker symbol to add (e.g., AAPL).",
            }
        },
        "required": ["ticker"],
    },
    tier=1,
    source="watchlist",
    permission=PermissionLevel.WRITE,  # Watchlist edits are reversible
)
def add_to_watchlist(ticker: str) -> str:
    """Add a ticker to watchlist."""
    logger.info("[TradingTools] Executing add to watchlist for %s", ticker)
    ticker = ticker.upper().strip()
    try:
        is_new = add_ticker(ticker, source="chat", notes="Added via AI Strategy Chat")
        msg = f"Added {ticker}" if is_new else f"Reactivated {ticker}"
        return json.dumps({"status": "success", "message": msg, "is_new": is_new})
    except Exception as e:
        logger.error("[TradingTools] Add to watchlist failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="remove_from_watchlist",
    description="Remove a stock ticker from the user's active watchlist. Requires user confirmation.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker symbol to remove (e.g., AAPL).",
            }
        },
        "required": ["ticker"],
    },
    tier=1,
    source="watchlist",
    permission=PermissionLevel.WRITE,  # Watchlist edits are reversible
)
def remove_from_watchlist(ticker: str) -> str:
    """Remove a ticker from watchlist."""
    logger.info("[TradingTools] Executing remove from watchlist for %s", ticker)
    ticker = ticker.upper().strip()
    try:
        removed = remove_ticker(ticker)
        if not removed:
            return json.dumps(
                {"status": "error", "message": f"{ticker} not in active watchlist"}
            )
        return json.dumps(
            {"status": "success", "message": f"Removed {ticker} from watchlist"}
        )
    except Exception as e:
        logger.error("[TradingTools] Remove from watchlist failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="get_sec_filings",
    description=(
        "Refresh AND read SEC institutional (13F) holdings for a ticker — which "
        "top funds hold it and how positions changed. Same data as "
        "get_institutional_holdings, but this one re-collects from the source "
        "first; prefer get_institutional_holdings when freshness doesn't matter."
    ),
    parameters={
        # `symbol` is declared so it SURVIVES schema filtering, and `ticker` is
        # no longer strictly required (2026-07-29). 14 of 47 calls over 7 days —
        # 30% — failed before reaching this function with
        #     "Malformed arguments: missing ['ticker']"
        # The SDK lowercases keys, drops every undeclared one, and only THEN
        # checks required (lazycat/tool_registry.py:534-551). Agents carrying
        # the old EDGAR-style schema send action/cik/limit/symbol, all of which
        # were dropped as undeclared — leaving `ticker` unset and the call
        # rejected before `**_extra` could ever swallow anything.
        #
        # SEC filings are the highest-value primary source in the toolset, so a
        # 30% rejection rate on a key-name mismatch is expensive. Declaring the
        # alias and resolving the subject in-function fixes it without loosening
        # anything: the ticker still has to come from somewhere real.
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Ticker symbol, e.g. AAPL."},
            "symbol": {"type": "string", "description": "Alias for ticker."},
        },
        "required": [],
    },
    tier=0,
    source="sec",
)
async def get_sec_filings_tool(ticker: str = "", **_extra) -> str:
    # **_extra swallows stray args (action/cik/limit/...) sent by agents that
    # cached the old EDGAR-style schema from lazy-tool — they used to raise
    # TypeError and fail every first call.
    from app.collectors.sec_collector import collect_ticker_institutional
    from app.tools.finance_tools import get_institutional_holdings
    from app.tools.tool_context import current_ticker

    # Resolution order: explicit arg, declared alias, then the ticker the
    # pipeline is actually analysing. The context fallback is safe because this
    # runs inside a per-ticker analysis — but it is LAST, so an agent asking
    # about a different company is always honoured over it.
    ticker = (ticker or _extra.get("symbol") or current_ticker() or "").strip().upper()
    if not ticker:
        # Fail with the schema, not a stack trace: a bare error just gets
        # retried verbatim by the model.
        return json.dumps({
            "error": "no ticker supplied and none in context",
            "usage": 'get_sec_filings{"ticker": "AAPL"}',
        })

    try:
        collected = await collect_ticker_institutional(ticker)
    except Exception as e:
        return json.dumps({"error": str(e)})
    # Returning just {"holders_collected": N} left agents with a count and no
    # data — they'd re-call in a loop hunting for the actual holdings.
    try:
        report = await get_institutional_holdings(ticker)
        return f"(collected {collected} holder records)\n\n{report}"
    except Exception as e:
        return json.dumps({"status": "success", "holders_collected": collected,
                           "note": f"collected but read-back failed: {e}"})


@registry.register(
    name="get_options_flow",
    description="Fetch unusual options activity and flow for a ticker.",
    parameters={
        "type": "object",
        "properties": {"ticker": {"type": "string"}},
        "required": ["ticker"],
    },
    tier=0,
    source="options",
)
async def get_options_flow_tool(ticker: str) -> str:
    from app.collectors.options_collector import collect_options

    try:
        res = await collect_options(ticker)
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"error": str(e)})


@registry.register(
    name="get_insider_trades",
    description="Fetch recent insider trading activity for a ticker.",
    parameters={
        "type": "object",
        "properties": {"ticker": {"type": "string"}},
        "required": ["ticker"],
    },
    tier=0,
    source="insider",
)
async def get_insider_trades_tool(ticker: str) -> str:
    from app.collectors.insider_collector import collect_insider

    try:
        res = await collect_insider(ticker)
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"error": str(e)})


@registry.register(
    name="get_congress_trades",
    description="Fetch recent congressional trading activity for a ticker.",
    parameters={
        "type": "object",
        "properties": {"ticker": {"type": "string"}},
        "required": ["ticker"],
    },
    tier=0,
    source="congress",
)
async def get_congress_trades_tool(ticker: str) -> str:
    """Refresh congressional disclosures for a ticker, then return the trades.

    This previously returned only {"trades_collected": N} — an agent asking what
    Congress bought got a count and no data, which is unusable as research.
    """
    from app.collectors.congress_collector import collect_trades_for_ticker
    from app.db.connection import get_db

    ticker = (ticker or "").upper().strip()
    if not ticker:
        return json.dumps({"error": "no ticker provided"})

    collected = 0
    try:
        # Best-effort refresh; a scrape failure should not block reading what we
        # already have on record.
        collected = await collect_trades_for_ticker(ticker)
    except Exception as e:
        logger.info("[congress] refresh failed for %s, serving stored rows: %s", ticker, e)

    try:
        rows = mongo_query.find_rows('congress_trades', {'ticker': ticker}, ['politician', 'party', 'chamber', 'state', 'transaction_type', 'amount_range', 'trade_date', 'disclosure_date', 'days_to_disclose'], sort=[('disclosure_date', -1)], limit=40)

        return json.dumps(
            {
                "status": "success",
                "ticker": ticker,
                "trades_collected": collected,
                "trade_count": len(rows),
                "trades": [
                    {
                        "politician": r[0],
                        "party": r[1],
                        "chamber": r[2],
                        "state": r[3],
                        "transaction_type": r[4],
                        "amount_range": r[5],
                        "trade_date": str(r[6]) if r[6] else None,
                        "disclosure_date": str(r[7]) if r[7] else None,
                        "days_to_disclose": r[8],
                    }
                    for r in rows
                ],
            },
            default=str,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@registry.register(
    name="get_earnings_data",
    description="Fetch upcoming or recent earnings dates and estimates for a ticker.",
    parameters={
        "type": "object",
        "properties": {"ticker": {"type": "string"}},
        "required": ["ticker"],
    },
    tier=0,
    source="earnings",
)
async def get_earnings_data_tool(ticker: str) -> str:
    from app.collectors.earnings_collector import collect_earnings

    try:
        res = await collect_earnings(ticker)
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"error": str(e)})




@registry.register(
    name="get_finviz_fundamentals",
    description="Fetch fundamental data from Finviz.",
    parameters={
        "type": "object",
        "properties": {"ticker": {"type": "string"}},
        "required": ["ticker"],
    },
    tier=0,
    source="finviz",
)
async def get_finviz_fundamentals_tool(ticker: str) -> str:
    from app.collectors.finviz_scraper import collect_fundamentals
    from app.db import mongo_store
    import json

    try:
        try:
            await collect_fundamentals(ticker)
        except Exception as ce:
            logger.info("[TradingTools] Live finviz scrape skipped (%s), reading stored fundamentals from MongoDB", ce)

        docs = mongo_store.find_docs("fundamentals", {"ticker": ticker}, sort=[("snapshot_date", -1)], limit=1)
        if docs:
            doc = docs[0]
            doc.pop("_id", None)
            return json.dumps(doc, default=str)
        return json.dumps({"error": "Failed to collect fundamentals from finviz"})
    except Exception as e:
        logger.error("[TradingTools] Finviz fundamentals failed for %s: %s", ticker, e)
        return json.dumps({"error": str(e)})


@registry.register(
    name="get_polygon_price_history",
    description="Fetch historical OHLCV price data from Polygon.",
    parameters={
        "type": "object",
        "properties": {"ticker": {"type": "string"}},
        "required": ["ticker"],
    },
    tier=0,
    source="polygon",
)
async def get_polygon_price_history_tool(ticker: str) -> str:
    from app.collectors.polygon_collector import collect_all

    try:
        res = await collect_all(ticker)
        return json.dumps(res)
    except Exception as e:
        return json.dumps({"error": str(e)})
