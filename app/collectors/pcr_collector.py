import logging
import asyncio
import datetime
import yfinance as yf

from app.db.connection import get_db

logger = logging.getLogger(__name__)

async def collect_spy_pcr() -> bool:
    """
    Fetches the SPY options chain using yfinance, calculates the total
    Put/Call Ratio (by volume and open interest) across the front 3 expirations,
    and saves it to the database.
    
    This acts as a free, highly-correlated proxy for the CBOE Equity Put/Call Ratio.
    """
    try:
        spy = yf.Ticker("SPY")
        
        # This is an I/O bound call, we must run it in a thread
        expirations = await asyncio.to_thread(lambda: spy.options)
        
        if not expirations:
            logger.warning("[pcr_collector] No SPY options data found.")
            return False
            
        total_put_oi = 0
        total_call_oi = 0
        total_put_vol = 0
        total_call_vol = 0
        
        # Only check the first 3 expirations to avoid massive payloads and capture near-term sentiment
        for date in expirations[:3]:
            opt = await asyncio.to_thread(spy.option_chain, date)
            
            # Puts
            total_put_oi += opt.puts['openInterest'].sum()
            total_put_vol += opt.puts['volume'].sum()
            
            # Calls
            total_call_oi += opt.calls['openInterest'].sum()
            total_call_vol += opt.calls['volume'].sum()
            
        pcr_volume = total_put_vol / total_call_vol if total_call_vol > 0 else 0
        pcr_oi = total_put_oi / total_call_oi if total_call_oi > 0 else 0
        
        today = datetime.date.today()
        
        def _insert():
            with get_db() as db:
                db.execute(
                    """
                    INSERT INTO put_call_ratio (
                        symbol, date, pcr_volume, pcr_oi, 
                        total_put_vol, total_call_vol, total_put_oi, total_call_oi
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, date) DO UPDATE SET
                        pcr_volume = EXCLUDED.pcr_volume,
                        pcr_oi = EXCLUDED.pcr_oi,
                        total_put_vol = EXCLUDED.total_put_vol,
                        total_call_vol = EXCLUDED.total_call_vol,
                        total_put_oi = EXCLUDED.total_put_oi,
                        total_call_oi = EXCLUDED.total_call_oi
                    """,
                    [
                        "SPY",
                        today,
                        float(pcr_volume),
                        float(pcr_oi),
                        int(total_put_vol),
                        int(total_call_vol),
                        int(total_put_oi),
                        int(total_call_oi)
                    ]
                )
                
        await asyncio.to_thread(_insert)
        logger.info(f"[pcr_collector] SPY PCR updated for {today}. Vol: {pcr_volume:.2f}, OI: {pcr_oi:.2f}")
        return True
        
    except Exception as e:
        logger.error(f"[pcr_collector] Error collecting SPY PCR: {e}", exc_info=True)
        return False

async def collect_all() -> bool:
    """Entry point for the PCR collector job."""
    return await collect_spy_pcr()
