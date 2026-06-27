import yfinance as yf
import pandas as pd
import asyncio
import logging

logger = logging.getLogger(__name__)

async def get_watchlist_snapshots(ticker_data: list[dict]) -> tuple[str, list]:
    """
    Bulk fetches recent market data (5d) for a list of ticker dictionaries using yfinance.
    Calculates Price, % Change (daily), and Relative Volume.
    Returns a tuple of (Markdown formatted table, raw_results_list).
    """
    if not ticker_data:
        return "No tickers provided.", []

    # Extract unique tickers for yfinance
    tickers_list = []
    ticker_meta = {}
    for t_obj in ticker_data:
        tkr = t_obj.get("ticker", "").upper().strip()
        if tkr and tkr not in ticker_meta:
            tickers_list.append(tkr)
            ticker_meta[tkr] = {
                "source": t_obj.get("source", "Watchlist"),
                "days_since_analysis": t_obj.get("days_since_analysis", "Never")
            }
            
    # Limit to prevent massive payload size issues
    tickers_list = tickers_list[:100]
    
    logger.info(f"[batch_screener] Fetching bulk yfinance data for {len(tickers_list)} tickers...")
    
    try:
        # Run yfinance download in a thread to prevent blocking the async loop
        df = await asyncio.to_thread(
            yf.download,
            " ".join(tickers_list),
            period="2mo",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False
        )
        
        if df.empty:
            return "Failed to fetch data."

        results = []
        
        # Process tickers cleanly handling both MultiIndex and flat Index (yfinance behavior varies by version)
        for t in tickers_list:
            try:
                # If MultiIndex (new behavior or multi-ticker)
                if isinstance(df.columns, pd.MultiIndex):
                    if t in df.columns.levels[0]:
                        ticker_df = df[t].dropna()
                    elif t in df.columns.levels[1]:
                        ticker_df = df.xs(t, level=1, axis=1).dropna()
                    else:
                        continue
                else:
                    # Flat Index (old behavior for single ticker)
                    ticker_df = df.dropna()
                    
                if len(ticker_df) >= 20:
                    current_price = float(ticker_df['Close'].iloc[-1])
                    prev_price = float(ticker_df['Close'].iloc[-2])
                    change_pct = ((current_price - prev_price) / prev_price) * 100
                    
                    vol_today = float(ticker_df['Volume'].iloc[-1])
                    avg_vol = float(ticker_df['Volume'].mean())
                    rel_vol = vol_today / avg_vol if avg_vol > 0 else 0
                    
                    sma20 = float(ticker_df['Close'].rolling(window=20).mean().iloc[-1])
                    delta = ticker_df['Close'].diff()
                    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                    rs = gain / loss
                    rsi = float((100 - (100 / (1 + rs))).iloc[-1])
                    
                    results.append((t, current_price, change_pct, rel_vol, sma20, rsi, ticker_meta[t]["source"], ticker_meta[t]["days_since_analysis"]))
            except Exception as e:
                logger.warning(f"[batch_screener] Error parsing {t}: {e}")


        if not results:
            return "No valid data parsed.", []

        # Sort by relative volume descending
        results.sort(key=lambda x: x[3], reverse=True)

        md_lines = []
        md_lines.append("| Ticker | Source | Days Since Analysis | Price | Change % | Rel Volume | SMA-20 | RSI (14) |")
        md_lines.append("|--------|--------|---------------------|-------|----------|------------|--------|----------|")
        for t, px, chg, rvol, sma, rsi, src, dsa in results:
            sma_rel = ((px - sma) / sma) * 100 if sma > 0 else 0
            md_lines.append(f"| {t} | {src} | {dsa} | ${px:.2f} | {chg:+.2f}% | {rvol:.2f}x | {sma_rel:+.2f}% | {rsi:.1f} |")

        return "\n".join(md_lines), results
    except Exception as e:
        logger.error(f"[batch_screener] Bulk fetch failed: {e}")
        return f"Error fetching snapshot data: {e}", []
