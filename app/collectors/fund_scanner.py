"""
Fund Scanner — Discovery-mode analysis of institutional 13F holdings.

Scans ALL 13F holdings in the DB and produces:
  1. Fund portfolio snapshots — top holdings per fund
  2. Cross-fund consensus — tickers held by multiple top funds
  3. Quarterly changes — new positions, exits, size changes
  4. Watchlist comparison — overlap between fund holdings and our tickers
  5. Discovery — tickers funds hold that we're NOT watching

Data source: sec_13f_holdings table (populated by sec_collector.py)
"""

from app.db.connection import get_db


def get_fund_portfolios(top_holdings: int = 20) -> list[dict]:
    """Get the top holdings for each fund in the latest filing quarter."""
    with get_db() as db:
        # Get the latest quarter per fund
        funds = db.execute("""
            SELECT DISTINCT f.filer_name, h.cik, h.filing_quarter
            FROM sec_13f_holdings h
            JOIN sec_13f_filers f ON h.cik = f.cik
            WHERE h.filing_quarter = (
                SELECT MAX(filing_quarter) FROM sec_13f_holdings AS sub
                WHERE sub.cik = h.cik
            )
            ORDER BY f.filer_name
        """).fetchall()

        portfolios = []
        for filer_name, cik, quarter in funds:
            holdings = db.execute(
                """
                SELECT ticker, shares, value_usd, pct_change,
                       is_new_position, is_exit
                FROM sec_13f_holdings
                WHERE cik = %s AND filing_quarter = %s
                ORDER BY value_usd DESC
                LIMIT %s
            """,
                [cik, quarter, top_holdings],
            ).fetchall()

            total_value = (
                db.execute(
                    """
                SELECT SUM(value_usd) FROM sec_13f_holdings
                WHERE cik = %s AND filing_quarter = %s
            """,
                    [cik, quarter],
                ).fetchone()[0]
                or 0
            )

            holding_count = (
                db.execute(
                    """
                SELECT COUNT(*) FROM sec_13f_holdings
                WHERE cik = %s AND filing_quarter = %s
            """,
                    [cik, quarter],
                ).fetchone()[0]
                or 0
            )

            top = []
            for h in holdings:
                pct_of_portfolio = (h[2] / total_value * 100) if total_value > 0 else 0
                top.append(
                    {
                        "ticker": h[0],
                        "shares": h[1],
                        "value_usd": h[2],
                        "pct_change": h[3],
                        "is_new": bool(h[4]),
                        "is_exit": bool(h[5]),
                        "pct_of_portfolio": round(pct_of_portfolio, 2),
                    }
                )

            portfolios.append(
                {
                    "fund": filer_name,
                    "quarter": quarter,
                    "total_value": total_value,
                    "holding_count": holding_count,
                    "top_holdings": top,
                }
            )

        return portfolios


def find_crossfund_consensus(min_funds: int = 3) -> list[dict]:
    """Find tickers held by multiple top funds — consensus = conviction.

    If Berkshire, Citadel, AND Renaissance all hold the same stock,
    that's a strong institutional conviction signal.
    """
    with get_db() as db:
        # Use latest quarter per fund
        rows = db.execute(
            """
            WITH latest_quarters AS (
                SELECT cik, MAX(filing_quarter) as q
                FROM sec_13f_holdings
                GROUP BY cik
            )
            SELECT h.ticker,
                   COUNT(DISTINCT h.cik) as fund_count,
                   STRING_AGG(DISTINCT f.filer_name, ', ') as funds,
                   SUM(h.value_usd) as total_value,
                   SUM(h.shares) as total_shares
            FROM sec_13f_holdings h
            JOIN latest_quarters lq ON h.cik = lq.cik AND h.filing_quarter = lq.q
            JOIN sec_13f_filers f ON h.cik = f.cik
            WHERE h.ticker != 'nan' AND h.ticker != '' AND LENGTH(h.ticker) <= 5
              -- yfinance rows carry a synthesized pseudo-CIK; counting them
              -- here would inflate fund counts by mixing incompatible sources.
              AND h.cik NOT LIKE 'yf_%%'
            GROUP BY h.ticker
            HAVING COUNT(DISTINCT h.cik) >= %s
            ORDER BY COUNT(DISTINCT h.cik) DESC, total_value DESC
        """,
            [min_funds],
        ).fetchall()

        consensus = []
        for r in rows:
            consensus.append(
                {
                    "ticker": r[0],
                    "fund_count": r[1],
                    "funds": r[2].split(",") if r[2] else [],
                    "total_value": r[3],
                    "total_shares": r[4],
                }
            )

        return consensus


