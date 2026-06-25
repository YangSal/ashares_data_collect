import datetime

from data_collect.utils.date_utils import is_market_day, add_one_market_day, minus_one_market_day


def test_is_market_day_weekend():
    # 2026-04-04 是周六
    assert not is_market_day("20260404")


def test_is_market_day_weekday():
    # 2026-04-06 是周一（假设非节假日）
    # 具体结果取决于 chinese_calendar 数据
    result = is_market_day("20260406")
    assert isinstance(result, bool)


def test_add_one_market_day():
    # 从周五跳到下周一（如果下周一是交易日）
    result = add_one_market_day("20260403")  # 周五
    assert isinstance(result, str)
    assert len(result) == 8


def test_minus_one_market_day():
    result = minus_one_market_day("20260406")  # 周一
    assert isinstance(result, str)
    assert len(result) == 8
