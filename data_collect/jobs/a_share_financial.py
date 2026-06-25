"""
A股财务数据采集任务

通过 xtquant 获取财务报表（资产负债表、利润表、现金流量表）、
每股指标、股本变动、股东户数、十大股东等数据，写入 PostgreSQL。
首次运行自动建表，字段根据 xtquant 返回的 DataFrame 动态生成。
"""

from __future__ import annotations

import logging
import sys
from typing import Dict, List, Tuple

import pandas as pd
from tqdm import tqdm

from data_collect.utils.date_utils import is_market_day
from data_collect.utils.db import get_connection, save_to_postgres, validate_table_name
from data_collect.utils.df_utils import normalize_trade_date
from data_collect.utils.notify import send_dingtalk
from data_collect.utils.retry import retry_xtquant
from data_collect.utils.xtquant_utils import require_xtdata, get_a_share_codes

logger = logging.getLogger(__name__)

# xtquant 表名 → PG 表名 + 主键定义
FINANCIAL_TABLES: Dict[str, dict] = {
    "Balance":         {"pg_table": "fin_balance",           "pk": ["stock_code", "report_date", "announce_date"]},
    "Income":          {"pg_table": "fin_income",            "pk": ["stock_code", "report_date", "announce_date"]},
    "CashFlow":        {"pg_table": "fin_cashflow",          "pk": ["stock_code", "report_date", "announce_date"]},
    "Pershareindex":   {"pg_table": "fin_indicator",         "pk": ["stock_code", "report_date", "announce_date"]},
    "Capital":         {"pg_table": "fin_capital",           "pk": ["stock_code", "report_date"]},
    "Holdernum":       {"pg_table": "fin_holdernum",         "pk": ["stock_code", "end_date"]},
    "Top10holder":     {"pg_table": "fin_top10_holder",      "pk": ["stock_code", "end_date", "rank"]},
    "Top10flowholder": {"pg_table": "fin_top10_flow_holder", "pk": ["stock_code", "end_date", "rank"]},
}

ALL_XT_TABLES = list(FINANCIAL_TABLES.keys())

# 需要丢弃的辅助列
_DROP_COLS = {"m_quarter"}

_DOWNLOAD_BATCH_SIZE = 50


# ======== 标准化 ========

def _normalize_financial_df(
    df: pd.DataFrame, stock_code: str, xt_table: str,
) -> pd.DataFrame:
    """标准化 xtquant 财务 DataFrame：添加 stock_code，转换日期列。"""
    if df.empty:
        return df

    df = df.copy()
    df["stock_code"] = stock_code

    # 报表类表：m_timetag → report_date, m_anntime → announce_date
    if "m_timetag" in df.columns:
        df["report_date"] = pd.to_datetime(df["m_timetag"], format="%Y%m%d", errors="coerce").dt.date
        df = df.drop(columns=["m_timetag"])
    if "m_anntime" in df.columns:
        df["announce_date"] = pd.to_datetime(df["m_anntime"], format="%Y%m%d", errors="coerce").dt.date
        df = df.drop(columns=["m_anntime"])

    # 股东类表：declareDate → announce_date, endDate → end_date
    if "declareDate" in df.columns:
        df["announce_date"] = pd.to_datetime(df["declareDate"], format="%Y%m%d", errors="coerce").dt.date
        df = df.drop(columns=["declareDate"])
    if "endDate" in df.columns:
        df["end_date"] = pd.to_datetime(df["endDate"], format="%Y%m%d", errors="coerce").dt.date
        df = df.drop(columns=["endDate"])

    # 丢弃辅助列
    drop = _DROP_COLS & set(df.columns)
    if drop:
        df = df.drop(columns=list(drop))

    # 去掉主键列中有 NaT/None 的行
    cfg = FINANCIAL_TABLES[xt_table]
    pk_cols = [c for c in cfg["pk"] if c in df.columns]
    if pk_cols:
        df = df.dropna(subset=pk_cols)
        # xtquant 源数据偶有完全重复行（如十大股东），按主键去重
        df = df.drop_duplicates(subset=pk_cols)

    return df


# ======== 自动建表 ========

