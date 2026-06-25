"""a_share_tick.py 测试（不依赖 xtquant 或网络）。

新存储方案：按交易日打包为单文件 base/YYYY/MM/DD.parquet（含全部股票）。
"""

import pandas as pd
import pytest

from data_collect.jobs.a_share_tick import (
    _expand_list_columns,
    _normalize_tick_df,
    _ensure_storage_ready,
    _day_file_path,
    _empty_marker_path,
    _day_done,
    _collect_one_day,
    read_tick,
    migrate_per_stock_to_packed,
)


def _make_fake_tick_df(n=3):
    """构造模拟 xtdata tick 返回的 DataFrame。"""
    return pd.DataFrame({
        "time": [1775611800000 + i * 3000 for i in range(n)],
        "lastPrice": [11.1 + i * 0.01 for i in range(n)],
        "open": [11.0] * n,
        "high": [11.2] * n,
        "low": [10.9] * n,
        "lastClose": [11.0] * n,
        "amount": [1000000.0 * (i + 1) for i in range(n)],
        "volume": [10000 * (i + 1) for i in range(n)],
        "pvolume": [10000 * (i + 1) for i in range(n)],
        "tickvol": [100 * (i + 1) for i in range(n)],
        "stockStatus": [0] * n,
        "openInt": [0] * n,
        "lastSettlementPrice": [0.0] * n,
        "askPrice": [[11.1 + j * 0.01 for j in range(5)] for _ in range(n)],
        "bidPrice": [[11.0 - j * 0.01 for j in range(5)] for _ in range(n)],
        "askVol": [[100 + j * 10 for j in range(5)] for _ in range(n)],
        "bidVol": [[200 + j * 10 for j in range(5)] for _ in range(n)],
        "settlementPrice": [0.0] * n,
        "transactionNum": [50 * (i + 1) for i in range(n)],
        "pe": [8.5] * n,
    })


class TestExpandListColumns:
    def test_expands_list_cols(self):
        df = pd.DataFrame({
            "askPrice": [[1.0, 2.0, 3.0, 4.0, 5.0]],
            "bidVol": [[10, 20, 30, 40, 50]],
            "open": [11.0],
        })
        result = _expand_list_columns(df)
        assert "askPrice" not in result.columns
        assert "ask_price_1" in result.columns
        assert "ask_price_5" in result.columns
        assert "bid_vol_1" in result.columns
        assert result["ask_price_3"].iloc[0] == 3.0
        assert result["open"].iloc[0] == 11.0

    def test_no_list_cols(self):
        df = pd.DataFrame({"open": [1.0], "close": [2.0]})
        result = _expand_list_columns(df)
        assert list(result.columns) == ["open", "close"]


class TestNormalizeTickDf:
    def test_full_normalization(self):
        raw = _make_fake_tick_df(2)
        result = _normalize_tick_df(raw, "000001.SZ")
        assert "time" not in result.columns
        assert "datetime" in result.columns
        assert result["stock_code"].iloc[0] == "000001.SZ"
        assert "ask_price_1" in result.columns
        assert "bid_vol_5" in result.columns
        assert "last_price" in result.columns
        assert "transaction_num" in result.columns

    def test_empty_df(self):
        result = _normalize_tick_df(pd.DataFrame(), "000001.SZ")
        assert result.empty


