"""
Fund Scanner — Discovery-mode analysis of institutional 13F holdings.

Scans ALL 13F holdings in the DB and produces:
  1. Fund portfolio snapshots — top holdings per fund
  2. Cross-fund consensus — tickers held by multiple top funds
  3. Quarterly changes — new positions, exits, size changes
  4. Watchlist comparison — overlap between fund holdings and our tickers
  5. Discovery — tickers funds hold that we're NOT watching

Data source: sec_13f_holdings and sec_13f_filers collections in MongoDB
"""

import logging
from app.db import mongo_store, mongo_query

logger = logging.getLogger(__name__)

TOP_PERFORMER_CIKS = {
    "0001037389",  # Renaissance Technologies
    "0001535392",  # Coatue Management
    "0001536411",  # Druckenmiller (Duquesne)
    "0001167483",  # Tiger Global Management
    "0001603466",  # Point72 Asset Management
    "0001103804",  # Viking Global Investors
    "0001061768",  # Baupost Group
    "0001040273",  # Third Point
    "0001079114",  # Greenlight Capital
    "0001336528",  # Pershing Square Capital
}


def _get_filer_map(ciks: list[str] | None = None) -> dict[str, str]:
    query = {"cik": {"$in": ciks}} if ciks else {}
    filers = mongo_store.find_docs("sec_13f_filers", query)
    return {f.get("cik"): f.get("filer_name") for f in filers if f.get("cik")}


def get_fund_portfolios(top_holdings: int = 20) -> list[dict]:
    """Get the top holdings for each fund in the latest filing quarter."""
    try:
        holdings = mongo_store.find_docs("sec_13f_holdings", {})
        if not holdings:
            return []

        cik_latest_q: dict[str, str] = {}
        for h in holdings:
            cik = h.get("cik")
            q = h.get("filing_quarter", "")
            if cik and (cik not in cik_latest_q or q > cik_latest_q[cik]):
                cik_latest_q[cik] = q

        filer_map = _get_filer_map(list(cik_latest_q.keys()))
        portfolios = []

        for cik, quarter in cik_latest_q.items():
            fund_holdings = [
                h for h in holdings
                if h.get("cik") == cik and h.get("filing_quarter") == quarter
            ]
            fund_holdings.sort(key=lambda x: x.get("value_usd") or 0, reverse=True)
            total_value = sum(h.get("value_usd") or 0 for h in fund_holdings)
            holding_count = len(fund_holdings)

            top = []
            for h in fund_holdings[:top_holdings]:
                val = h.get("value_usd") or 0
                pct = (val / total_value * 100) if total_value > 0 else 0
                top.append({
                    "ticker": h.get("ticker"),
                    "shares": h.get("shares") or 0,
                    "value_usd": val,
                    "pct_change": h.get("pct_change") or 0.0,
                    "is_new": bool(h.get("is_new_position")),
                    "is_exit": bool(h.get("is_exit")),
                    "pct_of_portfolio": round(pct, 2),
                })

            portfolios.append({
                "fund": filer_map.get(cik) or cik,
                "quarter": quarter,
                "total_value": total_value,
                "holding_count": holding_count,
                "top_holdings": top,
            })

        portfolios.sort(key=lambda p: p["fund"])
        return portfolios
    except Exception as e:
        logger.warning("[fund_scanner] get_fund_portfolios failed: %s", e)
        return []


