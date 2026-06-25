"""
A股日线K线采集任务

通过 xtquant 获取日线数据，写入 daily_kline 表。
支持增量采集（单日）和全量补历史（日期范围）。
分批下载（50只/批），每批下载后立即批量读取+写入DB。
"""

from __future__ import annotations

import logging
from typing import List

import pandas as pd
import sys
from tqdm import tqdm

from data_collect.utils.date_utils import is_market_day, date_range
from data_collect.utils.db import save_to_postgres, get_dates_with_data, get_connection
from data_collect.utils.df_utils import normalize_trade_date
from data_collect.utils.notify import send_dingtalk
from data_collect.utils.xtquant_utils import require_xtdata, get_a_share_codes, download_history_with_retry

logger = logging.getLogger(__name__)

TABLE_NAME = "daily_kline"
_DOWNLOAD_BATCH_SIZE = 50


def _normalize_daily_df(df: pd.DataFrame, stock_code: str, trade_date: str) -> pd.DataFrame:
    """标准化单只股票日线 DataFrame。"""
    if df.empty:
        return df

    df = df.copy()
    if "time" in df.columns and pd.api.types.is_numeric_dtype(df["time"]):
        df["trade_date"] = (
            pd.to_datetime(df["time"], unit="ms") + pd.Timedelta(hours=8)
        ).dt.date
    df = df.drop(columns=["time"], errors="ignore")
    df["stock_code"] = stock_code

    target = pd.to_datetime(trade_date).date()
    df = df[df["trade_date"] == target]

    cols = ["stock_code", "trade_date", "open", "high", "low", "close", "volume", "amount"]
    return df[[c for c in cols if c in df.columns]]


def _download_and_read_batch(batch: List[str], trade_date: str) -> List[pd.DataFrame]:
    """下载一批股票日线并批量读取。"""
    xtdata = require_xtdata()
    download_history_with_retry(batch, "1d", trade_date, trade_date)

    raw = xtdata.get_market_data_ex(
        field_list=["time", "open", "high", "low", "close", "volume", "amount"],
        stock_list=batch, period="1d",
        start_time=trade_date, end_time=trade_date,
        count=-1, dividend_type="none", fill_data=False,
    )

    chunks = []
    for code in batch:
        df = raw.get(code)
        if df is None or df.empty:
            continue
        try:
            one = _normalize_daily_df(df, code, trade_date)
            if not one.empty:
                chunks.append(one)
        except Exception as exc:
            logger.debug(f"日线标准化失败 {code}: {exc}")

    return chunks


def _download_and_save_batch(batch: List[str], trade_date: str) -> tuple[int, int]:
    """下载一批股票日线并立即写入DB。"""
    chunks = _download_and_read_batch(batch, trade_date)
    if not chunks:
        return 0, 0
    merged = pd.concat(chunks, ignore_index=True)
    return save_to_postgres(merged, pre_aligned_df=merged, table_name=TABLE_NAME)


def _get_missing_codes(trade_date: str, codes: list) -> list:
    """查 DB 找出当天缺失数据的股票代码。"""
    with get_connection() as conn:
        existing = pd.read_sql(
            "SELECT DISTINCT stock_code FROM daily_kline WHERE trade_date = %s",
            conn, params=[trade_date],
        )
    existing_set = set(existing["stock_code"])
    return [c for c in codes if c not in existing_set]


def _collect_one_day(trade_date: str, limit_stocks: int | None = None,
                     codes: list | None = None, pbar: tqdm | None = None) -> tuple[int, int]:
    """采集单天全部股票日线。先查 DB 剔除已有股票，只下载缺失的。"""
    if codes is None:
        codes = get_a_share_codes()
        if not codes:
            raise RuntimeError("未获取到A股股票列表")
        if limit_stocks is not None and limit_stocks > 0:
            codes = codes[:limit_stocks]

    # 查 DB 找出缺失的股票，只下载缺失的
    missing_codes = _get_missing_codes(trade_date, codes)
    if not missing_codes:
        if pbar:
            pbar.update(len(codes))
            pbar.set_postfix_str("已完整")
        return 0, 0

    if pbar:
        pbar.total = len(missing_codes)
        pbar.set_postfix_str(f"缺{len(missing_codes)}/{len(codes)}")
        pbar.refresh()

    total_tried, total_inserted = 0, 0
    total_batches = (len(missing_codes) + _DOWNLOAD_BATCH_SIZE - 1) // _DOWNLOAD_BATCH_SIZE

    for i in range(0, len(missing_codes), _DOWNLOAD_BATCH_SIZE):
        batch = missing_codes[i : i + _DOWNLOAD_BATCH_SIZE]
        batch_no = i // _DOWNLOAD_BATCH_SIZE + 1

        if pbar:
            pbar.set_postfix_str(f"缺{len(missing_codes)}/{len(codes)} batch={batch_no}/{total_batches}")

        tried, inserted = _download_and_save_batch(batch, trade_date)
        total_tried += tried
        total_inserted += inserted

        if pbar:
            pbar.update(len(batch))

    return total_tried, total_inserted


