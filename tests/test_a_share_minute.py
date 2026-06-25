import pandas as pd

from data_collect.jobs.a_share_minute import normalize_minute_df


def test_normalize_minute_df():
    sample = pd.DataFrame(
        {
            "time": [1708671000000, 1708671060000],
            "open": [10.0, 10.1],
            "high": [10.2, 10.3],
            "low": [9.9, 10.0],
            "close": [10.1, 10.2],
            "volume": [1000, 1200],
            "amount": [10000, 12200],
        }
    )
    normalized = normalize_minute_df(sample, "000001.SZ")
    assert len(normalized) == 2
    assert {"stock_code", "bar_time", "trade_date"}.issubset(normalized.columns)


def test_normalize_minute_df_none():
    result = normalize_minute_df(None, "000001.SZ")
    assert result.empty
