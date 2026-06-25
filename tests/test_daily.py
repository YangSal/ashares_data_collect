"""a_share_daily.py 测试（不依赖 xtquant 或数据库）。"""

import pandas as pd

from data_collect.jobs.a_share_daily import _normalize_daily_df


def test_normalize_empty():
    result = _normalize_daily_df(pd.DataFrame(), "000001.SZ", "20260408")
    assert result.empty


def test_normalize_daily():
    raw = pd.DataFrame({
        "time": [1775577600000],  # 2026-04-08 00:00:00 UTC
        "open": [11.0],
        "high": [11.5],
        "low": [10.8],
        "close": [11.2],
        "volume": [100000],
        "amount": [1100000.0],
    })
    result = _normalize_daily_df(raw, "000001.SZ", "20260408")
    assert not result.empty
    assert "stock_code" in result.columns
    assert "trade_date" in result.columns
    assert "time" not in result.columns
    assert result["stock_code"].iloc[0] == "000001.SZ"
