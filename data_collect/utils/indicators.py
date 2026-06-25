import pandas as pd
import numpy as np


def tdx_sma(series: pd.Series, n: int, m: int) -> pd.Series:
    """
    Tongdaxin-style SMA: SMA(X, N, M) = (M*X + (N-M)*SMA_prev) / N
    Initializes with first non-NaN value of series.
    """
    series = series.astype(float)
    result = np.empty(len(series), dtype=float)
    prev = np.nan
    for i, x in enumerate(series.values):
        if np.isnan(x):
            result[i] = np.nan
            continue
        if np.isnan(prev):
            prev = x
        else:
            prev = (m * x + (n - m) * prev) / n
        result[i] = prev
    return pd.Series(result, index=series.index)


def hhv(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(window=n, min_periods=n).max()


def llv(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(window=n, min_periods=n).min()


def wr(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    """Williams %R as defined in the article."""
    highest = hhv(high, n)
    lowest = llv(low, n)
    denom = (highest - lowest)
    wr_value = 100 * (highest - close) / denom
    return wr_value


def rsv(close: pd.Series, high: pd.Series, low: pd.Series, n: int = 9) -> pd.Series:
    lowest = llv(low, n)
    highest = hhv(high, n)
    denom = (highest - lowest)
    rsv_val = (close - lowest) / denom * 100
    return rsv_val


def kd(close: pd.Series, high: pd.Series, low: pd.Series, n: int = 9) -> tuple[pd.Series, pd.Series]:
    """
    KD using RSV(9), then K=SMA(RSV,3,1), D=SMA(K,3,1).
    """
    rsv_val = rsv(close, high, low, n)
    k = tdx_sma(rsv_val, 3, 1)
    d = tdx_sma(k, 3, 1)
    return k, d


def rsi(close: pd.Series, n: int = 9) -> pd.Series:
    diff = close.diff()
    up = diff.clip(lower=0)
    ad = diff.abs()
    up_sma = tdx_sma(up, n, 1)
    ad_sma = tdx_sma(ad, n, 1)
    r = 100 * (up_sma / ad_sma)
    return r


def every_k_over_80(k: pd.Series, days: int = 3) -> pd.Series:
    """True where last `days` K values are all > 80."""
    cond = (k > 80).astype(int)
    all_true = cond.rolling(days, min_periods=days).sum() == days
    return all_true


