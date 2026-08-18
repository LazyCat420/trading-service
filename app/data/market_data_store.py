import logging
import datetime
from typing import Optional
from app.data.market_snapshot import MarketSnapshot
from app.db import mongo_store

logger = logging.getLogger(__name__)


def save_snapshot(snapshot: MarketSnapshot):
    """Save a market snapshot to the database."""
    mongo_store.upsert_doc('market_snapshots', {'ticker': snapshot.ticker, 'fetched_at': snapshot.fetched_at}, {'ticker': snapshot.ticker, 'fetched_at': snapshot.fetched_at, 'data_source': snapshot.data_source, 'candles_used': snapshot.candles_used, 'price': snapshot.price, 'open': snapshot.open, 'high': snapshot.high, 'low': snapshot.low, 'volume': snapshot.volume, 'vwap': snapshot.vwap, 'rsi_14': snapshot.rsi_14, 'macd': snapshot.macd, 'macd_signal': snapshot.macd_signal, 'macd_hist': snapshot.macd_hist, 'bb_upper': snapshot.bb_upper, 'bb_lower': snapshot.bb_lower, 'bb_pct': snapshot.bb_pct, 'sma_20': snapshot.sma_20, 'sma_50': snapshot.sma_50, 'sma_200': snapshot.sma_200, 'atr_14': snapshot.atr_14, 'adx_14': snapshot.adx_14, 'stoch_k': snapshot.stoch_k, 'stoch_d': snapshot.stoch_d, 'returns_1d': snapshot.returns_1d, 'returns_5d': snapshot.returns_5d, 'returns_20d': snapshot.returns_20d, 'volatility_20d': snapshot.volatility_20d, 'sharpe_20d': snapshot.sharpe_20d, 'max_drawdown_20d': snapshot.max_drawdown_20d, 'beta_20d': snapshot.beta_20d, 'pe_ratio': snapshot.pe_ratio, 'forward_pe': snapshot.forward_pe, 'eps': snapshot.eps, 'market_cap': snapshot.market_cap, 'revenue_growth': snapshot.revenue_growth, 'profit_margin': snapshot.profit_margin, 'debt_to_equity': snapshot.debt_to_equity}, insert_only=True)
    from app.telemetry import send_system_log
    send_system_log(
        subsystem="DB",
        message=f"Upserted market snapshot for {snapshot.ticker} to market_snapshots"
    )


def get_latest_snapshot(
    ticker: str, max_age_minutes: int = 15
) -> Optional[MarketSnapshot]:
    """Retrieve the most recent market snapshot for a ticker if it's within max_age_minutes."""
    threshold = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=max_age_minutes
    )

    with get_db() as db:
        cur = db.execute(
            """
            SELECT * FROM market_snapshots 
            WHERE ticker = %s AND fetched_at >= %s
            ORDER BY fetched_at DESC LIMIT 1
            """,
            [ticker, threshold],
        )
        row = cur.fetchone()

    if not row:
        return None

    cols = [description[0] for description in cur.description]
    data = dict(zip(cols, row))

    # Parse fetched_at string back to datetime if needed
    fetched_at = data.get("fetched_at")
    if isinstance(fetched_at, str):
        try:
            fetched_at = datetime.datetime.fromisoformat(
                fetched_at.replace("Z", "+00:00")
            )
        except ValueError:
            pass

    # Initialize dataclass with the exact properties
    return MarketSnapshot(
        ticker=data["ticker"],
        fetched_at=fetched_at,
        data_source=data["data_source"],
        candles_used=data["candles_used"],
        price=data["price"],
        open=data["open"],
        high=data["high"],
        low=data["low"],
        volume=data["volume"],
        vwap=data["vwap"],
        rsi_14=data["rsi_14"],
        macd=data["macd"],
        macd_signal=data["macd_signal"],
        macd_hist=data["macd_hist"],
        bb_upper=data["bb_upper"],
        bb_lower=data["bb_lower"],
        bb_pct=data["bb_pct"],
        sma_20=data["sma_20"],
        sma_50=data["sma_50"],
        sma_200=data["sma_200"],
        atr_14=data["atr_14"],
        adx_14=data["adx_14"],
        stoch_k=data["stoch_k"],
        stoch_d=data["stoch_d"],
        returns_1d=data["returns_1d"],
        returns_5d=data["returns_5d"],
        returns_20d=data["returns_20d"],
        volatility_20d=data["volatility_20d"],
        sharpe_20d=data["sharpe_20d"],
        max_drawdown_20d=data["max_drawdown_20d"],
        beta_20d=data["beta_20d"],
        pe_ratio=data["pe_ratio"],
        forward_pe=data["forward_pe"],
        eps=data["eps"],
        market_cap=data["market_cap"],
        revenue_growth=data["revenue_growth"],
        profit_margin=data["profit_margin"],
        debt_to_equity=data["debt_to_equity"],
    )
