import asyncio
import logging
import pandas as pd
import yfinance as yf
from app.db.connection import get_db

logger = logging.getLogger(__name__)

# Bounds on the bulk technicals pass. See _refresh_technicals_bulk for why
# these exist: an unbounded pass over ~503 tickers pinned CPU and tripped
# Docker's 10s healthcheck on the 2026-07-25 deploy.
_PER_TICKER_TIMEOUT_SEC = 20.0
_BULK_REFRESH_MAX_SEC = 240.0


async def collect_sp500_prices(period: str = "6mo"):
    """
    Batch downloads historical prices for all S&P 500 tickers from ticker_metadata.
    """
    logger.info(f"Batch downloading S&P 500 prices for period: {period}...")

    count = 0
    written_tickers: set[str] = set()

    with get_db() as db:
        rows = db.execute(
            "SELECT ticker FROM ticker_metadata WHERE sp500 = TRUE"
        ).fetchall()
        if not rows:
            logger.error(
                "No S&P 500 tickers found in ticker_metadata. Run load_sp500_universe first."
            )
            return {"total": 0}

        tickers = [row[0] for row in rows]

        # yfinance batch download is faster but can be fragile. We'll do a robust batch.
        # We download in chunks of 100 to avoid URL too long or memory spikes.
        chunk_size = 100
        inserts = []

        for i in range(0, len(tickers), chunk_size):
            chunk = tickers[i : i + chunk_size]
            logger.info(f"Downloading prices for batch {i // chunk_size + 1}...")

            try:
                # OFF THE EVENT LOOP. yf.download is synchronous network I/O
                # (~7s per 100-ticker chunk), and `collect_sp500_prices` is an
                # async function called from a background task — so six
                # sequential chunks blocked the loop for ~45s straight. The
                # HTTP server shares that loop, so /health stopped answering
                # and Docker marked the container UNHEALTHY on every deploy
                # (measured 2026-07-27: ~2 min unhealthy, CPU only 23% — it
                # was never CPU-bound, the loop simply never got a turn).
                #
                # asyncio.to_thread, not a shorter timeout on the healthcheck:
                # the healthcheck was telling the truth. The service really
                # was unable to serve requests.
                # We use group_by='ticker' to get a clean MultiIndex
                data = await asyncio.to_thread(
                    yf.download, chunk, period=period, group_by="ticker",
                    progress=False,
                )
                # Yield between chunks so anything else queued on the loop
                # (health probe, in-flight request) runs before the next one.
                await asyncio.sleep(0)

                # yf.download returns different structures depending on if 1 or multiple tickers are provided
                if len(chunk) == 1:
                    ticker = chunk[0]
                    for date, row in data.iterrows():
                        if pd.isna(row["Close"]):
                            continue
                        inserts.append(
                            (
                                ticker,
                                date.strftime("%Y-%m-%d"),
                                float(row["Open"])
                                if not pd.isna(row["Open"])
                                else None,
                                float(row["High"])
                                if not pd.isna(row["High"])
                                else None,
                                float(row["Low"]) if not pd.isna(row["Low"]) else None,
                                float(row["Close"])
                                if not pd.isna(row["Close"])
                                else None,
                                int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
                                "yfinance",
                            )
                        )
                else:
                    for ticker in chunk:
                        # Depending on yfinance version, columns might have ticker on level 0 or level 1.
                        # Usually if group_by='ticker', level 0 is ticker.
                        if ticker not in data.columns.levels[0]:
                            continue

                        ticker_data = data[ticker]
                        for date, row in ticker_data.iterrows():
                            # Use pd.isna safely on series values
                            # yfinance latest version might return a Series for Close instead of a scalar if not grouped correctly, but this should be fine.
                            try:
                                # Using .iloc or accessing by name
                                close_val = row.get("Close")
                                if close_val is None or pd.isna(close_val):
                                    continue

                                open_val = row.get("Open")
                                high_val = row.get("High")
                                low_val = row.get("Low")
                                volume_val = row.get("Volume")

                                inserts.append(
                                    (
                                        ticker,
                                        date.strftime("%Y-%m-%d"),
                                        float(open_val) if pd.notna(open_val) else None,
                                        float(high_val) if pd.notna(high_val) else None,
                                        float(low_val) if pd.notna(low_val) else None,
                                        float(close_val)
                                        if pd.notna(close_val)
                                        else None,
                                        int(volume_val) if pd.notna(volume_val) else 0,
                                        "yfinance",
                                    )
                                )
                            except Exception as inner_e:
                                # Skip row if weird format
                                pass
            except Exception as e:
                logger.error(f"Error downloading chunk starting with {chunk[0]}: {e}")

        if inserts:
            logger.info(f"Inserting {len(inserts)} price records into the database...")
            query = """
                INSERT INTO price_history (ticker, date, open, high, low, close, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, date, source) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """
            # Execute individually as our PooledCursor doesn't expose executemany
            # directly. ~2,000 synchronous round-trips, so this runs in a
            # worker thread for the same reason the download does — on the
            # event loop it was the second half of the deploy-time stall.
            def _insert_all() -> tuple[int, set]:
                written: set = set()
                inserted = 0
                for item in inserts:
                    try:
                        db.execute(query, item)
                        inserted += 1
                        written.add(item[0])
                    except Exception:
                        pass
                return inserted, written

            _inserted, _written = await asyncio.to_thread(_insert_all)
            count += _inserted
            written_tickers.update(_written)

            logger.info(f"Successfully collected and saved {count} price records.")

    # Technicals are a pure function of price_history, so every writer of that
    # table owes it a refresh. This loop runs daily over ~503 tickers from
    # boot_service._sp500_daily_refresh_loop and had NO refresh — the same
    # shape as the bug that served a 1963 RSI for CVX, on the writer whose
    # cadence best matches the original "5 of 503 fresh" symptom (2026-07-25
    # audit). Done outside the `get_db()` block so the connection is released
    # before the recompute, which opens its own.
    if written_tickers:
        await _refresh_technicals_bulk(sorted(written_tickers))

    return {"total": count}