def find_crossfund_consensus(min_funds: int = 3) -> list[dict]:
    """Find tickers held by multiple top funds — consensus = conviction."""
    try:
        holdings = mongo_store.find_docs("sec_13f_holdings", {})
        if not holdings:
            return []

        cik_latest_q: dict[str, str] = {}
        for h in holdings:
            cik = h.get("cik")
            q = h.get("filing_quarter", "")
            if cik and (cik not in cik_latest_q or q > cik_latest_q[cik]):
                cik_latest_q[cik] = q

        filer_map = _get_filer_map(list(cik_latest_q.keys()))

        ticker_map: dict[str, dict] = {}
        for h in holdings:
            cik = h.get("cik")
            ticker = h.get("ticker", "")
            if not ticker or ticker == "nan" or len(ticker) > 5 or (cik and str(cik).startswith("yf_")):
                continue
            if h.get("filing_quarter") != cik_latest_q.get(cik):
                continue

            if ticker not in ticker_map:
                ticker_map[ticker] = {
                    "ciks": set(),
                    "total_value": 0,
                    "total_shares": 0,
                }
            ticker_map[ticker]["ciks"].add(cik)
            ticker_map[ticker]["total_value"] += (h.get("value_usd") or 0)
            ticker_map[ticker]["total_shares"] += (h.get("shares") or 0)

        consensus = []
        for ticker, data in ticker_map.items():
            fund_count = len(data["ciks"])
            if fund_count >= min_funds:
                funds = [filer_map.get(c) or c for c in data["ciks"]]
                consensus.append({
                    "ticker": ticker,
                    "fund_count": fund_count,
                    "funds": funds,
                    "total_value": data["total_value"],
                    "total_shares": data["total_shares"],
                })

        consensus.sort(key=lambda x: (x["fund_count"], x["total_value"]), reverse=True)
        return consensus
    except Exception as e:
        logger.warning("[fund_scanner] find_crossfund_consensus failed: %s", e)
        return []


def detect_quarterly_changes() -> dict:
    """Detect new positions, exits, and significant size changes across funds."""
    try:
        holdings = mongo_store.find_docs("sec_13f_holdings", {})
        if not holdings:
            return {"new_positions": [], "exits": [], "size_changes": [], "note": "No filings yet"}

        fund_quarters: dict[str, set[str]] = {}
        for h in holdings:
            cik = h.get("cik")
            q = h.get("filing_quarter")
            if cik and q and not str(cik).startswith("yf_"):
                fund_quarters.setdefault(cik, set()).add(q)

        filer_map = _get_filer_map(list(fund_quarters.keys()))
        new_positions = []
        exits = []

        for cik, qs in fund_quarters.items():
            if len(qs) < 2:
                continue
            sorted_qs = sorted(list(qs), reverse=True)
            latest_q, prev_q = sorted_qs[0], sorted_qs[1]

            latest_holdings = {h.get("ticker"): h for h in holdings if h.get("cik") == cik and h.get("filing_quarter") == latest_q}
            prev_holdings = {h.get("ticker"): h for h in holdings if h.get("cik") == cik and h.get("filing_quarter") == prev_q}

            fund_name = filer_map.get(cik) or cik

            for t, h in latest_holdings.items():
                if t and t != "nan" and t not in prev_holdings:
                    new_positions.append({
                        "fund": fund_name, "ticker": t, "shares": h.get("shares") or 0,
                        "value": h.get("value_usd") or 0, "quarter": latest_q,
                    })

            for t, h in prev_holdings.items():
                if t and t != "nan" and t not in latest_holdings:
                    exits.append({
                        "fund": fund_name, "ticker": t, "shares": h.get("shares") or 0,
                        "value": h.get("value_usd") or 0, "quarter": prev_q,
                    })

        new_positions.sort(key=lambda x: x["value"], reverse=True)
        exits.sort(key=lambda x: x["value"], reverse=True)

        return {
            "new_positions": new_positions[:50],
            "exits": exits[:50],
            "new_position_count": len(new_positions),
            "exit_count": len(exits),
        }
    except Exception as e:
        logger.warning("[fund_scanner] detect_quarterly_changes failed: %s", e)
        return {"new_positions": [], "exits": [], "size_changes": []}


