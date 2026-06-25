"""
A股分钟K线采集任务

负责：
- 通过 xtquant 获取分钟K线数据
- 归一化 xtdata 返回的数据格式
- 编排完整的采集→对齐→导出→入库流程
"""

from __future__ import annotations

import logging
import sys
from typing import List

import pandas as pd
from tqdm import tqdm

from data_collect.utils.date_utils import is_market_day, date_range
from data_collect.utils.df_utils import normalize_trade_date
from data_collect.utils.db import prepare_db_aligned_data, save_to_postgres
from data_collect.utils.export import export_stock_csvs
from data_collect.utils.notify import send_dingtalk
from data_collect.utils.retry import retry_xtquant
from data_collect.utils.xtquant_utils import require_xtdata, get_a_share_codes, download_history_with_retry


# ======== 数据获取 ========

def normalize_minute_df(raw_obj, stock_code: str) -> pd.DataFrame:
    """
    归一化 xtdata 返回对象为统一 DataFrame，列名：
    stock_code, bar_time, trade_date, open, high, low, close, volume, amount
    """
    if raw_obj is None:
        return pd.DataFrame()

    frame = None
    if isinstance(raw_obj, pd.DataFrame):
        frame = raw_obj
    elif isinstance(raw_obj, dict):
        if stock_code in raw_obj and isinstance(raw_obj[stock_code], pd.DataFrame):
            frame = raw_obj[stock_code]
        else:
            for value in raw_obj.values():
                if isinstance(value, pd.DataFrame):
                    frame = value
                    break

    if frame is None or frame.empty:
        return pd.DataFrame()

    lower_col_map = {column.lower(): column for column in frame.columns}
    for field in ["open", "high", "low", "close", "volume", "amount"]:
        if field in lower_col_map and lower_col_map[field] != field:
            frame[field] = frame[lower_col_map[field]]
        elif field not in lower_col_map:
            frame[field] = pd.NA

    if "time" in lower_col_map:
        raw_time = frame[lower_col_map["time"]]
    elif "datetime" in lower_col_map:
        raw_time = frame[lower_col_map["datetime"]]
    else:
        raw_time = pd.Series(frame.index, index=frame.index)

    if pd.api.types.is_numeric_dtype(raw_time):
        bar_time = pd.to_datetime(raw_time, unit="ms", errors="coerce")
        bar_time = bar_time + pd.Timedelta(hours=8)
    else:
        bar_time = pd.to_datetime(raw_time, errors="coerce")
        if bar_time.isna().all():
            bar_time = pd.to_datetime(raw_time, unit="ms", errors="coerce")
            bar_time = bar_time + pd.Timedelta(hours=8)

    result = pd.DataFrame(
        {
            "stock_code": stock_code,
            "bar_time": bar_time,
            "open": pd.to_numeric(frame["open"], errors="coerce"),
            "high": pd.to_numeric(frame["high"], errors="coerce"),
            "low": pd.to_numeric(frame["low"], errors="coerce"),
            "close": pd.to_numeric(frame["close"], errors="coerce"),
            "volume": pd.to_numeric(frame["volume"], errors="coerce"),
            "amount": pd.to_numeric(frame["amount"], errors="coerce"),
        }
    ).dropna(subset=["bar_time"])

    if result.empty:
        return result

    result["trade_date"] = result["bar_time"].dt.strftime("%Y%m%d")
    return result


@retry_xtquant
def fetch_one_stock_minute(stock_code: str, trade_date: str) -> pd.DataFrame:
    """获取单只股票指定交易日的1分钟K线。"""
    xtdata = require_xtdata()
    params = dict(
        field_list=["time", "open", "high", "low", "close", "volume", "amount"],
        stock_list=[stock_code],
        period="1m",
        start_time=trade_date,
        end_time=trade_date,
        count=-1,
        dividend_type="none",
        fill_data=False,
    )
    raw = xtdata.get_local_data(**params)
    if not raw:
        raw = xtdata.get_market_data_ex(**params)
    frame = normalize_minute_df(raw, stock_code)
    if frame.empty:
        return frame
    return frame[frame["trade_date"] == trade_date]


_DOWNLOAD_BATCH_SIZE = 50


def fetch_all_stocks_minute(trade_date: str, limit_stocks: int | None = None) -> pd.DataFrame:
    """获取全部A股在指定交易日的分钟K线（分批下载+读取）。"""
    codes = get_a_share_codes()
    if not codes:
        raise RuntimeError("未获取到A股股票列表，请检查QMT是否启动并已连接xtdata")

    if limit_stocks is not None and limit_stocks > 0:
        codes = codes[:limit_stocks]

    all_chunks: List[pd.DataFrame] = []
    fail_count = 0

    for i in tqdm(range(0, len(codes), _DOWNLOAD_BATCH_SIZE),
                  desc=f"分钟线({trade_date})", unit="批", file=sys.stdout):
        batch = codes[i : i + _DOWNLOAD_BATCH_SIZE]
        download_history_with_retry(batch, "1m", trade_date, trade_date)

        for code in batch:
            try:
                one = fetch_one_stock_minute(code, trade_date)
                if not one.empty:
                    all_chunks.append(one)
            except Exception:
                fail_count += 1

    if fail_count:
        logging.warning(f"分钟线采集 {fail_count} 只股票失败")

    if not all_chunks:
        return pd.DataFrame(
            columns=[
                "stock_code", "bar_time", "trade_date",
                "open", "high", "low", "close", "volume", "amount",
            ]
        )

    merged = pd.concat(all_chunks, ignore_index=True)
    return merged.drop_duplicates(subset=["stock_code", "bar_time"])


