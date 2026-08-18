"""
Data Sanity Guardrails — Post-collection spot checks.

Run after data collection to catch value conversion bugs,
missing data, and obviously wrong values before they reach the LLM.

Usage:
    from app.processors.data_sanity import run_sanity_checks
    failures = run_sanity_checks()
    if failures:
        for f in failures:
            print(f"❌ {f}")
    else:
        print("✅ All sanity checks passed")
"""

import logging
from app.db import mongo_store


def run_sanity_checks() -> list[str]:
    """Run all sanity checks. Returns list of failure messages. Empty = all good."""
    failures = []
    # ── 13F Holdings ──
    try:
        # No single position > $1T
        top_pos = mongo_store.find_docs("sec_13f_holdings", {}, sort=[("value_usd", -1)], limit=1)
        if top_pos and top_pos[0].get("value_usd") and top_pos[0]["value_usd"] > 1e12:
            failures.append(
                f"13F: Max position value ${top_pos[0]['value_usd'] / 1e9:.1f}B exceeds $1T ceiling"
            )

        # No position with negative value
        neg_count = mongo_store.count_docs("sec_13f_holdings", {"value_usd": {"$lt": 0}})
        if neg_count > 0:
            failures.append(f"13F: {neg_count} holdings with negative value_usd")

        # No position with 0 shares but positive value
        zero_share_count = mongo_store.count_docs("sec_13f_holdings", {"shares": {"$lte": 0}, "value_usd": {"$gt": 0}})
        if zero_share_count > 0:
            failures.append(
                f"13F: {zero_share_count} holdings with 0 shares but positive value"
            )

        # Berkshire AAPL sanity (if exists)
        brk_docs = mongo_store.find_docs(
            "sec_13f_holdings",
            {"ticker": "AAPL", "filer_name": {"$regex": "Berkshire", "$options": "i"}},
            sort=[("value_usd", -1)],
            limit=1,
        )
        if brk_docs and brk_docs[0].get("value_usd"):
            val = brk_docs[0]["value_usd"]
            if val < 30e9:
                failures.append(
                    f"13F: Berkshire AAPL = ${val / 1e9:.1f}B (expected > $30B)"
                )
            elif val > 300e9:
                failures.append(
                    f"13F: Berkshire AAPL = ${val / 1e9:.1f}B (expected < $300B)"
                )
    except Exception as e:
        logger.warning(f"13F sanity check error: {e}")

    # ── Fundamentals ──
    try:
        # AAPL market cap > $1T
        aapl_fund = mongo_store.find_docs("fundamentals", {"ticker": "AAPL"}, sort=[("snapshot_date", -1)], limit=1)
        if aapl_fund and aapl_fund[0].get("market_cap") and aapl_fund[0]["market_cap"] < 1e12:
            failures.append(
                f"Fundamentals: AAPL market cap ${aapl_fund[0]['market_cap'] / 1e9:.1f}B < $1T"
            )

        # No negative market caps
        neg_mkt_cap = mongo_store.count_docs("fundamentals", {"market_cap": {"$lt": 0}})
        if neg_mkt_cap > 0:
            failures.append(
                f"Fundamentals: {neg_mkt_cap} tickers with negative market cap"
            )

        # P/E ratio sanity (should be 0-500 or NULL)
        extreme_pe = mongo_store.find_docs(
            "fundamentals",
            {"pe_ratio": {"$ne": None}, "$or": [{"pe_ratio": {"$lt": -100}}, {"pe_ratio": {"$gt": 1000}}]},
            limit=5,
        )
        if extreme_pe:
            tickers = [f"{r.get('ticker')}={r.get('pe_ratio'):.0f}" for r in extreme_pe if r.get("pe_ratio") is not None]
            failures.append(
                f"Fundamentals: Extreme P/E ratios: {', '.join(tickers)}"
            )
    except Exception as e:
        logger.warning(f"Fundamentals sanity check error: {e}")

    # ── Price Data ──
    try:
        # No $0 or negative prices
        zero_prices = mongo_store.count_docs("price_history", {"close": {"$lte": 0}})
        if zero_prices > 0:
            failures.append(f"Prices: {zero_prices} rows with close <= $0")
    except Exception as e:
        logger.warning(f"Price sanity check error: {e}")

    # ── Congress Trades ──
    try:
        # Check both chambers exist
        c_docs = mongo_store.find_docs("congress_trades", {"chamber": {"$nin": ["", None]}})
        chamber_set = {d.get("chamber") for d in c_docs if d.get("chamber")}
        if (
            chamber_set
            and "Senate" not in chamber_set
            and "senate" not in {c.lower() for c in chamber_set}
        ):
            failures.append(
                f"Congress: Only chambers found: {chamber_set} — missing Senate data"
            )
        if (
            chamber_set
            and "House" not in chamber_set
            and "house" not in {c.lower() for c in chamber_set}
        ):
            failures.append(
                f"Congress: Only chambers found: {chamber_set} — missing House data"
            )
    except Exception as e:
        logger.warning(f"Congress sanity check error: {e}")

    # ── Technicals ──
    try:
        # RSI should be 0-100
        bad_rsi = mongo_store.count_docs("technicals", {"rsi_14": {"$ne": None}, "$or": [{"rsi_14": {"$lt": 0}}, {"rsi_14": {"$gt": 100}}]})
        if bad_rsi > 0:
            failures.append(
                f"Technicals: {bad_rsi} rows with RSI outside 0-100 range"
            )
    except Exception as e:
        logger.warning(f"Technicals sanity check error: {e}")

    # ── News Content Quality ──
    try:
        # Articles containing known truncation markers (NewsAPI free tier, paywalls)
        trunc_count = mongo_store.count_docs(
            "news_articles",
            {
                "summary": {"$regex": r"(\[\+\d+|subscribe to read|log in to read|continue reading|cookie settings|access denied)", "$options": "i"},
                "$or": [{"quality_status": None}, {"quality_status": "relevant"}],
            },
        )
        if trunc_count > 10:
            failures.append(
                f"News: {trunc_count} unprocessed articles with paywall/truncation markers"
            )

        # Completely empty content
        empty_count = mongo_store.count_docs("news_articles", {"$or": [{"summary": None}, {"summary": ""}]})
        if empty_count > 0:
            failures.append(
                f"News: {empty_count} articles with NULL or empty summary"
            )
    except Exception as e:
        logger.warning(f"News content quality sanity check error: {e}")

    return failures


def print_sanity_report():
    """Run checks and print formatted report."""
    print("\n" + "=" * 60)
    print("[SANITY CHECK] Post-Collection Data Validation")
    print("=" * 60)

    failures = run_sanity_checks()

    if not failures:
        print("  [OK] All sanity checks passed")
    else:
        for f in failures:
            print(f"  [FAIL] {f}")
        print(
            f"\n  {len(failures)} issue(s) detected — review before running LLM analysis"
        )

    print("=" * 60 + "\n")
    return failures