def compare_with_watchlist(watchlist_tickers: list[str]) -> dict:
    """Compare fund holdings against our watchlist."""
    try:
        holdings = mongo_store.find_docs("sec_13f_holdings", {})
        cik_latest_q: dict[str, str] = {}
        for h in holdings:
            cik = h.get("cik")
            q = h.get("filing_quarter", "")
            if cik and (cik not in cik_latest_q or q > cik_latest_q[cik]):
                cik_latest_q[cik] = q

        filer_map = _get_filer_map(list(cik_latest_q.keys()))

        fund_tickers: dict[str, list[dict]] = {}
        for h in holdings:
            cik = h.get("cik")
            ticker = h.get("ticker", "")
            if not ticker or ticker == "nan" or len(ticker) > 5 or (cik and str(cik).startswith("yf_")):
                continue
            if h.get("filing_quarter") != cik_latest_q.get(cik):
                continue
            fund_tickers.setdefault(ticker.upper(), []).append({
                "fund": filer_map.get(cik) or cik,
                "shares": h.get("shares") or 0,
                "value": h.get("value_usd") or 0,
            })

        fund_set = set(fund_tickers.keys())
        watchlist_set = {t.upper() for t in watchlist_tickers}

        overlap = fund_set & watchlist_set
        discovery = fund_set - watchlist_set
        not_held = watchlist_set - fund_set

        overlap_details = []
        for ticker in sorted(overlap):
            holders = fund_tickers.get(ticker, [])
            holders.sort(key=lambda x: x["value"], reverse=True)
            overlap_details.append({
                "ticker": ticker,
                "fund_count": len(holders),
                "total_value": sum(h["value"] for h in holders),
                "holders": holders[:5],
            })

        discovery_details = []
        for ticker in sorted(discovery):
            holders = fund_tickers.get(ticker, [])
            holders.sort(key=lambda x: x["value"], reverse=True)
            total_val = sum(h["value"] for h in holders)
            if total_val > 0:
                discovery_details.append({
                    "ticker": ticker,
                    "fund_count": len(holders),
                    "total_value": total_val,
                    "top_holder": holders[0]["fund"] if holders else "",
                })

        discovery_details.sort(key=lambda x: x["total_value"], reverse=True)

        return {
            "overlap": overlap_details,
            "overlap_count": len(overlap),
            "discovery": discovery_details[:30],
            "discovery_count": len(discovery),
            "not_held": sorted(not_held),
            "not_held_count": len(not_held),
            "fund_total_tickers": len(fund_set),
        }
    except Exception as e:
        logger.warning("[fund_scanner] compare_with_watchlist failed: %s", e)
        return {
            "overlap": [], "overlap_count": 0, "discovery": [],
            "discovery_count": 0, "not_held": [], "not_held_count": 0, "fund_total_tickers": 0,
        }


def generate_report(watchlist_tickers: list[str] | None = None) -> str:
    """Generate a human-readable institutional holdings report."""
    lines = []
    lines.append("=" * 70)
    lines.append("INSTITUTIONAL FUND SCANNER REPORT")
    lines.append("=" * 70)

    portfolios = get_fund_portfolios(top_holdings=10)
    lines.append(f"\n📊 Fund Portfolios ({len(portfolios)} funds tracked):")
    for p in portfolios:
        total_fmt = f"${p['total_value']:,.0f}" if p["total_value"] else "$0"
        lines.append(f"\n   {p['fund']} ({p['quarter']}) — {p['holding_count']} holdings, {total_fmt} total")
        for h in p["top_holdings"][:5]:
            val_fmt = f"${h['value_usd']:,.0f}" if h["value_usd"] else "$0"
            new_flag = " 🆕" if h["is_new"] else ""
            lines.append(f"      {h['ticker']}: {h['shares']:,} shares, {val_fmt} ({h['pct_of_portfolio']:.1f}%){new_flag}")

    consensus = find_crossfund_consensus(min_funds=2)
    if consensus:
        lines.append(f"\n🎯 Cross-Fund Consensus ({len(consensus)} tickers held by 2+ funds):")
        for c in consensus[:15]:
            val_fmt = f"${c['total_value']:,.0f}" if c["total_value"] else "$0"
            lines.append(f"   {c['ticker']}: {c['fund_count']} funds ({val_fmt}) — {', '.join(c['funds'][:3])}")

    changes = detect_quarterly_changes()
    if changes.get("new_positions"):
        lines.append(f"\n🆕 New Positions ({changes['new_position_count']}):")
        for np in changes["new_positions"][:10]:
            val_fmt = f"${np['value']:,.0f}" if np["value"] else "$0"
            lines.append(f"   {np['fund']} → {np['ticker']} ({val_fmt})")
    if changes.get("exits"):
        lines.append(f"\n🚪 Exits ({changes['exit_count']}):")
        for ex in changes["exits"][:10]:
            val_fmt = f"${ex['value']:,.0f}" if ex["value"] else "$0"
            lines.append(f"   {ex['fund']} ← {ex['ticker']} ({val_fmt})")

    if watchlist_tickers:
        comp = compare_with_watchlist(watchlist_tickers)
        lines.append("\n🔍 Watchlist Comparison:")
        lines.append(f"   Funds hold {comp['fund_total_tickers']} unique tickers")
        lines.append(f"   Overlap with watchlist: {comp['overlap_count']}")
        lines.append(f"   Discovery (funds hold, not on watchlist): {comp['discovery_count']}")
        lines.append(f"   Not held by any fund: {comp['not_held_count']}")

        if comp["overlap"]:
            lines.append("\n   📌 Overlap:")
            for o in comp["overlap"]:
                val_fmt = f"${o['total_value']:,.0f}" if o["total_value"] else "$0"
                lines.append(f"      {o['ticker']}: {o['fund_count']} funds, {val_fmt}")
                for h in o["holders"][:3]:
                    lines.append(f"         {h['fund']}: {h['shares']:,} shares")

        if comp["discovery"]:
            lines.append("\n   🆕 Discovery (funds hold, you're not watching):")
            for d in comp["discovery"][:15]:
                val_fmt = f"${d['total_value']:,.0f}" if d["total_value"] else "$0"
                lines.append(f"      {d['ticker']}: {d['fund_count']} funds, {val_fmt} (top: {d['top_holder']})")

    lines.append(f"\n{'=' * 70}")
    return "\n".join(lines)


