import logging
import datetime
from typing import Optional
from app.db import mongo_query
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

    # SELECT * ... ORDER BY fetched_at DESC LIMIT 1
    docs = mongo_query.find_dicts(
        'market_snapshots',
        {'ticker': ticker, 'fetched_at': {'$gte': threshold}},
        sort=[('fetched_at', -1)], limit=1,
    )
    if not docs:
        return None

    # A Mongo doc can simply omit a field a PG row would have held as NULL,
    # so every read below is a .get() — a subscript would KeyError.
    data = docs[0]

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
        ticker=data.get("ticker"),
        fetched_at=fetched_at,
        data_source=data.get("data_source"),
        candles_used=data.get("candles_used"),
        price=data.get("price"),
        open=data.get("open"),
        high=data.get("high"),
        low=data.get("low"),
        volume=data.get("volume"),
        vwap=data.get("vwap"),
        rsi_14=data.get("rsi_14"),
        macd=data.get("macd"),
        macd_signal=data.get("macd_signal"),
        macd_hist=data.get("macd_hist"),
        bb_upper=data.get("bb_upper"),
        bb_lower=data.get("bb_lower"),
        bb_pct=data.get("bb_pct"),
        sma_20=data.get("sma_20"),
        sma_50=data.get("sma_50"),
        sma_200=data.get("sma_200"),
        atr_14=data.get("atr_14"),
        adx_14=data.get("adx_14"),
        stoch_k=data.get("stoch_k"),
        stoch_d=data.get("stoch_d"),
        returns_1d=data.get("returns_1d"),
        returns_5d=data.get("returns_5d"),
        returns_20d=data.get("returns_20d"),
        volatility_20d=data.get("volatility_20d"),
        sharpe_20d=data.get("sharpe_20d"),
        max_drawdown_20d=data.get("max_drawdown_20d"),
        beta_20d=data.get("beta_20d"),
        pe_ratio=data.get("pe_ratio"),
        forward_pe=data.get("forward_pe"),
        eps=data.get("eps"),
        market_cap=data.get("market_cap"),
        revenue_growth=data.get("revenue_growth"),
        profit_margin=data.get("profit_margin"),
        debt_to_equity=data.get("debt_to_equity"),
    )
