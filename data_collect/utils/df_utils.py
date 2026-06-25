"""DataFrame 工具：日期标准化、字段对齐、代码格式转换。"""

from __future__ import annotations

import datetime as dt
from typing import List, Sequence, Tuple

import pandas as pd


def normalize_trade_date(date_text: str | None) -> str:
    """将输入日期标准化为 YYYYMMDD，支持 YYYYMMDD / YYYY-MM-DD。"""
    if date_text is None or str(date_text).strip() == "":
        return dt.datetime.now().strftime("%Y%m%d")
    parsed = pd.to_datetime(str(date_text), errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"无效日期格式: {date_text}，请使用 YYYYMMDD 或 YYYY-MM-DD")
    return parsed.strftime("%Y%m%d")


def align_dataframe_to_table(df: pd.DataFrame, table_columns: Sequence[str]) -> pd.DataFrame:
    """仅保留数据库中存在的字段，并按数据库字段顺序排列。"""
    if df.empty:
        return pd.DataFrame(columns=list(table_columns))

    lowered = {col.lower(): col for col in df.columns}
    selected = {}
    for db_col in table_columns:
        if db_col in df.columns:
            selected[db_col] = df[db_col]
        elif db_col.lower() in lowered:
            selected[db_col] = df[lowered[db_col.lower()]]

    if not selected:
        return pd.DataFrame(columns=list(table_columns))

    return pd.DataFrame(selected)


def _normalize_code_value(code_val: object, max_len: int | None) -> str:
    text = str(code_val)
    if "." in text:
        num, market = text.split(".", 1)
        market = market.lower()
        if max_len == 8 and len(num) == 6 and len(market) >= 2:
            return f"{market[:2]}{num}"
        if max_len == 6 and len(num) == 6:
            return num
    if max_len is not None and len(text) > max_len:
        return text[:max_len]
    return text


def align_dataframe_by_position(
    df: pd.DataFrame, table_schema: Sequence[Tuple[str, str, int | None]]
) -> pd.DataFrame:
    """字段无同名交集时，按约定顺序映射到目标表。"""
    if df.empty:
        return pd.DataFrame(columns=[name for name, _, _ in table_schema])

    source_order = [
        "trade_date", "bar_time", "stock_code",
        "open", "high", "low", "close", "volume", "amount",
    ]
    if not all(col in df.columns for col in source_order):
        return pd.DataFrame(columns=[name for name, _, _ in table_schema])

    use_len = min(len(source_order), len(table_schema))
    mapped = {}
    for idx in range(use_len):
        target_col, target_type, max_len = table_schema[idx]
        source_col = source_order[idx]
        series = df[source_col]

        type_name = (target_type or "").lower()
        if "time without time zone" in type_name:
            series = pd.to_datetime(series, errors="coerce").dt.time
        elif type_name == "date":
            series = pd.to_datetime(series, errors="coerce").dt.date
        elif source_col == "stock_code":
            series = series.map(lambda x: _normalize_code_value(x, max_len))

        mapped[target_col] = series

    return pd.DataFrame(mapped)