def get_institutional_signal(ticker: str) -> dict:
    """Get institutional positioning signal for a single ticker in pure MongoDB."""
    ticker = ticker.upper().strip()
    try:
        holdings = mongo_store.find_docs("sec_13f_holdings", {"ticker": ticker})
        if not holdings:
            return {
                "fund_count": 0,
                "holders": [],
                "total_institutional_value": 0,
                "has_new_position": False,
                "has_top_performer": False,
                "top_performer_names": [],
                "momentum": "UNKNOWN",
            }

        ciks = list({h.get("cik") for h in holdings if h.get("cik")})
        filer_map = _get_filer_map(ciks)

        cik_latest_q = {}
        for h in holdings:
            cik = h.get("cik")
            q = h.get("filing_quarter", "")
            if cik and (cik not in cik_latest_q or q > cik_latest_q[cik]):
                cik_latest_q[cik] = q

        latest_holdings = [h for h in holdings if h.get("filing_quarter") == cik_latest_q.get(h.get("cik"))]
        latest_holdings.sort(key=lambda h: h.get("value_usd") or 0, reverse=True)

        holders = []
        total_value = 0
        has_new = False
        top_perf_names = []

        for h in latest_holdings:
            cik = h.get("cik")
            fund_name = filer_map.get(cik) or cik or "Unknown Fund"
            val = h.get("value_usd") or 0
            is_new = bool(h.get("is_new_position"))
            pct_change = float(h.get("pct_change") or 0.0)
            total_value += val
            if is_new:
                has_new = True
            if cik in TOP_PERFORMER_CIKS:
                top_perf_names.append(fund_name)
            holders.append({
                "fund": fund_name,
                "shares": h.get("shares") or 0,
                "value_usd": val,
                "is_new": is_new,
                "pct_change": pct_change,
            })

        changes = [h["pct_change"] for h in holders if h["pct_change"] != 0.0]
        if not changes:
            momentum = "FLAT"
        else:
            avg_change = sum(changes) / len(changes)
            if avg_change > 5.0:
                momentum = "INCREASING"
            elif avg_change < -5.0:
                momentum = "DECREASING"
            else:
                momentum = "FLAT"

        return {
            "fund_count": len(holders),
            "holders": holders[:10],
            "total_institutional_value": total_value,
            "has_new_position": has_new,
            "has_top_performer": len(top_perf_names) > 0,
            "top_performer_names": top_perf_names,
            "momentum": momentum,
        }
    except Exception as e:
        logger.warning("[fund_scanner] get_institutional_signal failed for %s: %s", ticker, e)
        return {
            "fund_count": 0,
            "holders": [],
            "total_institutional_value": 0,
            "has_new_position": False,
            "has_top_performer": False,
            "top_performer_names": [],
            "momentum": "UNKNOWN",
        }


