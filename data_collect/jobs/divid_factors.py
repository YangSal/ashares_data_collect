"""
A股复权因子采集任务

通过 xtquant.get_divid_factors() 获取除权除息数据，写入 divid_factors 表。
支持增量采集（单日）和全量补历史（日期范围）。
"""

from __future__ import annotations

import logging
import sys
from typing import List, Tuple

import pandas as pd
from tqdm import tqdm

from data_collect.config import get_export_config
from data_collect.utils.date_utils import is_market_day
from data_collect.utils.db import save_to_postgres
from data_collect.utils.df_utils import normalize_trade_date
from data_collect.utils.notify import send_dingtalk
from data_collect.utils.retry import retry_xtquant
from data_collect.utils.xtquant_utils import require_xtdata, get_a_share_codes


TABLE_NAME = "divid_factors"


@retry_xtquant
def fetch_divid_factors(
    stock_code: str,
    start_time: str = "",
    end_time: str = "",
) -> pd.DataFrame:
    """获取单只股票的除权因子数据。"""
    xtdata = require_xtdata()
    raw = xtdata.get_divid_factors(stock_code, start_time=start_time, end_time=end_time)

    if raw is None or (isinstance(raw, pd.DataFrame) and raw.empty):
        return pd.DataFrame()

    if not isinstance(raw, pd.DataFrame):
        return pd.DataFrame()

    df = raw.copy()
    df["stock_code"] = stock_code

    # index 通常是除权日期（YYYYMMDD 或时间戳）
    if df.index.name is None:
        df.index.name = "date"
    df = df.reset_index()

    # 标准化日期列
    date_col = df.columns[0]  # 第一列是日期（reset_index 后）
    df["date"] = pd.to_datetime(df[date_col], errors="coerce")
    if df["date"].isna().all():
        df["date"] = pd.to_datetime(df[date_col], unit="ms", errors="coerce")

    df = df.dropna(subset=["date"])
    if df.empty:
        return df

    df["date"] = df["date"].dt.date

    # 标准化列名（xtquant 返回的列名可能不一致）
    col_map = {}
    for col in df.columns:
        lower = col.lower()
        if lower in ("interest",):
            col_map[col] = "interest"
        elif lower in ("stockbonus", "stock_bonus"):
            col_map[col] = "stock_bonus"
        elif lower in ("stockgift", "stock_gift"):
            col_map[col] = "stock_gift"
        elif lower in ("allotnum", "allot_num"):
            col_map[col] = "allot_num"
        elif lower in ("allotprice", "allot_price"):
            col_map[col] = "allot_price"
        elif lower == "dr":
            col_map[col] = "dr"
    df = df.rename(columns=col_map)

    # 确保所有目标列存在
    for field in ["interest", "stock_bonus", "stock_gift", "allot_num", "allot_price", "dr"]:
        if field not in df.columns:
            df[field] = None

    return df[["stock_code", "date", "interest", "stock_bonus", "stock_gift",
               "allot_num", "allot_price", "dr"]]


def fetch_all_divid_factors(
    start_time: str = "",
    end_time: str = "",
    limit_stocks: int | None = None,
) -> pd.DataFrame:
    """获取全部A股的复权因子。"""
    codes = get_a_share_codes()
    if not codes:
        raise RuntimeError("未获取到A股股票列表")

    if limit_stocks is not None and limit_stocks > 0:
        codes = codes[:limit_stocks]

    chunks: List[pd.DataFrame] = []
    fail_count = 0
    for code in tqdm(codes, desc="复权因子", unit="股", file=sys.stdout):
        try:
            one = fetch_divid_factors(code, start_time=start_time, end_time=end_time)
            if not one.empty:
                chunks.append(one)
        except Exception:
            fail_count += 1
    if fail_count:
        logging.warning(f"复权因子采集 {fail_count} 只股票失败")

    if not chunks:
        return pd.DataFrame(columns=[
            "stock_code", "date", "interest", "stock_bonus", "stock_gift",
            "allot_num", "allot_price", "dr",
        ])

    merged = pd.concat(chunks, ignore_index=True)
    return merged.drop_duplicates(subset=["stock_code", "date"])


def save_divid_factors(df: pd.DataFrame) -> Tuple[int, int]:
    """写入复权因子到数据库。"""
    if df.empty:
        return 0, 0

    table_name = get_export_config().get("divid_factors_table", TABLE_NAME)
    return save_to_postgres(df, pre_aligned_df=df, table_name=table_name)


# ======== Pipeline 标准接口 ========

def run(run_date: str, **kwargs) -> str:
    """每日任务：获取指定日期的复权因子。"""
    trade_date = normalize_trade_date(run_date)
    limit_stocks = kwargs.get("limit_stocks")

    if not is_market_day(trade_date):
        return f"{trade_date} 非交易日，复权因子任务跳过。"

    data = fetch_all_divid_factors(
        start_time=trade_date, end_time=trade_date, limit_stocks=limit_stocks,
    )
    tried, inserted = save_divid_factors(data)
    return f"{trade_date} 复权因子任务完成，写入 {tried}/{inserted} 条。"


def run_backfill(start_date: str, end_date: str, limit_stocks: int | None = None) -> str:
    """全量补历史：获取指定日期范围内所有复权因子。"""
    data = fetch_all_divid_factors(
        start_time=start_date, end_time=end_date, limit_stocks=limit_stocks,
    )
    tried, inserted = save_divid_factors(data)
    message = (
        f"复权因子补历史完成 ({start_date}~{end_date})，"
        f"写入 {tried}/{inserted} 条。"
    )
    send_dingtalk(message)
    return message


def run_verify(start_date: str, end_date: str, **kwargs) -> str:
    """查漏补缺：复权因子按全量范围重新获取（xtquant 不支持按天查询）。"""
    limit_stocks = kwargs.get("limit_stocks")
    data = fetch_all_divid_factors(
        start_time=start_date, end_time=end_date, limit_stocks=limit_stocks,
    )
    tried, inserted = save_divid_factors(data)
    return (
        f"复权因子补缺完成 ({start_date}~{end_date})，"
        f"尝试 {tried}，新增 {inserted} 条。"
    )
