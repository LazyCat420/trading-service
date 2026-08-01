import pandera as pa
from pandera.typing import Series
import pandas as pd

# Split-adjusted deep history carries float noise: 1980s AAPL adjusts down to
# ~$0.10, and at that magnitude yfinance's adjustment arithmetic can land High a
# few ULPs under Open/Close. Exact comparison rejected AAPL's and MSFT's entire
# 40-year history while passing AMZN's. Tolerance is relative so it scales with
# price, and 1e-6 is far tighter than any real OHLC inconsistency.
_OHLC_RTOL = 1e-6


def _lower_bound(s: pd.Series) -> pd.Series:
    return s - (s.abs() * _OHLC_RTOL)


def _upper_bound(s: pd.Series) -> pd.Series:
    return s + (s.abs() * _OHLC_RTOL)


def high_is_max_mask(df: pd.DataFrame) -> pd.Series:
    # High should be >= Low, Open, and Close (within float tolerance)
    return (
        (df["High"] >= _lower_bound(df["Low"]))
        & (df["High"] >= _lower_bound(df["Open"]))
        & (df["High"] >= _lower_bound(df["Close"]))
    )


def low_is_min_mask(df: pd.DataFrame) -> pd.Series:
    # Low should be <= High, Open, and Close (within float tolerance)
    return (
        (df["Low"] <= _upper_bound(df["High"]))
        & (df["Low"] <= _upper_bound(df["Open"]))
        & (df["Low"] <= _upper_bound(df["Close"]))
    )


def ohlc_consistency_mask(df: pd.DataFrame) -> pd.Series:
    """Row-level version of the schema's OHLC checks, for SALVAGE.

    Vendors do ship single internally-inconsistent bars: 2026-07-18 RBLX came
    back with Open=49.46 above High=40.0 (the gap-down session of a guidance
    shock — exactly the bar that matters most). Frame-level validation then
    rejected all 125 rows, and because the bad bar stays inside the 6-month
    fetch window, every later collection re-failed the same way: RBLX/EC wrote
    no yfinance rows for 10 straight sessions while the desk priced RBLX 24%
    off. Callers use this mask to drop ONLY the offending bars before
    validating; the checks below reuse it so the two can never drift.
    """
    return high_is_max_mask(df) & low_is_min_mask(df)


class PriceHistorySchema(pa.DataFrameModel):
    Open: Series[float] = pa.Field(ge=0)
    High: Series[float] = pa.Field(ge=0)
    Low: Series[float] = pa.Field(ge=0)
    Close: Series[float] = pa.Field(ge=0)
    Volume: Series[int] = pa.Field(ge=0, coerce=True)

    @pa.dataframe_check
    def high_is_max(cls, df: pd.DataFrame) -> Series[bool]:
        return high_is_max_mask(df)

    @pa.dataframe_check
    def low_is_min(cls, df: pd.DataFrame) -> Series[bool]:
        return low_is_min_mask(df)