# ======== Pipeline 标准接口 ========

def run(run_date: str, **kwargs) -> str:
    """每日任务：采集指定日期的分钟线→对齐→导出→入库。"""
    trade_date = normalize_trade_date(run_date)
    limit_stocks = kwargs.get("limit_stocks")

    if not is_market_day(trade_date):
        return f"{trade_date} 非交易日，分钟线任务跳过。"

    data = fetch_all_stocks_minute(trade_date, limit_stocks=limit_stocks)
    aligned = prepare_db_aligned_data(data)
    export_dir, file_count = export_stock_csvs(aligned, trade_date)
    tried, inserted = save_to_postgres(data, pre_aligned_df=aligned)
    msg = f"{trade_date} 分钟线任务完成，写入 {tried}/{inserted} 条"
    if export_dir:
        msg += f"，导出 {file_count} 个CSV到 {export_dir.resolve()}"
    return msg + "。"


def run_backfill(start_date: str, end_date: str, limit_stocks: int | None = None) -> str:
    """补历史：逐交易日采集指定范围的分钟线数据。"""
    from data_collect.utils.progress import DualProgress

    trading_days = date_range(start_date, end_date)
    total_tried, total_inserted = 0, 0

    with DualProgress(trading_days) as dp:
        for trade_date in dp.iter_days():
            data = fetch_all_stocks_minute(trade_date, limit_stocks=limit_stocks)
            aligned = prepare_db_aligned_data(data)
            export_stock_csvs(aligned, trade_date)
            tried, inserted = save_to_postgres(data, pre_aligned_df=aligned)
            total_tried += tried
            total_inserted += inserted
            dp.log(f"写入 {tried}/{inserted}")

    message = (
        f"分钟线补历史完成 ({start_date}~{end_date})，"
        f"共 {len(trading_days)} 个交易日，"
        f"写入 {total_tried}/{total_inserted} 条。"
    )
    send_dingtalk(message)
    return message


def run_verify(start_date: str, end_date: str, **kwargs) -> str:
    """查漏补缺：检查日期范围内分钟线数据完整性，自动补缺。"""
    from data_collect.utils.db import get_dates_with_data
    from data_collect.utils.progress import DualProgress
    import pandas as pd_

    limit_stocks = kwargs.get("limit_stocks")
    trading_days = date_range(start_date, end_date)
    existing = get_dates_with_data("minutedata_tdx", "交易日期", start_date, end_date)
    missing = [d for d in trading_days if pd_.to_datetime(d).date() not in existing]

    if not missing:
        return f"分钟线检查完成，{len(trading_days)} 个交易日数据完整。"

    total_tried, total_inserted = 0, 0
    with DualProgress(missing) as dp:
        for d in dp.iter_days():
            data = fetch_all_stocks_minute(d, limit_stocks=limit_stocks)
            aligned = prepare_db_aligned_data(data)
            tried, inserted = save_to_postgres(data, pre_aligned_df=aligned)
            total_tried += tried
            total_inserted += inserted
            dp.log(f"写入 {tried}/{inserted}")

    return (
        f"分钟线补缺完成，{len(missing)}/{len(trading_days)} 天缺失，"
        f"补充 {total_tried}/{total_inserted} 条。"
    )


def run_evaluate(start_date: str, end_date: str, **kwargs) -> str:
    """评估数据缺失：对比应有股票和实际数据，输出缺失明细 CSV。"""
    import pandas as pd_eval
    from data_collect.utils.db import get_connection
    from data_collect.utils.xtquant_utils import get_a_share_codes

    trading_days = date_range(start_date, end_date)
    codes = get_a_share_codes()

    with get_connection() as conn:
        df_existing = pd_eval.read_sql(
            'SELECT DISTINCT "交易日期", "代码" FROM minutedata_tdx '
            'WHERE "交易日期" >= %s AND "交易日期" <= %s',
            conn, params=[start_date, end_date],
        )

    existing_set = set()
    for _, row in df_existing.iterrows():
        existing_set.add((str(row["交易日期"]), row["代码"].strip()))

    # 代码格式转换：xtquant 的 000001.SZ → DB 的 sz000001
    def to_db_code(xt_code):
        num, market = xt_code.split(".")
        return f"{market.lower()}{num}"

    missing_rows = []
    for d in trading_days:
        d_date = pd_eval.to_datetime(d).strftime("%Y-%m-%d")
        for code in codes:
            db_code = to_db_code(code)
            if (d_date, db_code) not in existing_set:
                missing_rows.append({"trade_date": d, "stock_code": code})

    if not missing_rows:
        return f"分钟线评估完成 ({start_date}~{end_date})，数据完整。"

    df_missing = pd_eval.DataFrame(missing_rows)
    output = f"evaluate_minute_{start_date}_{end_date}.csv"
    df_missing.to_csv(output, index=False, encoding="utf-8-sig")

    missing_dates = df_missing["trade_date"].nunique()
    missing_stocks = df_missing["stock_code"].nunique()

    return (
        f"分钟线评估完成 ({start_date}~{end_date})，"
        f"缺失 {len(df_missing)} 条 ({missing_dates} 天 × {missing_stocks} 只)，"
        f"明细已输出: {output}"
    )
