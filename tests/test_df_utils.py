import pandas as pd

from data_collect.utils.df_utils import (
    align_dataframe_to_table,
    align_dataframe_by_position,
    normalize_trade_date,
)


def test_align_dataframe_to_table():
    sample = pd.DataFrame(
        {
            "stock_code": ["000001.SZ"],
            "bar_time": [pd.Timestamp("2026-02-20 09:31:00")],
            "close": [10.1],
            "extra_col": [123],
        }
    )
    aligned = align_dataframe_to_table(sample, ["stock_code", "bar_time", "close"])
    assert list(aligned.columns) == ["stock_code", "bar_time", "close"]
    assert "extra_col" not in aligned.columns


def test_align_dataframe_by_position():
    sample = pd.DataFrame(
        {
            "trade_date": ["20260220"],
            "bar_time": [pd.Timestamp("2026-02-20 09:31:00")],
            "stock_code": ["000001.SZ"],
            "open": [10.0],
            "high": [10.2],
            "low": [9.9],
            "close": [10.1],
            "volume": [1000],
            "amount": [10000],
        }
    )
    schema = [
        ("日期", "date", None),
        ("时间", "time without time zone", None),
        ("代码", "character varying", 8),
    ]
    aligned = align_dataframe_by_position(sample, schema)
    assert list(aligned.columns) == ["日期", "时间", "代码"]
    assert str(aligned["代码"].iloc[0]) == "sz000001"


def test_normalize_trade_date_yyyymmdd():
    assert normalize_trade_date("20260220") == "20260220"


def test_normalize_trade_date_with_dash():
    assert normalize_trade_date("2026-02-20") == "20260220"
