"""
A股指数成分权重采集任务

采集沪深300、中证500、中证1000、上证50、创业板指的成分股权重。
主表 index_weight 存最新权重，changelog 表 index_weight_changelog 记录变化。

注意：xtquant 只返回当前权重，无法获取历史权重。
changelog 只能记录从首次运行起的权重变更，无法回溯历史调仓。
backfill 等同于 run 一次（建立当前基准）。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

import pandas as pd

from data_collect.utils.date_utils import is_market_day
from data_collect.utils.db import get_connection, require_psycopg2
from data_collect.utils.df_utils import normalize_trade_date
from data_collect.utils.notify import send_dingtalk
from data_collect.utils.xtquant_utils import require_xtdata

logger = logging.getLogger(__name__)

WEIGHT_TABLE = "index_weight"
CHANGELOG_TABLE = "index_weight_changelog"

# 采集的指数列表
INDEX_CODES = [
    "000300.SH",  # 沪深300
    "000905.SH",  # 中证500
    "000852.SH",  # 中证1000
    "000016.SH",  # 上证50
    "399006.SZ",  # 创业板指
]

_tables_ensured = False


def _ensure_tables() -> None:
    global _tables_ensured
    if _tables_ensured:
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{WEIGHT_TABLE}" (
                    "index_code"  VARCHAR(20) NOT NULL,
                    "stock_code"  VARCHAR(20) NOT NULL,
                    "weight"      DOUBLE PRECISION,
                    "update_date" DATE NOT NULL,
                    PRIMARY KEY ("index_code", "stock_code")
                );
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{CHANGELOG_TABLE}" (
                    "index_code"  VARCHAR(20) NOT NULL,
                    "stock_code"  VARCHAR(20) NOT NULL,
                    "changed_at"  DATE NOT NULL,
                    "old_weight"  DOUBLE PRECISION,
                    "new_weight"  DOUBLE PRECISION,
                    PRIMARY KEY ("index_code", "stock_code", "changed_at")
                );
            """)
        conn.commit()

    _tables_ensured = True


def _fetch_all_weights() -> Dict[str, Dict[str, float]]:
    """获取所有指数的成分权重。"""
    xtdata = require_xtdata()
    try:
        xtdata.download_index_weight()
    except Exception as exc:
        logger.warning(f"下载指数权重失败: {exc}")
    result = {}
    for idx in INDEX_CODES:
        try:
            w = xtdata.get_index_weight(idx)
            if isinstance(w, dict) and w:
                result[idx] = w
        except Exception:
            logger.warning(f"获取 {idx} 权重失败")
    return result


def _load_existing_weights() -> Dict[str, Dict[str, float]]:
    """从 DB 读取现有权重。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT index_code, stock_code, weight FROM "{WEIGHT_TABLE}"')
            rows = cur.fetchall()

    result: Dict[str, Dict[str, float]] = {}
    for idx, code, weight in rows:
        result.setdefault(idx, {})[code] = weight
    return result


def _sync_weights(
    new_data: Dict[str, Dict[str, float]], run_date: str,
) -> Tuple[int, int, int]:
    """同步权重数据，返回 (新增数, 删除数, 变更数)。"""
    if not new_data:
        return 0, 0, 0

    _ensure_tables()
    existing = _load_existing_weights()
    today = pd.to_datetime(run_date).date()

    inserts = []      # (index, code, weight, date)
    deletes = []      # (index, code)
    changelogs = []   # (index, code, date, old_w, new_w)

    all_indices = set(new_data.keys()) | set(existing.keys())

    for idx in all_indices:
        new_w = new_data.get(idx, {})
        old_w = existing.get(idx, {})
        all_codes = set(new_w.keys()) | set(old_w.keys())

        for code in all_codes:
            nv = new_w.get(code)
            ov = old_w.get(code)

            if nv is not None and ov is None:
                # 新调入
                inserts.append((idx, code, nv, today))
                changelogs.append((idx, code, today, None, nv))
            elif nv is None and ov is not None:
                # 调出
                deletes.append((idx, code))
                changelogs.append((idx, code, today, ov, None))
            elif nv is not None and ov is not None and abs(nv - ov) > 1e-6:
                # 权重变化
                inserts.append((idx, code, nv, today))  # UPSERT
                changelogs.append((idx, code, today, ov, nv))

    _, execute_values = require_psycopg2()

    with get_connection() as conn:
        with conn.cursor() as cur:
            if deletes:
                sql = f'DELETE FROM "{WEIGHT_TABLE}" WHERE index_code=%s AND stock_code=%s'
                cur.executemany(sql, deletes)

            if inserts:
                sql = (
                    f'INSERT INTO "{WEIGHT_TABLE}" (index_code, stock_code, weight, update_date) '
                    f'VALUES %s ON CONFLICT (index_code, stock_code) '
                    f'DO UPDATE SET weight = EXCLUDED.weight, update_date = EXCLUDED.update_date'
                )
                execute_values(cur, sql, inserts, page_size=1000)

            if changelogs:
                sql = (
                    f'INSERT INTO "{CHANGELOG_TABLE}" '
                    f'(index_code, stock_code, changed_at, old_weight, new_weight) '
                    f'VALUES %s ON CONFLICT DO NOTHING'
                )
                execute_values(cur, sql, changelogs, page_size=1000)

        conn.commit()

    return len(inserts), len(deletes), len(changelogs)


# ======== Pipeline 标准接口 ========

def run(run_date: str, **kwargs) -> str:
    trade_date = normalize_trade_date(run_date)

    if not is_market_day(trade_date):
        return f"{trade_date} 非交易日，指数权重任务跳过。"

    _ensure_tables()
    data = _fetch_all_weights()
    added, removed, changes = _sync_weights(data, trade_date)

    total_stocks = sum(len(v) for v in data.values())
    return (
        f"{trade_date} 指数权重完成，{len(data)} 个指数 {total_stocks} 只成分股，"
        f"新增/更新 {added}，调出 {removed}，变更 {changes} 条。"
    )


def run_backfill(start_date: str, end_date: str, limit_stocks: int | None = None) -> str:
    """建立当前基准快照。

    xtquant 只返回当前权重，不提供历史权重，因此无法回溯历史调仓。
    backfill 等同于 run 一次：首次运行建立 baseline，后续每日 run 检测权重变更。
    """
    result = run(end_date)
    send_dingtalk(result)
    return result