def detect_quarterly_changes() -> dict:
    """Detect new positions, exits, and significant size changes across funds.

    Compares the latest filing quarter against the previous one.
    """
    with get_db() as db:
        # Each fund is compared against ITS OWN two most recent quarters.
        #
        # Picking the two latest quarters globally is wrong and was actively
        # harmful: funds file on staggered schedules, so at any given moment most
        # have not yet filed the newest quarter. Every one of those funds then had
        # its entire portfolio read as a mass liquidation. Measured on live data,
        # 1,249 of 2,799 reported exits were funds that simply had not filed yet —
        # 45% of the signal was noise.
        per_fund_quarters = """
            SELECT cik,
                   MAX(filing_quarter)                          AS latest_q,
                   MAX(filing_quarter) FILTER (
                       WHERE filing_quarter < (
                           SELECT MAX(inner_h.filing_quarter)
                           FROM sec_13f_holdings inner_h
                           WHERE inner_h.cik = outer_h.cik
                       )
                   )                                            AS prev_q
            FROM sec_13f_holdings outer_h
            WHERE cik NOT LIKE 'yf_%%'
            GROUP BY cik
            HAVING COUNT(DISTINCT filing_quarter) >= 2
        """

        new_positions = db.execute(
            f"""
            WITH fq AS ({per_fund_quarters})
            SELECT f.filer_name, h.ticker, h.shares, h.value_usd, h.filing_quarter
            FROM sec_13f_holdings h
            JOIN fq ON fq.cik = h.cik AND h.filing_quarter = fq.latest_q
            JOIN sec_13f_filers f ON h.cik = f.cik
            WHERE NOT EXISTS (
                SELECT 1 FROM sec_13f_holdings prev
                WHERE prev.cik = h.cik
                  AND prev.ticker = h.ticker
                  AND prev.filing_quarter = fq.prev_q
            )
              AND h.ticker != 'nan' AND h.ticker != ''
            ORDER BY h.value_usd DESC
            LIMIT 50
            """
        ).fetchall()

        exits = db.execute(
            f"""
            WITH fq AS ({per_fund_quarters})
            SELECT f.filer_name, h.ticker, h.shares, h.value_usd, h.filing_quarter
            FROM sec_13f_holdings h
            JOIN fq ON fq.cik = h.cik AND h.filing_quarter = fq.prev_q
            JOIN sec_13f_filers f ON h.cik = f.cik
            WHERE NOT EXISTS (
                SELECT 1 FROM sec_13f_holdings latest
                WHERE latest.cik = h.cik
                  AND latest.ticker = h.ticker
                  AND latest.filing_quarter = fq.latest_q
            )
              AND h.ticker != 'nan' AND h.ticker != ''
            ORDER BY h.value_usd DESC
            LIMIT 50
            """
        ).fetchall()

        if not new_positions and not exits:
            return {
                "new_positions": [],
                "exits": [],
                "size_changes": [],
                "note": "No fund has two or more quarters of filings yet",
            }

        return {
            "new_positions": [
                {"fund": r[0], "ticker": r[1], "shares": r[2], "value": r[3],
                 "quarter": r[4]}
                for r in new_positions
            ],
            "exits": [
                {"fund": r[0], "ticker": r[1], "shares": r[2], "value": r[3],
                 "quarter": r[4]}
                for r in exits
            ],
            "new_position_count": len(new_positions),
            "exit_count": len(exits),
        }


