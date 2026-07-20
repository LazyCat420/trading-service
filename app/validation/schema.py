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


class PriceHistorySchema(pa.DataFrameModel):
    Open: Series[float] = pa.Field(ge=0)
    High: Series[float] = pa.Field(ge=0)
    Low: Series[float] = pa.Field(ge=0)
    Close: Series[float] = pa.Field(ge=0)
    Volume: Series[int] = pa.Field(ge=0, coerce=True)

    @pa.dataframe_check
    def high_is_max(cls, df: pd.DataFrame) -> Series[bool]:
        # High should be >= Low, Open, and Close (within float tolerance)
        return (
            (df["High"] >= _lower_bound(df["Low"]))
            & (df["High"] >= _lower_bound(df["Open"]))
            & (df["High"] >= _lower_bound(df["Close"]))
        )

    @pa.dataframe_check
    def low_is_min(cls, df: pd.DataFrame) -> Series[bool]:
        # Low should be <= High, Open, and Close (within float tolerance)
        return (
            (df["Low"] <= _upper_bound(df["High"]))
            & (df["Low"] <= _upper_bound(df["Open"]))
            & (df["Low"] <= _upper_bound(df["Close"]))
        )
