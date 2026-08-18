"""
Congress Scanner — Discovery-mode analysis of congressional stock trades.

Scans ALL congress trades in the DB (not just our watchlist tickers) and produces:
  1. Recent activity report — who bought/sold what this week/month
  2. Consensus signals — multiple congress members trading the same ticker
  3. Portfolio tracking — estimated current holdings per politician
  4. Watchlist comparison — overlap between congress trades and our tickers
  5. Discovery — tickers congress is trading that we're NOT watching

Data source: congress_trades collection (populated by congress_collector.py)
"""

import datetime
from collections import defaultdict
from app.db import mongo_store


def scan_recent_trades(days: int = 30) -> dict:
    """Get all congress trades from the last N days, grouped by ticker."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).date()

    query = {
        "trade_date": {"$gte": cutoff},
        "party": {"$nin": ["", None]},
    }
    docs = mongo_store.find_docs("congress_trades", query, sort=[("trade_date", -1)])

    trades = []
    for d in docs:
        trades.append(
            {
                "politician": d.get("politician"),
                "party": d.get("party"),
                "chamber": d.get("chamber"),
                "state": d.get("state"),
                "ticker": d.get("ticker"),
                "type": d.get("transaction_type"),
                "amount": d.get("amount_range"),
                "trade_date": str(d.get("trade_date")) if d.get("trade_date") else None,
                "disclosure_date": str(d.get("disclosure_date")) if d.get("disclosure_date") else None,
            }
        )

    return {
        "total_trades": len(trades),
        "period_days": days,
        "cutoff_date": str(cutoff),
        "trades": trades,
    }


def find_consensus_trades(days: int = 30, min_members: int = 2) -> list[dict]:
    """Find tickers that multiple congress members traded in the same direction."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).date()

    query = {
        "trade_date": {"$gte": cutoff},
        "party": {"$nin": ["", None]},
    }
    docs = mongo_store.find_docs("congress_trades", query)

    # Group by (ticker, transaction_type)
    groups = defaultdict(lambda: {"politicians": set(), "parties": set(), "dates": []})
    for d in docs:
        ticker = d.get("ticker")
        tx_type = d.get("transaction_type")
        pol = d.get("politician")
        party = d.get("party")
        t_date = d.get("trade_date")
        if not ticker or not tx_type or not pol:
            continue
        key = (ticker, tx_type)
        groups[key]["politicians"].add(pol)
        if party:
            groups[key]["parties"].add(party)
        if t_date:
            groups[key]["dates"].append(t_date)

    consensus = []
    for (ticker, direction), data in groups.items():
        if len(data["politicians"]) >= min_members:
            dates = sorted(data["dates"])
            consensus.append(
                {
                    "ticker": ticker,
                    "direction": direction,
                    "member_count": len(data["politicians"]),
                    "members": list(data["politicians"]),
                    "parties": list(data["parties"]),
                    "earliest_trade": str(dates[0]) if dates else None,
                    "latest_trade": str(dates[-1]) if dates else None,
                }
            )

    consensus.sort(key=lambda x: x["member_count"], reverse=True)
    return consensus


