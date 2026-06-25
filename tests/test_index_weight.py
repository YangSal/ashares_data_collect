"""a_share_index_weight.py 测试（不依赖 xtquant 或数据库）。"""

from data_collect.jobs.a_share_index_weight import INDEX_CODES, _fetch_all_weights


def test_index_codes_defined():
    assert "000300.SH" in INDEX_CODES  # 沪深300
    assert "000905.SH" in INDEX_CODES  # 中证500
    assert "000016.SH" in INDEX_CODES  # 上证50
    assert len(INDEX_CODES) >= 5


def test_fetch_weights(monkeypatch):
    class FakeXtdata:
        def download_index_weight(self): pass
        def get_index_weight(self, idx):
            if idx == "000300.SH":
                return {"000001.SZ": 0.85, "600519.SH": 3.2}
            return {}

    monkeypatch.setattr("data_collect.jobs.a_share_index_weight.require_xtdata", lambda: FakeXtdata())
    result = _fetch_all_weights()
    assert "000300.SH" in result
    assert result["000300.SH"]["000001.SZ"] == 0.85
