"""PostgreSQL 数据库操作模块。"""

from __future__ import annotations

import importlib
import logging
from typing import List, Sequence, Tuple

import pandas as pd

from data_collect.config import get_db_config, get_export_config
from data_collect.utils.df_utils import align_dataframe_to_table, align_dataframe_by_position
from data_collect.utils.retry import retry_db

logger = logging.getLogger(__name__)


def require_psycopg2():
    try:
        psycopg2 = importlib.import_module("psycopg2")
        extras_module = importlib.import_module("psycopg2.extras")
        execute_values = getattr(extras_module, "execute_values")
    except Exception as exc:
        raise ImportError("缺少 psycopg2-binary，请先安装：pip install psycopg2-binary") from exc
    return psycopg2, execute_values


def get_connection():
    """创建 PostgreSQL 连接。"""
    psycopg2, _ = require_psycopg2()
    return psycopg2.connect(**get_db_config())


def get_table_schema(conn, table_name: str) -> List[Tuple[str, str, int | None]]:
    """读取数据库表字段及类型，按定义顺序返回。"""
    sql = """
        SELECT column_name, data_type, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (table_name,))
        rows = cursor.fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def get_table_columns(conn, table_name: str) -> List[str]:
    """读取数据库表字段名，按定义顺序返回。"""
    return [name for name, _, _ in get_table_schema(conn, table_name)]


def validate_table_name(table_name: str) -> str:
    """校验表名，防止 SQL 注入。"""
    if not table_name.isidentifier():
        raise ValueError(f"非法表名: {table_name}")
    return table_name


# 进程内 schema 缓存：同一个子进程内不会变，避免 backfill 循环中重复查询
_schema_cache: dict = {}


def _get_cached_schema(table_name: str) -> List[Tuple[str, str, int | None]]:
    """获取表 schema，同一进程内缓存。"""
    if table_name not in _schema_cache:
        with get_connection() as conn:
            _schema_cache[table_name] = get_table_schema(conn, table_name)
    return _schema_cache[table_name]


def prepare_db_aligned_data(df: pd.DataFrame, table_name: str | None = None) -> pd.DataFrame:
    """将采集数据按数据库表字段对齐，得到最终入库形态数据。"""
    if df.empty:
        return pd.DataFrame()

    if table_name is None:
        table_name = get_export_config().get("table_name", "minutedata_tdx")

    table_schema = _get_cached_schema(table_name)
    table_columns = [name for name, _, _ in table_schema]

    if not table_columns:
        raise RuntimeError(f"表 {table_name} 不存在或当前账号无权访问字段信息")

    aligned = align_dataframe_to_table(df, table_columns)
    if aligned.empty:
        aligned = align_dataframe_by_position(df, table_schema)
    if aligned.empty:
        raise RuntimeError(f"采集结果字段与表 {table_name} 无交集，请检查表结构与字段映射")
    return aligned


@retry_db
def save_to_postgres(
    df: pd.DataFrame,
    pre_aligned_df: pd.DataFrame | None = None,
    table_name: str | None = None,
) -> Tuple[int, int]:
    """
    入库并自动跳过冲突数据。
    返回：(尝试写入条数, 实际写入条数)
    """
    if df.empty and (pre_aligned_df is None or pre_aligned_df.empty):
        return 0, 0

    if table_name is None:
        table_name = get_export_config().get("table_name", "minutedata_tdx")
    validate_table_name(table_name)

    _, execute_values = require_psycopg2()

    aligned = pre_aligned_df if pre_aligned_df is not None else prepare_db_aligned_data(df, table_name)
    aligned = aligned.where(pd.notna(aligned), None)
    rows = [tuple(record) for record in aligned.itertuples(index=False, name=None)]
    if not rows:
        return 0, 0

    with get_connection() as conn:
        quoted_columns = ", ".join(f'"{col}"' for col in aligned.columns)
        insert_sql = (
            f'INSERT INTO "{table_name}" ({quoted_columns}) VALUES %s '
            "ON CONFLICT DO NOTHING"
        )

        chunk_size = 10000
        affected_rows = 0
        with conn.cursor() as cursor:
            for i in range(0, len(rows), chunk_size):
                batch = rows[i : i + chunk_size]
                execute_values(cursor, insert_sql, batch, page_size=len(batch))
                affected_rows += cursor.rowcount

        conn.commit()

    skipped = len(rows) - affected_rows
    skip_msg = f"，跳过 {skipped}" if skipped else ""
    logger.debug(f"{table_name}: 尝试 {len(rows)} 条，写入 {affected_rows}{skip_msg}")
    return len(rows), affected_rows


def has_data_for_date(table: str, date_column: str, trade_date: str) -> bool:
    """检查指定表在指定日期是否有数据。"""
    validate_table_name(table)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT 1 FROM "{table}" WHERE "{date_column}" = %s LIMIT 1',
                (trade_date,),
            )
            return cur.fetchone() is not None


def get_dates_with_data(table: str, date_column: str, start_date: str, end_date: str) -> set:
    """批量查询日期范围内有数据的日期，返回 set。避免逐日查询的 N+1 问题。"""
    validate_table_name(table)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT DISTINCT "{date_column}" FROM "{table}" '
                f'WHERE "{date_column}" >= %s AND "{date_column}" <= %s',
                (start_date, end_date),
            )
            return {row[0] for row in cur.fetchall()}