# ======== Pipeline 标准接口 ========

def _get_codes(limit_stocks: int | None = None) -> list:
    codes = get_a_share_codes()
    if not codes:
        raise RuntimeError("未获取到A股股票列表")
    if limit_stocks is not None and limit_stocks > 0:
        codes = codes[:limit_stocks]
    return codes


def run(run_date: str, **kwargs) -> str:
    trade_date = normalize_trade_date(run_date)
    limit_stocks = kwargs.get("limit_stocks")

    if not is_market_day(trade_date):
        return f"{trade_date} 非交易日，日线任务跳过。"

    codes = _get_codes(limit_stocks)
    with tqdm(total=len(codes), desc=f"日线({trade_date})", unit="股", file=sys.stdout) as pbar:
        tried, inserted = _collect_one_day(trade_date, codes=codes, pbar=pbar)
    return f"{trade_date} 日线任务完成，写入 {tried}/{inserted} 条。"


def run_backfill(start_date: str, end_date: str, limit_stocks: int | None = None) -> str:
    trading_days = date_range(start_date, end_date)
    total_days = len(trading_days)
    codes = _get_codes(limit_stocks)
    total_tried, total_inserted = 0, 0

    for day_idx, trade_date in enumerate(trading_days, 1):
        with tqdm(total=len(codes), desc=f"{day_idx}/{total_days} {trade_date}", unit="股", file=sys.stdout) as pbar:
            tried, inserted = _collect_one_day(trade_date, codes=codes, pbar=pbar)
            total_tried += tried
            total_inserted += inserted

    message = (
        f"日线补历史完成 ({start_date}~{end_date})，"
        f"共 {total_days} 个交易日，"
        f"写入 {total_tried}/{total_inserted} 条。"
    )
    send_dingtalk(message)
    return message


def run_verify(start_date: str, end_date: str, **kwargs) -> str:
    """查漏补缺：逐日检查日线数据完整性（日期+股票级别），自动补缺。"""
    limit_stocks = kwargs.get("limit_stocks")
    trading_days = date_range(start_date, end_date)
    total_days = len(trading_days)
    codes = _get_codes(limit_stocks)
    total_tried, total_inserted = 0, 0
    days_fixed = 0

    for day_idx, trade_date in enumerate(trading_days, 1):
        missing_codes = _get_missing_codes(trade_date, codes)
        if not missing_codes:
            continue

        days_fixed += 1
        with tqdm(total=len(missing_codes), desc=f"补缺 {day_idx}/{total_days} {trade_date} 缺{len(missing_codes)}", unit="股", file=sys.stdout) as pbar:
            tried, inserted = _collect_one_day(trade_date, codes=missing_codes, pbar=pbar)
            total_tried += tried
            total_inserted += inserted

    if days_fixed == 0:
        return f"日线检查完成，{total_days} 个交易日数据完整（共{len(codes)}只）。"

    return (
        f"日线补缺完成，{days_fixed}/{total_days} 天有缺失，"
        f"补充 {total_tried}/{total_inserted} 条。"
    )


def run_evaluate(start_date: str, end_date: str, **kwargs) -> str:
    """评估数据缺失：对比应有股票和实际数据，输出缺失明细 CSV。"""
    from data_collect.utils.db import get_connection

    trading_days = date_range(start_date, end_date)
    codes = _get_codes()

    # 查询实际有数据的 (date, code) 组合
    with get_connection() as conn:
        df_existing = pd.read_sql(
            "SELECT DISTINCT trade_date, stock_code FROM daily_kline "
            "WHERE trade_date >= %s AND trade_date <= %s",
            conn, params=[start_date, end_date],
        )

    existing_set = set()
    for _, row in df_existing.iterrows():
        existing_set.add((str(row["trade_date"]), row["stock_code"]))

    # 对比找缺失
    missing_rows = []
    for d in trading_days:
        d_date = pd.to_datetime(d).strftime("%Y-%m-%d")
        for code in codes:
            if (d_date, code) not in existing_set:
                missing_rows.append({"trade_date": d, "stock_code": code})

    if not missing_rows:
        return f"日线评估完成 ({start_date}~{end_date})，{len(trading_days)} 天 × {len(codes)} 只，数据完整。"

    df_missing = pd.DataFrame(missing_rows)
    output = f"evaluate_daily_{start_date}_{end_date}.csv"
    df_missing.to_csv(output, index=False, encoding="utf-8-sig")

    # 统计摘要
    missing_dates = df_missing["trade_date"].nunique()
    missing_stocks = df_missing["stock_code"].nunique()

    return (
        f"日线评估完成 ({start_date}~{end_date})，"
        f"缺失 {len(df_missing)} 条 ({missing_dates} 天 × {missing_stocks} 只股票)，"
        f"明细已输出: {output}"
    )
