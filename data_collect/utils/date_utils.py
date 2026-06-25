"""
交易日工具模块

功能：
- 判断是否为交易日
- 交易日加减运算
- 交易日区间生成
- TargetDate 锚定日期类

时间输入可为字符串或 datetime，输出均为字符串（YYYYMMDD）。
"""

import datetime
import logging

import pandas as pd
import chinese_calendar

logger = logging.getLogger(__name__)


def _to_date(date_input) -> datetime.datetime:
    """统一转换为 datetime，无效输入抛 ValueError。"""
    if isinstance(date_input, str):
        return pd.to_datetime(date_input)
    if isinstance(date_input, (datetime.datetime, datetime.date)):
        return date_input
    raise ValueError(f"不支持的日期类型: {type(date_input)}, 值: {date_input}")


_calendar_warning_shown = False


def is_market_day(date_) -> bool:
    """判断是否是交易日。

    chinese_calendar 仅支持 2004-2026 年。
    - 2004 之前：退化为周一至周五判断（历史数据无需精确节假日）
    - 当前年份之后：抛异常提醒更新 chinese_calendar
    """
    global _calendar_warning_shown
    date_ = _to_date(date_)
    if date_.isoweekday() not in (1, 2, 3, 4, 5):
        return False
    try:
        return chinese_calendar.is_workday(date_)
    except (NotImplementedError, KeyError):
        current_year = datetime.datetime.now().year
        if date_.year > current_year:
            raise NotImplementedError(
                f"chinese_calendar 不支持 {date_.year} 年，请更新: "
                f"pip install -U chinese_calendar"
            )
        # 历史日期（2004之前）退化为工作日判断
        if not _calendar_warning_shown:
            logger.info(f"chinese_calendar 不支持 {date_.year} 年，退化为周末判断")
            _calendar_warning_shown = True
        return True


def add_one_market_day(ref_date) -> str:
    """返回 ref_date 下一个交易日期（不判断 ref_date 本身）。"""
    ref_date = _to_date(ref_date)
    res_day = ref_date + datetime.timedelta(days=1)
    while not is_market_day(res_day):
        res_day = res_day + datetime.timedelta(days=1)
    return res_day.strftime('%Y%m%d')


def minus_one_market_day(ref_date) -> str:
    """返回 ref_date 上一个交易日期（不判断 ref_date 本身）。"""
    ref_date = _to_date(ref_date)
    res_day = ref_date - datetime.timedelta(days=1)
    while not is_market_day(res_day):
        res_day = res_day - datetime.timedelta(days=1)
    return res_day.strftime('%Y%m%d')


def add_mark_day(ref_date, n) -> str:
    """锚定日期增加 n 个交易日。n 可为负数。"""
    if not isinstance(n, int):
        raise ValueError(f"n 必须为整数, 得到: {type(n)}")
    ref_date = _to_date(ref_date)
    if n == 0:
        return ref_date.strftime('%Y%m%d')
    step_fn = add_one_market_day if n > 0 else minus_one_market_day
    result = ref_date
    for _ in range(abs(n)):
        result = step_fn(result)
    return result


def date_range(date_start: str = "20220426", date_end: str = "20220507"):
    """生成交易日区间列表。"""
    res_list = []
    if is_market_day(date_start):
        res_list.append(date_start)

    next_day = add_one_market_day(date_start)
    while next_day <= date_end:
        res_list.append(next_day)
        next_day = add_one_market_day(next_day)
    return res_list


class TargetDate:
    def __init__(self, ref_date):
        """用锚定日期初始化。"""
        self._ref_date = _to_date(ref_date)

    @property
    def ref_date(self) -> str:
        return self._ref_date.strftime('%Y%m%d')

    def set_ref_date(self, ref_date):
        self._ref_date = _to_date(ref_date)

    @property
    def is_market_day(self):
        return is_market_day(self.ref_date)

    @staticmethod
    def to_date(date_str_) -> datetime.datetime:
        return pd.to_datetime(date_str_)

    def add_mark_day(self, n) -> str:
        """锚定日期增加 n 个交易日。n 可为负数。"""
        return add_mark_day(self.ref_date, n)


__all__ = ['is_market_day', 'add_one_market_day', 'minus_one_market_day', 'add_mark_day', 'TargetDate', 'date_range']
