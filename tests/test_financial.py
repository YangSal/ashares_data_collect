"""a_share_financial.py 测试（不依赖 xtquant 或数据库）。"""

import pandas as pd
import pytest

from data_collect.jobs.a_share_financial import (
    _normalize_financial_df,
    _pg_type,
    FINANCIAL_TABLES,
)


def _make_balance_df():
    """构造模拟 Balance 表 DataFrame。"""
    return pd.DataFrame({
        "m_timetag": ["20251231", "20250630"],
        "m_anntime": ["20260321", "20250823"],
        "tot_assets": [5e12, 4.8e12],
        "tot_liab": [4.5e12, 4.3e12],
        "total_equity": [5e11, 5e11],
        "m_quarter": [0.0, 0.0],
    })


def _make_holder_df():
    """构造模拟 Top10holder 表 DataFrame。"""
    return pd.DataFrame({
        "declareDate": ["20260321", "20260321"],
        "endDate": ["20251231", "20251231"],
        "quantity": [1e9, 8e8],
        "ratio": [5.15, 4.12],
        "rank": [1.0, 2.0],
        "name": ["中国证券金融", "香港中央结算"],
        "type": ["一般法人", "QFII"],
        "reason": ["未变", "新进"],
        "nature": ["国有股", "境外法人"],
    })


class TestNormalizeFinancialDf:
    def test_balance_normalization(self):
        df = _make_balance_df()
        result = _normalize_financial_df(df, "000001.SZ", "Balance")

        assert "stock_code" in result.columns
        assert "report_date" in result.columns
        assert "announce_date" in result.columns
        assert result["stock_code"].iloc[0] == "000001.SZ"
        # m_timetag/m_anntime 已被转换删除
        assert "m_timetag" not in result.columns
        assert "m_anntime" not in result.columns
        # m_quarter 已被删除
        assert "m_quarter" not in result.columns
        # 数值列保留
        assert "tot_assets" in result.columns

    def test_holder_normalization(self):
        df = _make_holder_df()
        result = _normalize_financial_df(df, "000001.SZ", "Top10holder")

        assert "stock_code" in result.columns
        assert "end_date" in result.columns
        assert "announce_date" in result.columns
        assert "declareDate" not in result.columns
        assert "endDate" not in result.columns
        assert result["name"].iloc[0] == "中国证券金融"

    def test_empty_df(self):
        result = _normalize_financial_df(pd.DataFrame(), "000001.SZ", "Balance")
        assert result.empty

    def test_drops_rows_with_null_pk(self):
        df = pd.DataFrame({
            "m_timetag": ["20251231", None],
            "m_anntime": ["20260321", "20250823"],
            "tot_assets": [5e12, 4.8e12],
        })
        result = _normalize_financial_df(df, "000001.SZ", "Balance")
        assert len(result) == 1


class TestPgType:
    def test_stock_code(self):
        s = pd.Series(["000001.SZ"])
        assert "VARCHAR" in _pg_type(s, "stock_code")

    def test_date_cols(self):
        s = pd.Series([pd.Timestamp("2025-12-31")])
        assert "DATE" in _pg_type(s, "report_date")
        assert "NOT NULL" in _pg_type(s, "report_date")
        assert "NOT NULL" not in _pg_type(s, "announce_date")

    def test_numeric(self):
        s = pd.Series([1.0, 2.0], dtype="float64")
        assert _pg_type(s, "tot_assets") == "DOUBLE PRECISION"

    def test_text(self):
        s = pd.Series(["hello"])
        assert _pg_type(s, "name") == "TEXT"

    def test_rank(self):
        s = pd.Series([1.0])
        assert "SMALLINT" in _pg_type(s, "rank")


class TestFinancialTablesConfig:
    def test_all_tables_have_required_keys(self):
        for xt_table, cfg in FINANCIAL_TABLES.items():
            assert "pg_table" in cfg, f"{xt_table} missing pg_table"
            assert "pk" in cfg, f"{xt_table} missing pk"
            assert "stock_code" in cfg["pk"], f"{xt_table} pk must include stock_code"

    def test_table_count(self):
        assert len(FINANCIAL_TABLES) == 8
