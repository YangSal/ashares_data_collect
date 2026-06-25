import tempfile

import pandas as pd

from data_collect.utils.export import export_stock_csvs


def _enable_csv(monkeypatch):
    """让测试中 enable_csv=True。"""
    monkeypatch.setattr(
        "data_collect.utils.export.get_export_config",
        lambda: {"enable_csv": True, "base_dir": "tempdata"},
    )


def test_export_stock_csvs(monkeypatch):
    _enable_csv(monkeypatch)
    sample = pd.DataFrame(
        {
            "stock_code": ["000001.SZ", "000001.SZ", "600000.SH"],
            "bar_time": [
                pd.Timestamp("2026-02-20 09:31:00"),
                pd.Timestamp("2026-02-20 09:32:00"),
                pd.Timestamp("2026-02-20 09:31:00"),
            ],
            "close": [10.1, 10.2, 9.8],
        }
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        export_dir, count = export_stock_csvs(sample, "20260220", base_dir=tmpdir)
        assert count == 2
        assert (export_dir / "000001.SZ.csv").exists()
        assert (export_dir / "600000.SH.csv").exists()


def test_export_empty_dataframe(monkeypatch):
    _enable_csv(monkeypatch)
    with tempfile.TemporaryDirectory() as tmpdir:
        export_dir, count = export_stock_csvs(pd.DataFrame(), "20260220", base_dir=tmpdir)
        assert count == 0


def test_export_disabled_by_default():
    """enable_csv=false 时应跳过导出。"""
    sample = pd.DataFrame({"stock_code": ["000001.SZ"], "close": [10.1]})
    export_dir, count = export_stock_csvs(sample, "20260220")
    assert export_dir is None
    assert count == 0