def _pg_type(series: pd.Series, col_name: str) -> str:
    """根据 pandas dtype 推断 PG 列类型。"""
    if col_name == "stock_code":
        return "VARCHAR(20) NOT NULL"
    if col_name in ("report_date", "announce_date", "end_date"):
        return "DATE NOT NULL" if col_name != "announce_date" else "DATE"
    if col_name == "rank":
        return "SMALLINT NOT NULL"
    dtype = series.dtype
    if pd.api.types.is_float_dtype(dtype) or pd.api.types.is_integer_dtype(dtype):
        return "DOUBLE PRECISION"
    return "TEXT"


# 已建表缓存（进程内）
_created_tables: set = set()


def _ensure_table_exists(pg_table: str, df: pd.DataFrame, pk_cols: List[str]) -> None:
    """如果 PG 表不存在，根据 DataFrame 自动建表。"""
    if pg_table in _created_tables:
        return

    validate_table_name(pg_table)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=%s",
                (pg_table,),
            )
            if cur.fetchone():
                _created_tables.add(pg_table)
                return

        # 建表
        col_defs = []
        for col in df.columns:
            pg_type = _pg_type(df[col], col)
            col_defs.append(f'    "{col}" {pg_type}')

        pk_str = ", ".join(f'"{c}"' for c in pk_cols)
        col_defs.append(f"    PRIMARY KEY ({pk_str})")

        ddl = f'CREATE TABLE IF NOT EXISTS "{pg_table}" (\n' + ",\n".join(col_defs) + "\n);"
        logger.info(f"自动建表: {pg_table}")

        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()

    _created_tables.add(pg_table)


def generate_all_ddl(sample_stock: str = "000001.SZ") -> str:
    """生成所有财务表的 DDL SQL（供导出到 sql/ 目录）。"""
    xtdata = require_xtdata()
    try:
        xtdata.download_financial_data2(stock_list=[sample_stock], table_list=ALL_XT_TABLES)
    except Exception:
        pass

    data = xtdata.get_financial_data([sample_stock], table_list=ALL_XT_TABLES)
    stock_data = data.get(sample_stock, {})

    ddl_parts = []
    for xt_table, cfg in FINANCIAL_TABLES.items():
        df = stock_data.get(xt_table)
        if df is None or not hasattr(df, "columns") or df.empty:
            ddl_parts.append(f"-- {cfg['pg_table']}: 无样本数据，跳过\n")
            continue

        normalized = _normalize_financial_df(df, sample_stock, xt_table)
        if normalized.empty:
            continue

        col_defs = []
        for col in normalized.columns:
            pg_type = _pg_type(normalized[col], col)
            col_defs.append(f'    "{col}" {pg_type}')

        pk_str = ", ".join(f'"{c}"' for c in cfg["pk"])
        col_defs.append(f"    PRIMARY KEY ({pk_str})")

        ddl = (
            f"-- {xt_table} → {cfg['pg_table']}\n"
            f'CREATE TABLE IF NOT EXISTS "{cfg["pg_table"]}" (\n'
            + ",\n".join(col_defs)
            + "\n);\n"
        )
        ddl_parts.append(ddl)

    return "\n".join(ddl_parts)


# ======== 数据写入 ========

def _save_financial_batch(
    stock_codes: List[str],
) -> Dict[str, Tuple[int, int]]:
    """下载并写入一批股票的全部财务数据，返回 {pg_table: (tried, inserted)}。"""
    xtdata = require_xtdata()

    try:
        xtdata.download_financial_data2(stock_list=stock_codes, table_list=ALL_XT_TABLES)
    except Exception as exc:
        logger.warning(f"下载财务数据失败: {exc}")

    data = xtdata.get_financial_data(stock_codes, table_list=ALL_XT_TABLES)

    result: Dict[str, Tuple[int, int]] = {}

    for xt_table, cfg in FINANCIAL_TABLES.items():
        pg_table = cfg["pg_table"]
        pk_cols = cfg["pk"]
        chunks = []

        for code in stock_codes:
            stock_data = data.get(code, {})
            df = stock_data.get(xt_table)
            if df is None or not hasattr(df, "columns") or df.empty:
                continue
            normalized = _normalize_financial_df(df, code, xt_table)
            if not normalized.empty:
                chunks.append(normalized)

        if not chunks:
            continue

        merged = pd.concat(chunks, ignore_index=True)
        _ensure_table_exists(pg_table, merged, pk_cols)

        tried, inserted = save_to_postgres(merged, pre_aligned_df=merged, table_name=pg_table)
        skipped = tried - inserted
        skip_msg = f"，跳过 {skipped}" if skipped else ""
        logger.info(f"{pg_table}: 尝试 {tried} 条，写入 {inserted}{skip_msg}")
        prev = result.get(pg_table, (0, 0))
        result[pg_table] = (prev[0] + tried, prev[1] + inserted)

    return result