class TestEnsureStorageReady:
    def test_ok_creates_dir(self, tmp_path, monkeypatch):
        target = tmp_path / "tick_root"
        monkeypatch.setattr(
            "data_collect.jobs.a_share_tick.get_tick_config",
            lambda: {"base_dir": str(target), "compression": "zstd"},
        )
        base = _ensure_storage_ready()
        assert base.exists()
        assert not (base / ".storage_selftest.parquet").exists()

    def test_raises_on_unwritable_path(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        monkeypatch.setattr(
            "data_collect.jobs.a_share_tick.get_tick_config",
            lambda: {"base_dir": str(blocker / "sub"), "compression": "zstd"},
        )
        with pytest.raises(RuntimeError, match="Tick存储不可用"):
            _ensure_storage_ready()


class TestDayPaths:
    def test_day_file_path(self, monkeypatch):
        monkeypatch.setattr(
            "data_collect.jobs.a_share_tick.get_tick_config",
            lambda: {"base_dir": "/data/tick"},
        )
        p = _day_file_path("20260408")
        assert str(p).replace("\\", "/") == "/data/tick/2026/04/08.parquet"

    def test_empty_marker_path(self, monkeypatch):
        monkeypatch.setattr(
            "data_collect.jobs.a_share_tick.get_tick_config",
            lambda: {"base_dir": "/data/tick"},
        )
        p = _empty_marker_path("20260408")
        assert str(p).replace("\\", "/") == "/data/tick/2026/04/08.empty"


def _patch_collect(monkeypatch, tmp_path, data: dict):
    """data: {code: raw_tick_df 或 None(无数据)}。打桩 xtdata / 代码列表 / 配置。"""
    class FakeXtdata:
        def get_market_data_ex(self, **kw):
            code = kw["stock_list"][0]
            df = data.get(code)
            return {code: (df.copy() if df is not None else pd.DataFrame())}

    monkeypatch.setattr("data_collect.jobs.a_share_tick.require_xtdata", lambda: FakeXtdata())
    monkeypatch.setattr("data_collect.jobs.a_share_tick.download_history_with_retry", lambda *a, **k: None)
    monkeypatch.setattr("data_collect.jobs.a_share_tick.get_a_share_codes", lambda: list(data.keys()))
    monkeypatch.setattr(
        "data_collect.jobs.a_share_tick.get_tick_config",
        lambda: {"base_dir": str(tmp_path), "compression": "zstd"},
    )


class TestCollectOneDay:
    def test_writes_packed_day_file(self, tmp_path, monkeypatch):
        data = {"000001.SZ": _make_fake_tick_df(5), "600519.SH": _make_fake_tick_df(3)}
        _patch_collect(monkeypatch, tmp_path, data)

        r = _collect_one_day("20260408")
        assert r.status == "written"
        assert r.with_data == 2
        assert r.rows == 8

        f = _day_file_path("20260408")
        assert f.exists()
        df = pd.read_parquet(f)
        assert len(df) == 8
        assert set(df["stock_code"].unique()) == {"000001.SZ", "600519.SH"}
        assert "ask_price_1" in df.columns
        # 临时文件应已被原子重命名清掉
        assert not (f.parent / (f.name + ".tmp")).exists()

    def test_idempotent_skip(self, tmp_path, monkeypatch):
        data = {"000001.SZ": _make_fake_tick_df(5)}
        _patch_collect(monkeypatch, tmp_path, data)
        assert _collect_one_day("20260408").status == "written"
        assert _day_done("20260408")
        assert _collect_one_day("20260408").status == "skipped"

    def test_unavailable_writes_marker(self, tmp_path, monkeypatch):
        data = {"000001.SZ": None, "600519.SH": None}
        _patch_collect(monkeypatch, tmp_path, data)
        r = _collect_one_day("20260408")
        assert r.status == "unavailable"
        assert not _day_file_path("20260408").exists()
        assert _empty_marker_path("20260408").exists()
        assert _day_done("20260408")  # 标记也算"已处理"，verify 不再重试


class TestReadTick:
    def test_read_and_filter(self, tmp_path, monkeypatch):
        data = {"000001.SZ": _make_fake_tick_df(5), "600519.SH": _make_fake_tick_df(3)}
        _patch_collect(monkeypatch, tmp_path, data)
        _collect_one_day("20260408")

        all_df = read_tick("20260408")
        assert len(all_df) == 8
        one = read_tick("20260408", "600519.SH")
        assert len(one) == 3
        assert set(one["stock_code"].unique()) == {"600519.SH"}
        assert read_tick("20260409").empty  # 无文件


def _write_legacy(tmp_path, trade_date, code, n):
    """写一个旧版每股文件 base/YYYY/MM/DD/{code}.parquet。"""
    dt = pd.to_datetime(trade_date)
    d = _normalize_tick_df(_make_fake_tick_df(n), code)
    p = tmp_path / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}" / f"{code}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    d.to_parquet(p, index=False)
    return p


class TestMigrate:
    def _patch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "data_collect.jobs.a_share_tick.get_tick_config",
            lambda: {"base_dir": str(tmp_path), "compression": "zstd"},
        )

    def test_packs_legacy_dirs(self, tmp_path, monkeypatch):
        self._patch(tmp_path, monkeypatch)
        _write_legacy(tmp_path, "20260408", "000001.SZ", 5)
        _write_legacy(tmp_path, "20260408", "600519.SH", 3)

        msg = migrate_per_stock_to_packed()
        assert "打包 1" in msg

        packed = _day_file_path("20260408")
        assert packed.exists()
        df = pd.read_parquet(packed)
        assert len(df) == 8
        assert set(df["stock_code"].unique()) == {"000001.SZ", "600519.SH"}
        # delete_old 默认 False，旧目录保留
        assert (tmp_path / "2026" / "04" / "08").exists()

    def test_delete_old_removes_dir(self, tmp_path, monkeypatch):
        self._patch(tmp_path, monkeypatch)
        _write_legacy(tmp_path, "20260408", "000001.SZ", 5)
        migrate_per_stock_to_packed(delete_old=True)
        assert _day_file_path("20260408").exists()
        assert not (tmp_path / "2026" / "04" / "08").exists()

    def test_idempotent_skip_when_packed_exists(self, tmp_path, monkeypatch):
        self._patch(tmp_path, monkeypatch)
        _write_legacy(tmp_path, "20260408", "000001.SZ", 5)
        migrate_per_stock_to_packed()           # 首次打包
        msg = migrate_per_stock_to_packed()     # 再次：应跳过
        assert "跳过 1" in msg

    def test_dry_run_no_write(self, tmp_path, monkeypatch):
        self._patch(tmp_path, monkeypatch)
        _write_legacy(tmp_path, "20260408", "000001.SZ", 5)
        msg = migrate_per_stock_to_packed(dry_run=True)
        assert "dry-run" in msg
        assert not _day_file_path("20260408").exists()