def build_politician_portfolios(top_n: int = 20) -> list[dict]:
    """Estimate current holdings per politician based on buy/sell history."""
    query = {"party": {"$nin": ["", None]}}
    docs = mongo_store.find_docs("congress_trades", query)

    # Group by politician
    pols = defaultdict(lambda: {
        "party": None,
        "chamber": None,
        "state": None,
        "trades": [],
        "ticker_buys": defaultdict(int),
        "ticker_sells": defaultdict(int),
        "ticker_last_date": {},
    })

    for d in docs:
        pol = d.get("politician")
        if not pol:
            continue
        p = pols[pol]
        p["party"] = d.get("party") or p["party"]
        p["chamber"] = d.get("chamber") or p["chamber"]
        p["state"] = d.get("state") or p["state"]
        p["trades"].append(d)

        ticker = d.get("ticker")
        tx_type = d.get("transaction_type")
        t_date = d.get("trade_date")
        if ticker:
            if tx_type == "buy":
                p["ticker_buys"][ticker] += 1
            elif tx_type == "sell":
                p["ticker_sells"][ticker] += 1
            if t_date:
                if ticker not in p["ticker_last_date"] or str(t_date) > str(p["ticker_last_date"][ticker]):
                    p["ticker_last_date"][ticker] = t_date

    sorted_pols = sorted(pols.items(), key=lambda item: len(item[1]["trades"]), reverse=True)[:top_n]

    portfolios = []
    for pol, data in sorted_pols:
        buys = sum(1 for d in data["trades"] if d.get("transaction_type") == "buy")
        sells = sum(1 for d in data["trades"] if d.get("transaction_type") == "sell")
        all_tickers = list({d.get("ticker") for d in data["trades"] if d.get("ticker")})

        held_tickers = [
            t for t, buy_count in data["ticker_buys"].items()
            if buy_count > data["ticker_sells"].get(t, 0)
        ]
        held_tickers.sort(key=lambda t: str(data["ticker_last_date"].get(t, "")), reverse=True)

        dates = sorted([d.get("trade_date") for d in data["trades"] if d.get("trade_date")])
        portfolios.append(
            {
                "politician": pol,
                "party": data["party"],
                "chamber": data["chamber"],
                "state": data["state"],
                "total_trades": len(data["trades"]),
                "buys": buys,
                "sells": sells,
                "all_tickers_traded": all_tickers,
                "estimated_holdings": held_tickers,
                "holding_count": len(held_tickers),
                "earliest_trade": str(dates[0]) if dates else None,
                "latest_trade": str(dates[-1]) if dates else None,
            }
        )

    return portfolios


def compare_with_watchlist(watchlist_tickers: list[str], days: int = 90) -> dict:
    """Compare congress trades against our watchlist."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).date()

    query = {
        "trade_date": {"$gte": cutoff},
        "party": {"$nin": ["", None]},
    }
    docs = mongo_store.find_docs("congress_trades", query, sort=[("trade_date", -1)])

    congress_set = {d.get("ticker").upper() for d in docs if d.get("ticker")}
    watchlist_set = {t.upper() for t in watchlist_tickers}

    overlap = congress_set & watchlist_set
    discovery = congress_set - watchlist_set
    not_traded = watchlist_set - congress_set

    # Trades by ticker
    trades_by_ticker = defaultdict(list)
    for d in docs:
        t = d.get("ticker")
        if t:
            trades_by_ticker[t.upper()].append(d)

    overlap_details = []
    for ticker in sorted(overlap):
        trades = trades_by_ticker[ticker]
        overlap_details.append(
            {
                "ticker": ticker,
                "trade_count": len(trades),
                "trades": [
                    {
                        "politician": t.get("politician"),
                        "type": t.get("transaction_type"),
                        "amount": t.get("amount_range"),
                        "date": str(t.get("trade_date")),
                    }
                    for t in trades[:5]
                ],
            }
        )

    discovery_details = []
    for ticker in sorted(discovery):
        trades = trades_by_ticker[ticker]
        if trades:
            discovery_details.append(
                {
                    "ticker": ticker,
                    "trade_count": len(trades),
                    "traders": list({t.get("politician") for t in trades if t.get("politician")}),
                    "latest_trade": str(trades[0].get("trade_date")) if trades[0].get("trade_date") else None,
                }
            )

    discovery_details.sort(key=lambda x: x["trade_count"], reverse=True)

    return {
        "overlap": overlap_details,
        "overlap_count": len(overlap),
        "discovery": discovery_details[:30],
        "discovery_count": len(discovery),
        "not_traded": sorted(not_traded),
        "not_traded_count": len(not_traded),
        "congress_total_tickers": len(congress_set),
        "watchlist_total": len(watchlist_set),
    }


def flag_notable_activity(days: int = 14) -> list[dict]:
    """Flag notable congress trading activity."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).date()

    query = {
        "trade_date": {"$gte": cutoff},
        "party": {"$nin": ["", None]},
    }
    docs = mongo_store.find_docs("congress_trades", query, sort=[("trade_date", -1)])

    flags = []
    large_keywords = ("50K", "100K", "250K", "500K", "1M", "5M")
    for d in docs:
        amt = d.get("amount_range") or ""
        if any(k in amt for k in large_keywords):
            flags.append(
                {
                    "type": "LARGE_TRADE",
                    "politician": d.get("politician"),
                    "party": d.get("party"),
                    "ticker": d.get("ticker"),
                    "direction": d.get("transaction_type"),
                    "amount": amt,
                    "date": str(d.get("trade_date")),
                }
            )

    # Cluster trades
    pol_trades = defaultdict(list)
    for d in docs:
        pol = d.get("politician")
        if pol:
            pol_trades[pol].append(d)

    for pol, trades in pol_trades.items():
        if len(trades) >= 3:
            dates = sorted([d.get("trade_date") for d in trades if d.get("trade_date")])
            tickers = list({d.get("ticker") for d in trades if d.get("ticker")})
            flags.append(
                {
                    "type": "CLUSTER_TRADE",
                    "politician": pol,
                    "trade_count": len(trades),
                    "tickers": tickers,
                    "first_date": str(dates[0]) if dates else None,
                    "last_date": str(dates[-1]) if dates else None,
                }
            )

    return flags


