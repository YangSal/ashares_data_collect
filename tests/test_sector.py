"""a_share_sector.py 测试（不依赖 xtquant 或数据库）。"""

from data_collect.jobs.a_share_sector import _SECTOR_PREFIXES, _get_filtered_sectors


def test_sector_prefixes_defined():
    assert "GICS1" in _SECTOR_PREFIXES
    assert "THY1" in _SECTOR_PREFIXES
    assert "TGN" in _SECTOR_PREFIXES
    assert "TFG" in _SECTOR_PREFIXES


def test_filtered_sectors_excludes_base_market(monkeypatch):
    class FakeXtdata:
        def download_sector_data(self): pass
        def get_sector_list(self):
            return ["沪深A股", "GICS1信息技术", "THY1银行", "TGN人工智能", "500SW1银行"]

    monkeypatch.setattr("data_collect.jobs.a_share_sector.require_xtdata", lambda: FakeXtdata())
    result = _get_filtered_sectors()
    assert "沪深A股" not in result
    assert "500SW1银行" not in result
    assert "GICS1信息技术" in result
    assert "THY1银行" in result
    assert "TGN人工智能" in result