async def _refresh_technicals_bulk(tickers: list[str]) -> None:
    """Recompute derived indicators for every ticker whose prices just changed.

    Mirrors `yfinance_collector._refresh_technicals`, but batched: this
    collector writes hundreds of tickers in one pass, so the per-ticker hook
    used on the single-ticker path does not fit.

    Fail-open per ticker AND in aggregate — stale technicals are bad, but a
    failure here must never cost us the price rows we just collected.

    THROTTLED, and that is load-bearing. The first deploy of this function ran
    ~503 recomputes back-to-back during boot, pinned CPU at ~86% and made
    Docker's 10s healthcheck time out three times running — the container went
    UNHEALTHY while the HTTP endpoint itself was answering in 0.02s. Each
    recompute is pandas/ta work on up to 500 rows; individually ~0.1s warm, but
    with no gap between them the event loop never gets a turn.

    So: a per-ticker timeout (one pathological ticker cannot wedge the run), a
    sleep(0) yield between tickers, and a wall-clock ceiling on the whole pass.
    Leftovers are picked up by the next run rather than blocking boot — the
    single-ticker hook in yfinance_collector already keeps cycle tickers fresh,
    so this bulk pass is a backstop, not the critical path.
    """
    import asyncio

    from app.processors.technical_processor import compute_technicals

    started = asyncio.get_event_loop().time()
    ok = 0
    failed = 0
    skipped = 0
    for idx, ticker in enumerate(tickers):
        if asyncio.get_event_loop().time() - started > _BULK_REFRESH_MAX_SEC:
            skipped = len(tickers) - idx
            logger.warning(
                "[sp500] technicals refresh hit its %.0fs ceiling — %d ticker(s) "
                "left for the next run (prices are already saved)",
                _BULK_REFRESH_MAX_SEC, skipped,
            )
            break
        try:
            await asyncio.wait_for(
                asyncio.to_thread(compute_technicals, ticker),
                timeout=_PER_TICKER_TIMEOUT_SEC,
            )
            ok += 1
        except Exception as e:
            failed += 1
            logger.warning(
                "[sp500] %s: technicals refresh failed (non-fatal): %s", ticker, e
            )
        # Hand the event loop back between tickers so health checks, the API
        # and the scheduler still get served during a long pass.
        await asyncio.sleep(0)
    logger.info(
        "[sp500] technicals refreshed for %d/%d tickers (%d failed, %d deferred)",
        ok, len(tickers), failed, skipped,
    )