def generate_report(watchlist_tickers: list[str] | None = None) -> str:
    """Generate a human-readable congress trading report."""
    lines = []
    lines.append("=" * 70)
    lines.append("CONGRESS TRADING SCANNER REPORT")
    lines.append("=" * 70)

    recent = scan_recent_trades(days=30)
    lines.append(
        f"\n📊 Recent Activity (last 30 days): {recent['total_trades']} trades"
    )

    buys = [t for t in recent["trades"] if t["type"] == "buy"]
    sells = [t for t in recent["trades"] if t["type"] == "sell"]
    lines.append(f"   Buys: {len(buys)} | Sells: {len(sells)}")

    consensus = find_consensus_trades(days=30, min_members=2)
    if consensus:
        lines.append(f"\n🎯 Consensus Signals ({len(consensus)} found):")
        for c in consensus[:10]:
            lines.append(
                f"   {c['ticker']} — {c['direction'].upper()} by {c['member_count']} members: "
                f"{', '.join(c['members'][:3])}"
            )

    flags = flag_notable_activity(days=14)
    if flags:
        large = [f for f in flags if f["type"] == "LARGE_TRADE"]
        clusters = [f for f in flags if f["type"] == "CLUSTER_TRADE"]
        if large:
            lines.append(f"\n🚨 Large Trades ({len(large)}):")
            for f in large[:10]:
                lines.append(
                    f"   {f['politician']} ({f['party']}) — {f['direction'].upper()} "
                    f"{f['ticker']} ({f['amount']}) on {f['date']}"
                )
        if clusters:
            lines.append(f"\n📈 Cluster Traders ({len(clusters)}):")
            for f in clusters[:10]:
                lines.append(
                    f"   {f['politician']}: {f['trade_count']} trades in "
                    f"{', '.join(f['tickers'][:5])}"
                )

    portfolios = build_politician_portfolios(top_n=10)
    if portfolios:
        lines.append("\n👤 Top 10 Active Traders:")
        for p in portfolios:
            lines.append(
                f"   {p['politician']} ({p['party']}/{p['chamber']}) — "
                f"{p['total_trades']} trades, est. {p['holding_count']} holdings: "
                f"{', '.join(p['estimated_holdings'][:5])}"
            )

    if watchlist_tickers:
        comp = compare_with_watchlist(watchlist_tickers)
        lines.append("\n🔍 Watchlist Comparison:")
        lines.append(
            f"   Congress traded {comp['congress_total_tickers']} unique tickers"
        )
        lines.append(f"   Overlap with watchlist: {comp['overlap_count']} tickers")
        lines.append(
            f"   Discovery (not on watchlist): {comp['discovery_count']} tickers"
        )
        lines.append(f"   Watchlist not traded: {comp['not_traded_count']} tickers")

        if comp["overlap"]:
            lines.append("\n   📌 Overlap Details:")
            for o in comp["overlap"]:
                lines.append(f"      {o['ticker']}: {o['trade_count']} trades")
                for t in o["trades"][:3]:
                    lines.append(
                        f"         {t['politician']} — {t['type']} {t['amount']} ({t['date']})"
                    )

        if comp["discovery"]:
            lines.append("\n   🆕 Discovery (congress trading, you're not watching):")
            for d in comp["discovery"][:15]:
                lines.append(
                    f"      {d['ticker']}: {d['trade_count']} trades by "
                    f"{', '.join(d['traders'][:3])}"
                )

    lines.append(f"\n{'=' * 70}")
    return "\n".join(lines)
