import yfinance as yf
import pandas as pd
import asyncio
import logging

logger = logging.getLogger(__name__)

async def get_watchlist_snapshots(tickers: list[str]) -> str:
    """
    Bulk fetches recent market data (5d) for a list of tickers using yfinance.
    Calculates Price, % Change (daily), and Relative Volume.
    Returns a Markdown formatted table.
    """
    if not tickers:
        return "No tickers provided."

    # Remove duplicates and limit to prevent massive payload size issues
    tickers = list(set(tickers))[:100]
    
    logger.info(f"[batch_screener] Fetching bulk yfinance data for {len(tickers)} tickers...")
    
    try:
        # Run yfinance download in a thread to prevent blocking the async loop
        df = await asyncio.to_thread(
            yf.download,
            " ".join(tickers),
            period="5d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False
        )
        
        if df.empty:
            return "Failed to fetch data."

        results = []
        
        # If only one ticker is requested, yfinance doesn't use the MultiIndex column
        if len(tickers) == 1:
            t = tickers[0]
            try:
                if len(df) >= 2:
                    current_price = df['Close'].iloc[-1]
                    prev_price = df['Close'].iloc[-2]
                    change_pct = ((current_price - prev_price) / prev_price) * 100
                    
                    vol_today = df['Volume'].iloc[-1]
                    avg_vol = df['Volume'].mean()
                    rel_vol = vol_today / avg_vol if avg_vol > 0 else 0
                    
                    results.append((t, current_price, change_pct, rel_vol))
            except Exception as e:
                logger.warning(f"[batch_screener] Error parsing {t}: {e}")
        else:
            for t in tickers:
                try:
                    if t in df.columns.levels[0]:
                        ticker_df = df[t].dropna()
                        if len(ticker_df) >= 2:
                            current_price = ticker_df['Close'].iloc[-1]
                            prev_price = ticker_df['Close'].iloc[-2]
                            change_pct = ((current_price - prev_price) / prev_price) * 100
                            
                            vol_today = ticker_df['Volume'].iloc[-1]
                            avg_vol = ticker_df['Volume'].mean()
                            rel_vol = vol_today / avg_vol if avg_vol > 0 else 0
                            
                            results.append((t, current_price, change_pct, rel_vol))
                except Exception as e:
                    logger.warning(f"[batch_screener] Error parsing {t}: {e}")

        if not results:
            return "No valid data parsed."

        # Sort by relative volume descending
        results.sort(key=lambda x: x[3], reverse=True)

        md_lines = []
        md_lines.append("| Ticker | Price | Change % | Rel Volume |")
        md_lines.append("|--------|-------|----------|------------|")
        for t, px, chg, rvol in results:
            md_lines.append(f"| {t} | ${px:.2f} | {chg:+.2f}% | {rvol:.2f}x |")

        return "\n".join(md_lines)
    except Exception as e:
        logger.error(f"[batch_screener] Bulk fetch failed: {e}")
        return f"Error fetching snapshot data: {e}"
