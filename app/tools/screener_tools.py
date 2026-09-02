"""Agent tool: query the market-map screener (finviz-parity snapshot)."""

import json
import logging

from pydantic import BaseModel, Field

from app.services.screener_client import screener_client
from app.tools.registry import registry, PermissionLevel

logger = logging.getLogger(__name__)

# Compact default projection — keeps a 15-row result around 1.5KB.
_DEFAULT_COLUMNS = [
    "ticker", "name", "sector", "market_cap", "price", "change_pct",
    "pe_ratio", "rsi_14", "perf_month_pct",
]

_MAX_LIMIT = 50
# Self-cap: the registry's 50k truncation slices JSON mid-object; we shrink
# by whole rows instead so the payload is always valid JSON.
_MAX_RESULT_CHARS = 12_000


class ScreenerQueryInput(BaseModel):
    filters: list[str] = Field(
        default_factory=list,
        description="Conditions as 'field:op:value', ops lt/lte/gt/gte/eq/ne/in "
                    "(in uses |-separated values). Example: ['rsi_14:lt:30', "
                    "'market_cap:gt:10000000000', 'sector:eq:Energy']",
    )
    sort: str | None = Field(default=None, description="Field to sort by")
    dir: str = Field(default="desc", description="Sort direction: asc or desc")
    limit: int = Field(default=15, ge=1, le=_MAX_LIMIT,
                       description="Max rows to return (1-50)")
    columns: list[str] = Field(
        default_factory=list,
        description="Fields to return per row (ticker always included). "
                    "Empty = compact default set. Request ONLY what you need.",
    )


def _round(v):
    if isinstance(v, float):
        return round(v, 4) if abs(v) < 1000 else round(v, 1)
    return v


@registry.register(
    name="screener_query",
    description=(
        "Query the live market screener: 1,000+ tickers x 119 up-to-date fields. "
        "Filter/sort/select across: descriptive (sector, industry, market_cap, "
        "market_cap_tier, sp500, asset_class, price, change_pct, volume, "
        "avg_volume, rel_volume), valuation (pe_ratio, forward_pe, peg_ratio, "
        "price_to_sales, price_to_book, price_to_fcf, ev_to_ebitda, ev_to_sales), "
        "growth (eps_growth_this_y, eps_growth_next_y, eps_growth_past_5y, "
        "eps_growth_qoq, sales_growth_qoq, revenue_growth), profitability "
        "(gross_margin, oper_margin, net_margin, roe, roa, roic, debt_to_equity, "
        "current_ratio, quick_ratio), dividends/analyst (dividend_yield, "
        "payout_ratio, target_price, target_upside_pct, recom_score [1=strong "
        "buy..5=strong sell], earnings_date), ownership/short (insider_own_pct, "
        "inst_own_pct, shares_float, short_float_pct, short_ratio), technicals "
        "(rsi_14, beta, atr_14, sma20/50/200_dist_pct, w52_high/low_dist_pct, "
        "perf_week/month/quarter/ytd/year_pct, volatility_month_pct, gap_pct), "
        "smart money (funds_holders, funds_new_positions, funds_net_activity, "
        "congress_buys_90d, congress_sells_90d, news_count_7d), ETF "
        "(etf_category, etf_aum, etf_expense_pct, etf_ret_3y_pct). "
        "Percent fields are already in % units. An invalid field name returns "
        "the full valid-field list. Keep limit small and request only the "
        "columns you need. Example: filters=['sector:eq:Energy','pe_ratio:lt:15'], "
        "sort='market_cap', columns=['name','pe_ratio','dividend_yield']."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filters": {
                "type": "array", "items": {"type": "string"},
                "description": "Conditions 'field:op:value' (lt/lte/gt/gte/eq/ne/in)",
            },
            "sort": {"type": "string", "description": "Field to sort by"},
            "dir": {"type": "string", "enum": ["asc", "desc"],
                    "description": "Sort direction (default desc)"},
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT,
                      "description": "Max rows (default 15)"},
            "columns": {
                "type": "array", "items": {"type": "string"},
                "description": "Fields per row; empty = compact default set",
            },
        },
        "required": [],
    },
    tier=0,
    source="trading-client screener",
    permission=PermissionLevel.READ_ONLY,
    max_result_chars=20_000,
    input_model=ScreenerQueryInput,
    tags=["market", "screener"],
    domain="market-data",
)
async def screener_query(
    filters: list[str] | None = None,
    sort: str | None = None,
    dir: str = "desc",
    limit: int = 15,
    columns: list[str] | None = None,
) -> dict:
    limit = max(1, min(int(limit or 15), _MAX_LIMIT))
    cols = list(columns) if columns else list(_DEFAULT_COLUMNS)
    payload = await screener_client.query(
        filters=filters or [], sort=sort, direction=dir,
        limit=limit, columns=cols,
    )
    if payload is None:
        # Distinguish outage from empty — never report a dead backend as
        # "no stocks matched".
        return {
            "error": "screener backend unreachable",
            "detail": screener_client.last_error,
        }
    if "error" in payload:
        return payload  # 400 detail: lists valid fields for self-correction

    rows = [{k: _round(v) for k, v in r.items()} for r in payload.get("rows", [])]
    result = {
        "total_matches": payload.get("total"),
        "returned": len(rows),
        "rows": rows,
    }
    # Shrink by whole rows (never let the registry's mid-string truncation
    # produce broken JSON).
    while rows and len(json.dumps(result)) > _MAX_RESULT_CHARS:
        rows.pop()
        result["returned"] = len(rows)
        result["note"] = ("result trimmed to fit size budget — request fewer "
                          "columns or a smaller limit")
    total_matches = result.get("total_matches") or 0
    if total_matches > result["returned"]:
        result.setdefault(
            "note",
            f"{result['total_matches']} tickers match but only "
            f"{result['returned']} returned — tighten filters or sort to get "
            "the most relevant rows first",
        )
    return result