def compare_with_watchlist(watchlist_tickers: list[str]) -> dict:
    """Compare fund holdings against our watchlist.

    Returns:
      - overlap: tickers held by funds AND in our watchlist
      - discovery: tickers held by funds that we're NOT watching
      - not_held: watchlist tickers with no institutional presence
    """
    with get_db() as db:
        # All tickers currently held by funds (latest quarter)
        fund_tickers = db.execute("""
            WITH latest_quarters AS (
                SELECT cik, MAX(filing_quarter) as q
                FROM sec_13f_holdings GROUP BY cik
            )
            SELECT DISTINCT h.ticker
            FROM sec_13f_holdings h
            JOIN latest_quarters lq ON h.cik = lq.cik AND h.filing_quarter = lq.q
            WHERE h.ticker != 'nan' AND h.ticker != '' AND LENGTH(h.ticker) <= 5
              -- yfinance rows carry a synthesized pseudo-CIK; counting them
              -- here would inflate fund counts by mixing incompatible sources.
              AND h.cik NOT LIKE 'yf_%'
        """).fetchall()
        fund_set = {r[0] for r in fund_tickers}

        watchlist_set = {t.upper() for t in watchlist_tickers}

        overlap = fund_set & watchlist_set
        discovery = fund_set - watchlist_set
        not_held = watchlist_set - fund_set

        # Details for overlap
        overlap_details = []
        for ticker in sorted(overlap):
            holders = db.execute(
                """
                WITH latest_quarters AS (
                    SELECT cik, MAX(filing_quarter) as q
                    FROM sec_13f_holdings GROUP BY cik
                )
                SELECT f.filer_name, h.shares, h.value_usd
                FROM sec_13f_holdings h
                JOIN latest_quarters lq ON h.cik = lq.cik AND h.filing_quarter = lq.q
                JOIN sec_13f_filers f ON h.cik = f.cik
                WHERE h.ticker = %s
                ORDER BY h.value_usd DESC
            """,
                [ticker],
            ).fetchall()
            overlap_details.append(
                {
                    "ticker": ticker,
                    "fund_count": len(holders),
                    "total_value": sum(h[2] or 0 for h in holders),
                    "holders": [
                        {"fund": h[0], "shares": h[1], "value": h[2]}
                        for h in holders[:5]
                    ],
                }
            )

        # Details for discovery — top tickers by value held
        discovery_details = []
        for ticker in sorted(discovery):
            holders = db.execute(
                """
                WITH latest_quarters AS (
                    SELECT cik, MAX(filing_quarter) as q
                    FROM sec_13f_holdings GROUP BY cik
                )
                SELECT f.filer_name, h.shares, h.value_usd
                FROM sec_13f_holdings h
                JOIN latest_quarters lq ON h.cik = lq.cik AND h.filing_quarter = lq.q
                JOIN sec_13f_filers f ON h.cik = f.cik
                WHERE h.ticker = %s
                ORDER BY h.value_usd DESC
            """,
                [ticker],
            ).fetchall()
            total_val = sum(h[2] or 0 for h in holders)
            if total_val > 0:
                discovery_details.append(
                    {
                        "ticker": ticker,
                        "fund_count": len(holders),
                        "total_value": total_val,
                        "top_holder": holders[0][0] if holders else "",
                    }
                )

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


def generate_report(watchlist_tickers: list[str] | None = None) -> str:
    """Generate a human-readable institutional holdings report."""
    lines = []
    lines.append("=" * 70)
    lines.append("INSTITUTIONAL FUND SCANNER REPORT")
    lines.append("=" * 70)

    # Fund portfolios
    portfolios = get_fund_portfolios(top_holdings=10)
    lines.append(f"\n📊 Fund Portfolios ({len(portfolios)} funds tracked):")
    for p in portfolios:
        total_fmt = f"${p['total_value']:,.0f}" if p["total_value"] else "$0"
        lines.append(
            f"\n   {p['fund']} ({p['quarter']}) — "
            f"{p['holding_count']} holdings, {total_fmt} total"
        )
        for h in p["top_holdings"][:5]:
            val_fmt = f"${h['value_usd']:,.0f}" if h["value_usd"] else "$0"
            new_flag = " 🆕" if h["is_new"] else ""
            lines.append(
                f"      {h['ticker']}: {h['shares']:,} shares, "
                f"{val_fmt} ({h['pct_of_portfolio']:.1f}%){new_flag}"
            )

    # Cross-fund consensus
    consensus = find_crossfund_consensus(min_funds=2)
    if consensus:
        lines.append(
            f"\n🎯 Cross-Fund Consensus ({len(consensus)} tickers held by 2+ funds):"
        )
        for c in consensus[:15]:
            val_fmt = f"${c['total_value']:,.0f}" if c["total_value"] else "$0"
            lines.append(
                f"   {c['ticker']}: {c['fund_count']} funds ({val_fmt}) — "
                f"{', '.join(c['funds'][:3])}"
            )

    # Quarterly changes
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

    # Watchlist comparison
    if watchlist_tickers:
        comp = compare_with_watchlist(watchlist_tickers)
        lines.append("\n🔍 Watchlist Comparison:")
        lines.append(f"   Funds hold {comp['fund_total_tickers']} unique tickers")
        lines.append(f"   Overlap with watchlist: {comp['overlap_count']}")
        lines.append(
            f"   Discovery (funds hold, not on watchlist): {comp['discovery_count']}"
        )
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
                lines.append(
                    f"      {d['ticker']}: {d['fund_count']} funds, "
                    f"{val_fmt} (top: {d['top_holder']})"
                )

    lines.append(f"\n{'=' * 70}")
    return "\n".join(lines)


