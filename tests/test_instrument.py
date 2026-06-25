"""a_share_instrument.py 测试（不依赖 xtquant 或数据库）。"""

from data_collect.jobs.a_share_instrument import (
    _DAILY_CHANGING_FIELDS,
    _fetch_instrument,
    _pg_type_for_value,
)


def test_fetch_excludes_daily_fields(monkeypatch):
    fake_detail = {
        "ExchangeID": "SZ",
        "InstrumentName": "平安银行",
        "PreClose": 11.22,
        "UpStopPrice": 12.34,
        "DownStopPrice": 10.1,
        "TradingDay": "20260409",
        "IsTrading": True,
        "InstrumentStatus": 0,
        "FloatVolume": 1.7e10,
    }

    class FakeXtdata:
        def get_instrument_detail(self, code, iscomplete=False):
            return dict(fake_detail)

    monkeypatch.setattr(
        "data_collect.jobs.a_share_instrument.require_xtdata",
        lambda: FakeXtdata(),
    )
    result = _fetch_instrument("000001.SZ")
    assert result is not None
    assert "PreClose" not in result
    assert "UpStopPrice" not in result
    assert "TradingDay" not in result
    assert "IsTrading" not in result
    assert "ExchangeID" in result
    assert "InstrumentName" in result
    assert "FloatVolume" in result


def test_fetch_returns_none_on_empty(monkeypatch):
    class FakeXtdata:
        def get_instrument_detail(self, code, iscomplete=False):
            return {}

    monkeypatch.setattr(
        "data_collect.jobs.a_share_instrument.require_xtdata",
        lambda: FakeXtdata(),
    )
    result = _fetch_instrument("000001.SZ")
    assert result is None


def test_pg_type_for_value():
    assert _pg_type_for_value(True) == "BOOLEAN"
    assert _pg_type_for_value(100) == "BIGINT"
    assert _pg_type_for_value(11.22) == "DOUBLE PRECISION"
    assert _pg_type_for_value("SZ") == "TEXT"


def test_daily_changing_fields_defined():
    assert "PreClose" in _DAILY_CHANGING_FIELDS
    assert "UpStopPrice" in _DAILY_CHANGING_FIELDS
    assert "ExchangeID" not in _DAILY_CHANGING_FIELDS