# ======== Pipeline 标准接口 ========

def _should_run_today(trade_date: str) -> bool:
    """判断当天是否在财报披露窗口内。

    披露窗口（3/4/7/8/10月）：每周一、四更新。
    其余月份：月初第一个交易日（day<=5）更新，捕获零星修订。
    """
    dt = pd.to_datetime(trade_date)
    month, weekday = dt.month, dt.weekday()  # weekday: 0=周一

    if month in (3, 4, 7, 8, 10):
        return weekday in (0, 3)  # 周一、周四
    return dt.day <= 5


def run(run_date: str, **kwargs) -> str:
    """每日任务：下载全部 A 股最新财务数据，写入 PG。"""
    trade_date = normalize_trade_date(run_date)
    limit_stocks = kwargs.get("limit_stocks")

    if not is_market_day(trade_date):
        return f"{trade_date} 非交易日，财务数据任务跳过。"

    if not _should_run_today(trade_date):
        return f"{trade_date} 非财报披露窗口，财务数据任务跳过。"

    codes = get_a_share_codes()
    if limit_stocks is not None and limit_stocks > 0:
        codes = codes[:limit_stocks]

    totals: Dict[str, Tuple[int, int]] = {}

    for i in range(0, len(codes), _DOWNLOAD_BATCH_SIZE):
        batch = codes[i : i + _DOWNLOAD_BATCH_SIZE]
        batch_result = _save_financial_batch(batch)
        for table, (tried, inserted) in batch_result.items():
            prev = totals.get(table, (0, 0))
            totals[table] = (prev[0] + tried, prev[1] + inserted)

    total_tried = sum(t for t, _ in totals.values())
    total_inserted = sum(i for _, i in totals.values())
    return (
        f"{trade_date} 财务数据任务完成，"
        f"{len(totals)} 张表，写入 {total_tried}/{total_inserted} 条。"
    )


def run_backfill(start_date: str, end_date: str, limit_stocks: int | None = None) -> str:
    """全量补历史：分批下载所有股票全量财务数据。"""
    codes = get_a_share_codes()
    if limit_stocks is not None and limit_stocks > 0:
        codes = codes[:limit_stocks]

    totals: Dict[str, Tuple[int, int]] = {}
    total_batches = (len(codes) + _DOWNLOAD_BATCH_SIZE - 1) // _DOWNLOAD_BATCH_SIZE

    with tqdm(total=len(codes), desc="财务数据", unit="股", file=sys.stdout) as pbar:
        for i in range(0, len(codes), _DOWNLOAD_BATCH_SIZE):
            batch = codes[i : i + _DOWNLOAD_BATCH_SIZE]
            pbar.set_postfix(batch=f"{i // _DOWNLOAD_BATCH_SIZE + 1}/{total_batches}")

            batch_result = _save_financial_batch(batch)
            for table, (tried, inserted) in batch_result.items():
                prev = totals.get(table, (0, 0))
                totals[table] = (prev[0] + tried, prev[1] + inserted)

            pbar.update(len(batch))

    total_tried = sum(t for t, _ in totals.values())
    total_inserted = sum(i for _, i in totals.values())

    lines = [f"财务数据补历史完成，{len(codes)} 只股票，{len(totals)} 张表："]
    for table, (tried, inserted) in sorted(totals.items()):
        lines.append(f"  {table}: {tried}/{inserted}")
    lines.append(f"  合计: {total_tried}/{total_inserted}")

    message = "\n".join(lines)
    send_dingtalk(message)
    return message