# ── Top Performer Fund Tiers ──
# Funds known for exceptional 3-year annualized returns.
# Used to weight conviction signals: a position held by a top performer
# is more meaningful than one held only by a mega-AUM indexer.
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


def get_institutional_signal(ticker: str) -> dict:
    """Get institutional positioning signal for a single ticker.

    Returns a dict with:
      - fund_count: how many tracked funds hold this ticker
      - holders: list of {fund, shares, value_usd, is_new, pct_of_portfolio}
      - total_institutional_value: aggregate value across all holders
      - has_new_position: True if any fund opened a new position this quarter
      - has_top_performer: True if any top-performing fund holds it
      - top_performer_names: list of top-performer fund names holding it
      - momentum: "INCREASING" | "DECREASING" | "FLAT" | "UNKNOWN"
    """
    ticker = ticker.upper().strip()
    with get_db() as db:
        rows = db.execute(
            """
            WITH latest_quarters AS (
                SELECT cik, MAX(filing_quarter) as q
                FROM sec_13f_holdings GROUP BY cik
            )
            SELECT f.filer_name, h.cik, h.shares, h.value_usd,
                   h.is_new_position, h.pct_change
            FROM sec_13f_holdings h
            JOIN latest_quarters lq ON h.cik = lq.cik AND h.filing_quarter = lq.q
            JOIN sec_13f_filers f ON h.cik = f.cik
            WHERE h.ticker = %s
            ORDER BY h.value_usd DESC
            """,
            [ticker],
        ).fetchall()

        if not rows:
            return {
                "fund_count": 0,
                "holders": [],
                "total_institutional_value": 0,
                "has_new_position": False,
                "has_top_performer": False,
                "top_performer_names": [],
                "momentum": "UNKNOWN",
            }

        holders = []
        total_value = 0
        has_new = False
        top_perf_names = []

        for r in rows:
            fund_name, cik, shares, value, is_new, pct_change = r
            value = value or 0
            total_value += value
            if is_new:
                has_new = True
            if cik in TOP_PERFORMER_CIKS:
                top_perf_names.append(fund_name)
            holders.append({
                "fund": fund_name,
                "shares": shares or 0,
                "value_usd": value,
                "is_new": bool(is_new),
                "pct_change": float(pct_change) if pct_change else 0.0,
            })

        # Determine aggregate momentum from pct_change values
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
            "holders": holders[:10],  # cap at top 10 by value
            "total_institutional_value": total_value,
            "has_new_position": has_new,
            "has_top_performer": len(top_perf_names) > 0,
            "top_performer_names": top_perf_names,
            "momentum": momentum,
        }