def get_top_conviction_tickers(min_funds: int = 2, max_results: int = 30) -> list[dict]:
    """Return tickers ranked by institutional conviction score."""
    try:
        pipeline = [
            {"$match": {"ticker": {"$nin": ["nan", "", None]}}},
            {
                "$group": {
                    "_id": "$ticker",
                    "cik_list": {"$addToSet": "$cik"},
                    "total_value": {"$sum": "$value_usd"},
                    "any_new": {"$max": "$is_new_position"},
                }
            },
            {"$project": {
                "ticker": "$_id",
                "fund_count": {"$size": "$cik_list"},
                "cik_list": 1,
                "total_value": 1,
                "any_new": 1,
            }},
            {"$match": {"fund_count": {"$gte": min_funds}}},
            {"$sort": {"fund_count": -1, "total_value": -1}},
            {"$limit": max_results},
        ]
        docs = mongo_store.aggregate("sec_13f_holdings", pipeline)
        results = []
        for d in docs:
            ticker = d.get("ticker") or d.get("_id")
            fund_count = d.get("fund_count", 0)
            ciks = set(d.get("cik_list") or [])
            top_perf_count = len(ciks & TOP_PERFORMER_CIKS)

            score = (fund_count * 10) + (top_perf_count * 15)
            if d.get("any_new"):
                score += 10

            results.append({
                "ticker": ticker,
                "fund_count": fund_count,
                "fund_names": [],
                "total_value": d.get("total_value", 0),
                "has_new_position": bool(d.get("any_new")),
                "top_performer_count": top_perf_count,
                "conviction_score": score,
            })

        results.sort(key=lambda x: x["conviction_score"], reverse=True)
        return results[:max_results]
    except Exception as e:
        logger.warning("[fund_scanner] get_top_conviction_tickers failed: %s", e)
        return []


def get_fund_momentum(ticker: str) -> dict:
    """Compare latest vs previous quarter holdings for a ticker."""
    ticker = ticker.upper().strip()
    try:
        holdings = mongo_store.find_docs("sec_13f_holdings", {"ticker": ticker})
        quarters = sorted(list({h.get("filing_quarter") for h in holdings if h.get("filing_quarter")}), reverse=True)
        if len(quarters) < 2:
            return {
                "direction": "NO_HISTORY",
                "new_buyers": [],
                "exiters": [],
                "net_share_change": 0,
                "net_value_change": 0,
                "latest_quarter": quarters[0] if quarters else None,
                "previous_quarter": None,
            }
        latest_q = quarters[0]
        prev_q = quarters[1]

        ciks = list({h.get("cik") for h in holdings if h.get("cik")})
        filer_map = _get_filer_map(ciks)

        latest_holdings = [h for h in holdings if h.get("filing_quarter") == latest_q]
        prev_holdings = [h for h in holdings if h.get("filing_quarter") == prev_q]

        latest_map = {
            filer_map.get(h.get("cik")) or h.get("cik"): {"shares": h.get("shares") or 0, "value": h.get("value_usd") or 0}
            for h in latest_holdings
        }
        prev_map = {
            filer_map.get(h.get("cik")) or h.get("cik"): {"shares": h.get("shares") or 0, "value": h.get("value_usd") or 0}
            for h in prev_holdings
        }

        new_buyers = [f for f in latest_map if f not in prev_map]
        exiters = [f for f in prev_map if f not in latest_map]

        net_shares = 0
        net_value = 0
        for fund in latest_map:
            lat = latest_map[fund]
            prv = prev_map.get(fund, {"shares": 0, "value": 0})
            net_shares += lat["shares"] - prv["shares"]
            net_value += lat["value"] - prv["value"]
        for fund in exiters:
            net_shares -= prev_map[fund]["shares"]
            net_value -= prev_map[fund]["value"]

        if net_shares > 0 or len(new_buyers) > len(exiters):
            direction = "INCREASING"
        elif net_shares < 0 or len(exiters) > len(new_buyers):
            direction = "DECREASING"
        else:
            direction = "FLAT"

        return {
            "direction": direction,
            "new_buyers": new_buyers,
            "exiters": exiters,
            "net_share_change": net_shares,
            "net_value_change": net_value,
            "latest_quarter": latest_q,
            "previous_quarter": prev_q,
        }
    except Exception as e:
        logger.warning("[fund_scanner] get_fund_momentum failed for %s: %s", ticker, e)
        return {
            "direction": "NO_HISTORY",
            "new_buyers": [],
            "exiters": [],
            "net_share_change": 0,
            "net_value_change": 0,
            "latest_quarter": None,
            "previous_quarter": None,
        }
