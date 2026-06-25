"""divid_factors.py 测试（不依赖 xtquant 或数据库）。"""

import pandas as pd

from data_collect.jobs.divid_factors import fetch_divid_factors


def test_fetch_divid_factors_returns_empty_on_none(monkeypatch):
    """模拟 xtdata.get_divid_factors 返回 None。"""
    class FakeXtdata:
        def get_divid_factors(self, code, start_time="", end_time=""):
            return None

    monkeypatch.setattr(
        "data_collect.jobs.divid_factors.require_xtdata",
        lambda: FakeXtdata(),
    )
    result = fetch_divid_factors("000001.SZ")
    assert result.empty


def test_fetch_divid_factors_normalizes_columns(monkeypatch):
    """模拟 xtdata 返回有数据的 DataFrame。"""
    fake_data = pd.DataFrame(
        {
            "interest": [0.5],
            "stockBonus": [0.1],
            "stockGift": [0.2],
            "allotNum": [0.0],
            "allotPrice": [0.0],
            "dr": [0.95],
        },
        index=pd.to_datetime(["2026-03-15"]),
    )

    class FakeXtdata:
        def get_divid_factors(self, code, start_time="", end_time=""):
            return fake_data

    monkeypatch.setattr(
        "data_collect.jobs.divid_factors.require_xtdata",
        lambda: FakeXtdata(),
    )
    result = fetch_divid_factors("000001.SZ")
    assert not result.empty
    assert "stock_code" in result.columns
    assert "dr" in result.columns
    assert result["stock_code"].iloc[0] == "000001.SZ"
    expected_cols = {"stock_code", "date", "interest", "stock_bonus", "stock_gift",
                     "allot_num", "allot_price", "dr"}
    assert set(result.columns) == expected_cols
