"""
A股板块/行业分类采集任务

采集 GICS 行业、同花顺行业、同花顺概念、同花顺风格板块的成分股映射。
主表 sector_stock 存最新成分，changelog 表 sector_changelog 记录调入/调出。

注意：xtquant 只返回当前板块成分，无法获取历史状态。
changelog 只能记录从首次运行起的成分变更（调入/调出），无法回溯历史调整。
backfill 等同于 run 一次（建立当前基准）。
"""

from __future__ import annotations

import logging
import sys
from typing import Dict, List, Set, Tuple

import pandas as pd
from tqdm import tqdm

from data_collect.utils.date_utils import is_market_day
from data_collect.utils.db import get_connection, require_psycopg2
from data_collect.utils.df_utils import normalize_trade_date
from data_collect.utils.notify import send_dingtalk
from data_collect.utils.xtquant_utils import require_xtdata

logger = logging.getLogger(__name__)

SECTOR_TABLE = "sector_stock"
CHANGELOG_TABLE = "sector_changelog"

# 只采集这些前缀的板块
_SECTOR_PREFIXES = ("GICS1", "GICS2", "GICS3", "GICS4", "THY1", "THY2", "THY3", "TDGN", "TGN", "TFG")

_tables_ensured = False


def _ensure_tables() -> None:
    global _tables_ensured
    if _tables_ensured:
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{SECTOR_TABLE}" (
                    "sector_name" TEXT NOT NULL,
                    "stock_code"  VARCHAR(20) NOT NULL,
                    "update_date" DATE NOT NULL,
                    PRIMARY KEY ("sector_name", "stock_code")
                );
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{CHANGELOG_TABLE}" (
                    "sector_name" TEXT NOT NULL,
                    "stock_code"  VARCHAR(20) NOT NULL,
                    "changed_at"  DATE NOT NULL,
                    "action"      VARCHAR(10) NOT NULL,
                    PRIMARY KEY ("sector_name", "stock_code", "changed_at", "action")
                );
            """)
        conn.commit()

    _tables_ensured = True


def _get_filtered_sectors() -> List[str]:
    """获取需要采集的板块列表。

    注意：QMT mini版可能只有基础板块（沪深A股等），
    无 GICS/THY 等细分板块。download_sector_data() 会阻塞，不调用。
    """
    xtdata = require_xtdata()
    all_sectors = xtdata.get_sector_list()
    return [s for s in all_sectors if s.startswith(_SECTOR_PREFIXES)]


def _fetch_all_sector_stocks(sectors: List[str]) -> Dict[str, Set[str]]:
    """获取所有板块的成分股。"""
    xtdata = require_xtdata()
    result = {}
    fail_count = 0
    for sector in tqdm(sectors, desc="板块成分", unit="板块", file=sys.stdout):
        try:
            codes = xtdata.get_stock_list_in_sector(sector)
            if codes:
                result[sector] = set(codes)
        except Exception:
            fail_count += 1

    if fail_count:
        logger.warning(f"板块成分采集 {fail_count} 个板块失败")
    return result


def _load_existing_sectors() -> Dict[str, Set[str]]:
    """从 DB 读取现有板块成分。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT sector_name, stock_code FROM "{SECTOR_TABLE}"')
            rows = cur.fetchall()

    result: Dict[str, Set[str]] = {}
    for sector, code in rows:
        result.setdefault(sector, set()).add(code)
    return result


def _sync_sectors(
    new_data: Dict[str, Set[str]], run_date: str,
) -> Tuple[int, int, int]:
    """同步板块数据，返回 (新增数, 删除数, changelog条数)。"""
    if not new_data:
        return 0, 0, 0

    _ensure_tables()
    existing = _load_existing_sectors()
    today = pd.to_datetime(run_date).date()

    inserts = []      # (sector, code, date)
    deletes = []      # (sector, code)
    changelogs = []   # (sector, code, date, action)

    all_sectors = set(new_data.keys()) | set(existing.keys())

    for sector in all_sectors:
        new_codes = new_data.get(sector, set())
        old_codes = existing.get(sector, set())

        added = new_codes - old_codes
        removed = old_codes - new_codes

        for code in added:
            inserts.append((sector, code, today))
            changelogs.append((sector, code, today, "add"))
        for code in removed:
            deletes.append((sector, code))
            changelogs.append((sector, code, today, "remove"))

    _, execute_values = require_psycopg2()

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 批量删除调出的成分股
            if deletes:
                sql = f'DELETE FROM "{SECTOR_TABLE}" WHERE sector_name=%s AND stock_code=%s'
                cur.executemany(sql, deletes)

            # 插入调入的成分股
            if inserts:
                sql = f'INSERT INTO "{SECTOR_TABLE}" (sector_name, stock_code, update_date) VALUES %s ON CONFLICT DO NOTHING'
                execute_values(cur, sql, inserts, page_size=5000)

            # 记录变更
            if changelogs:
                sql = (
                    f'INSERT INTO "{CHANGELOG_TABLE}" '
                    f'(sector_name, stock_code, changed_at, action) VALUES %s '
                    f'ON CONFLICT DO NOTHING'
                )
                execute_values(cur, sql, changelogs, page_size=5000)

        conn.commit()

    return len(inserts), len(deletes), len(changelogs)


# ======== Pipeline 标准接口 ========

def run(run_date: str, **kwargs) -> str:
    trade_date = normalize_trade_date(run_date)

    if not is_market_day(trade_date):
        return f"{trade_date} 非交易日，板块分类任务跳过。"

    _ensure_tables()
    sectors = _get_filtered_sectors()
    data = _fetch_all_sector_stocks(sectors)
    added, removed, changes = _sync_sectors(data, trade_date)

    return (
        f"{trade_date} 板块分类完成，{len(sectors)} 个板块，"
        f"调入 {added}，调出 {removed}，变更 {changes} 条。"
    )


def run_backfill(start_date: str, end_date: str, limit_stocks: int | None = None) -> str:
    """建立当前基准快照。

    xtquant 只返回当前板块成分，不提供历史快照，因此无法回溯历史调整。
    backfill 等同于 run 一次：首次运行建立 baseline，后续每日 run 检测调入/调出。
    """
    result = run(end_date)
    send_dingtalk(result)
    return result
