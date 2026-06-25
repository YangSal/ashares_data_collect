"""clean_qmt_cache.py 测试（纯文件操作，不依赖 QMT）。"""

from pathlib import Path

import pytest

from data_collect.jobs import clean_qmt_cache as C


def _setup(tmp_path, monkeypatch, make_datadir=True):
    """造一个 QMT 安装目录，datadir 下放两个文件（共 3000 字节）。"""
    install = tmp_path / "QMT"
    dd = install / "userdata_mini" / "datadir"
    if make_datadir:
        (dd / "SH" / "1d").mkdir(parents=True)
        (dd / "SH" / "1d" / "a.dat").write_bytes(b"x" * 1000)
        (dd / "SZ").mkdir(parents=True)
        (dd / "SZ" / "b.dat").write_bytes(b"y" * 2000)
    monkeypatch.setattr(
        "data_collect.jobs.clean_qmt_cache.get_qmt_config",
        lambda: {"install_dir": str(install)},
    )
    return install, dd


class TestCleanCache:
    def test_clears_contents_keeps_datadir(self, tmp_path, monkeypatch):
        _install, dd = _setup(tmp_path, monkeypatch)
        freed, deleted, skipped = C.clean_cache()
        assert (freed, deleted, skipped) == (3000, 2, 0)
        assert dd.exists()                       # datadir 本身保留
        assert list(dd.rglob("*")) == []         # 内容（文件+子目录）清空

    def test_dry_run_no_delete(self, tmp_path, monkeypatch):
        _install, dd = _setup(tmp_path, monkeypatch)
        freed, deleted, skipped = C.clean_cache(dry_run=True)
        assert (freed, deleted) == (3000, 2)
        assert (dd / "SZ" / "b.dat").exists()    # 未实际删除

    def test_missing_datadir(self, tmp_path, monkeypatch):
        _install, _dd = _setup(tmp_path, monkeypatch, make_datadir=False)
        assert C.clean_cache() == (0, 0, 0)
        assert "不存在" in C.run()

    def test_skips_locked_file(self, tmp_path, monkeypatch):
        _install, dd = _setup(tmp_path, monkeypatch)
        real_unlink = Path.unlink

        def fake_unlink(self, *a, **k):
            if self.name == "b.dat":
                raise PermissionError("file in use")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", fake_unlink)
        _freed, deleted, skipped = C.clean_cache()
        assert deleted == 1 and skipped == 1     # a.dat 删, b.dat 跳过
        assert (dd / "SZ" / "b.dat").exists()

    def test_run_message(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        msg = C.run()
        assert "释放" in msg and "删除 2 文件" in msg

    def test_no_install_dir_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "data_collect.jobs.clean_qmt_cache.get_qmt_config", lambda: {}
        )
        with pytest.raises(RuntimeError):
            C.clean_cache()