def get_top_conviction_tickers(min_funds: int = 2, max_results: int = 30) -> list[dict]:
    """Return tickers ranked by institutional conviction score.

    Conviction score = fund_count * 10 + top_performer_count * 15 + new_position_bonus.
    This feeds the Discovery Engine as an additional lead source alongside
    news/Reddit/YouTube trending.
    """
    with get_db() as db:
        rows = db.execute(
            """
            WITH latest_quarters AS (
                SELECT cik, MAX(filing_quarter) as q
                FROM sec_13f_holdings GROUP BY cik
            )
            SELECT h.ticker,
                   COUNT(DISTINCT h.cik) as fund_count,
                   STRING_AGG(DISTINCT f.filer_name, ', ') as fund_names,
                   SUM(h.value_usd) as total_value,
                   BOOL_OR(COALESCE(h.is_new_position, FALSE)) as any_new,
                   STRING_AGG(DISTINCT h.cik, ',') as cik_list
            FROM sec_13f_holdings h
            JOIN latest_quarters lq ON h.cik = lq.cik AND h.filing_quarter = lq.q
            JOIN sec_13f_filers f ON h.cik = f.cik
            WHERE h.ticker != 'nan' AND h.ticker != '' AND LENGTH(h.ticker) <= 5
              -- yfinance rows carry a synthesized pseudo-CIK; counting them
              -- here would inflate fund counts by mixing incompatible sources.
              AND h.cik NOT LIKE 'yf_%%'
            GROUP BY h.ticker
            HAVING COUNT(DISTINCT h.cik) >= %s
            ORDER BY COUNT(DISTINCT h.cik) DESC, SUM(h.value_usd) DESC
            """,
            [min_funds],
        ).fetchall()

        results = []
        for r in rows:
            ticker, fund_count, fund_names, total_value, any_new, cik_list = r
            # Count how many top-performer funds hold this ticker
            ciks = set((cik_list or "").split(","))
            top_perf_count = len(ciks & TOP_PERFORMER_CIKS)

            # Conviction score
            score = (fund_count * 10) + (top_perf_count * 15)
            if any_new:
                score += 10  # new position bonus

            results.append({
                "ticker": ticker,
                "fund_count": fund_count,
                "fund_names": (fund_names or "").split(", ")[:5],
                "total_value": total_value or 0,
                "has_new_position": bool(any_new),
                "top_performer_count": top_perf_count,
                "conviction_score": score,
            })

        # Sort by conviction score descending
        results.sort(key=lambda x: x["conviction_score"], reverse=True)
        return results[:max_results]


def get_fund_momentum(ticker: str) -> dict:
    """Compare latest vs previous quarter holdings for a ticker.

    Returns:
      - direction: "INCREASING" | "DECREASING" | "FLAT" | "NO_HISTORY"
      - new_buyers: funds that opened a new position this quarter
      - exiters: funds that exited this quarter
      - net_share_change: total share count change across all holders
      - net_value_change: total value change across all holders
      - latest_quarter: the quarter being compared
      - previous_quarter: the baseline quarter
    """
    ticker = ticker.upper().strip()
    with get_db() as db:
        # Get two most recent quarters that have data for this ticker
        quarters = db.execute(
            """
            SELECT DISTINCT filing_quarter FROM sec_13f_holdings
            WHERE ticker = %s
            ORDER BY filing_quarter DESC LIMIT 2
            """,
            [ticker],
        ).fetchall()

        if len(quarters) < 2:
            return {
                "direction": "NO_HISTORY",
                "new_buyers": [],
                "exiters": [],
                "net_share_change": 0,
                "net_value_change": 0,
                "latest_quarter": quarters[0][0] if quarters else None,
                "previous_quarter": None,
            }

        latest_q = quarters[0][0]
        prev_q = quarters[1][0]

        # Latest quarter holdings
        latest = db.execute(
            """
            SELECT f.filer_name, h.shares, h.value_usd
            FROM sec_13f_holdings h
            JOIN sec_13f_filers f ON h.cik = f.cik
            WHERE h.ticker = %s AND h.filing_quarter = %s
            """,
            [ticker, latest_q],
        ).fetchall()

        # Previous quarter holdings
        prev = db.execute(
            """
            SELECT f.filer_name, h.shares, h.value_usd
            FROM sec_13f_holdings h
            JOIN sec_13f_filers f ON h.cik = f.cik
            WHERE h.ticker = %s AND h.filing_quarter = %s
            """,
            [ticker, prev_q],
        ).fetchall()

        latest_map = {r[0]: {"shares": r[1] or 0, "value": r[2] or 0} for r in latest}
        prev_map = {r[0]: {"shares": r[1] or 0, "value": r[2] or 0} for r in prev}

        new_buyers = [f for f in latest_map if f not in prev_map]
        exiters = [f for f in prev_map if f not in latest_map]

        # Net changes for funds present in both quarters
        net_shares = 0
        net_value = 0
        for fund in latest_map:
            lat = latest_map[fund]
            prv = prev_map.get(fund, {"shares": 0, "value": 0})
            net_shares += lat["shares"] - prv["shares"]
            net_value += lat["value"] - prv["value"]
        # Subtract exited positions
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
