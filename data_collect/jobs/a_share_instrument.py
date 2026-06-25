"""
A股合约详情采集任务

通过 xtquant.get_instrument_detail() 获取合约基础信息（83字段），
排除每日变动字段后存入 instrument_info 快照表。
与上次快照对比，有变化时写入 instrument_changelog 变更记录。

注意：xtquant 只返回当前快照，无法获取历史状态。
changelog 只能记录从首次运行起的增量变更，无法回溯历史。
backfill 等同于 run 一次（建立当前基准）。
"""

from __future__ import annotations

import logging
import sys
from typing import Dict, List, Tuple

import pandas as pd
from tqdm import tqdm

from data_collect.utils.date_utils import is_market_day
from data_collect.utils.db import get_connection, require_psycopg2
from data_collect.utils.df_utils import normalize_trade_date
from data_collect.utils.notify import send_dingtalk
from data_collect.utils.xtquant_utils import require_xtdata, get_a_share_codes

logger = logging.getLogger(__name__)

INFO_TABLE = "instrument_info"
CHANGELOG_TABLE = "instrument_changelog"

# 每日变动的字段，不存入快照表
_DAILY_CHANGING_FIELDS = {
    "PreClose", "UpStopPrice", "DownStopPrice", "SettlementPrice",
    "TradingDay", "IsTrading", "InstrumentStatus", "LastVolume",
}


def _fetch_instrument(stock_code: str) -> dict | None:
    """获取单只股票的合约详情，排除每日变动字段。"""
    xtdata = require_xtdata()
    detail = xtdata.get_instrument_detail(stock_code, iscomplete=True)
    if not detail:
        return None
    # 排除每日变动字段
    return {k: v for k, v in detail.items() if k not in _DAILY_CHANGING_FIELDS}


def _pg_type_for_value(value) -> str:
    """根据 Python 值推断 PG 列类型。"""
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE PRECISION"
    return "TEXT"


_tables_ensured = set()


def _ensure_tables(sample_detail: dict) -> None:
    """确保 instrument_info 和 instrument_changelog 表存在。"""
    if INFO_TABLE in _tables_ensured:
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=%s",
                (INFO_TABLE,),
            )
            if not cur.fetchone():
                # 自动建 instrument_info
                col_defs = ['    "stock_code" VARCHAR(20) PRIMARY KEY']
                for k, v in sample_detail.items():
                    pg_type = _pg_type_for_value(v)
                    col_defs.append(f'    "{k}" {pg_type}')
                col_defs.append('    "updated_at" TIMESTAMP DEFAULT NOW()')
                ddl = f'CREATE TABLE IF NOT EXISTS "{INFO_TABLE}" (\n' + ",\n".join(col_defs) + "\n);"
                cur.execute(ddl)
                logger.info(f"自动建表: {INFO_TABLE}")

            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=%s",
                (CHANGELOG_TABLE,),
            )
            if not cur.fetchone():
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS "{CHANGELOG_TABLE}" (
                        "stock_code"  VARCHAR(20) NOT NULL,
                        "changed_at"  DATE NOT NULL,
                        "field_name"  VARCHAR(80) NOT NULL,
                        "old_value"   TEXT,
                        "new_value"   TEXT,
                        PRIMARY KEY ("stock_code", "changed_at", "field_name")
                    );
                """)
                logger.info(f"自动建表: {CHANGELOG_TABLE}")

        conn.commit()

    _tables_ensured.add(INFO_TABLE)


def _load_existing(stock_codes: List[str]) -> Dict[str, dict]:
    """从 DB 读取现有快照，返回 {stock_code: {field: value}}。"""
    if not stock_codes:
        return {}

    with get_connection() as conn:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(stock_codes))
            cur.execute(
                f'SELECT * FROM "{INFO_TABLE}" WHERE stock_code IN ({placeholders})',
                stock_codes,
            )
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

    result = {}
    for row in rows:
        record = dict(zip(columns, row))
        code = record.pop("stock_code")
        record.pop("updated_at", None)
        result[code] = record
    return result


def _upsert_and_log_changes(
    details: Dict[str, dict],
    run_date: str,
) -> Tuple[int, int, int]:
    """写入/更新快照，记录变更。返回 (新增数, 更新数, 变更字段数)。"""
    if not details:
        return 0, 0, 0

    codes = list(details.keys())
    existing = _load_existing(codes)

    inserts = []
    updates = []
    changelogs = []
    today = pd.to_datetime(run_date).date()

    for code, new_data in details.items():
        old_data = existing.get(code)
        if old_data is None:
            # 新股票，INSERT
            inserts.append((code, new_data))
        else:
            # 对比字段变化
            changed_fields = {}
            for field, new_val in new_data.items():
                old_val = old_data.get(field)
                # 统一比较（转字符串避免类型不匹配）
                if str(new_val) != str(old_val):
                    changed_fields[field] = (str(old_val), str(new_val))

            if changed_fields:
                updates.append((code, new_data))
                for field, (old_v, new_v) in changed_fields.items():
                    changelogs.append((code, today, field, old_v, new_v))

    _, execute_values = require_psycopg2()

    with get_connection() as conn:
        with conn.cursor() as cur:
            # UPSERT: 新记录 INSERT，已有记录 UPDATE（合并 inserts + updates）
            all_upserts = inserts + updates
            if all_upserts:
                sample = all_upserts[0][1]
                fields = list(sample.keys())
                cols = '"stock_code", ' + ", ".join(f'"{f}"' for f in fields)
                set_clause = ", ".join(
                    f'"{f}" = EXCLUDED."{f}"' for f in fields
                )
                rows = [(code, *[data[f] for f in fields]) for code, data in all_upserts]
                sql = (
                    f'INSERT INTO "{INFO_TABLE}" ({cols}) VALUES %s '
                    f'ON CONFLICT ("stock_code") DO UPDATE SET {set_clause}, '
                    f'"updated_at" = NOW()'
                )
                execute_values(cur, sql, rows, page_size=1000)

            # INSERT changelog
            if changelogs:
                sql = (
                    f'INSERT INTO "{CHANGELOG_TABLE}" '
                    f'("stock_code", "changed_at", "field_name", "old_value", "new_value") '
                    f'VALUES %s ON CONFLICT DO NOTHING'
                )
                execute_values(cur, sql, changelogs, page_size=1000)

        conn.commit()

    return len(inserts), len(updates), len(changelogs)


def _collect_all(limit_stocks: int | None = None) -> Dict[str, dict]:
    """获取全部 A 股合约详情。"""
    codes = get_a_share_codes()
    if not codes:
        raise RuntimeError("未获取到A股股票列表")

    if limit_stocks is not None and limit_stocks > 0:
        codes = codes[:limit_stocks]

    details = {}
    fail_count = 0
    for code in tqdm(codes, desc="合约详情", unit="股", file=sys.stdout):
        try:
            d = _fetch_instrument(code)
            if d:
                details[code] = d
        except Exception:
            fail_count += 1

    if fail_count:
        logger.warning(f"合约详情采集 {fail_count} 只股票失败")

    return details


# ======== Pipeline 标准接口 ========

def run(run_date: str, **kwargs) -> str:
    trade_date = normalize_trade_date(run_date)
    limit_stocks = kwargs.get("limit_stocks")

    if not is_market_day(trade_date):
        return f"{trade_date} 非交易日，合约详情任务跳过。"

    details = _collect_all(limit_stocks)
    if details:
        sample = next(iter(details.values()))
        _ensure_tables(sample)

    new, updated, changes = _upsert_and_log_changes(details, trade_date)
    return (
        f"{trade_date} 合约详情完成，"
        f"新增 {new}，更新 {updated}，变更 {changes} 个字段。"
    )


def run_backfill(start_date: str, end_date: str, limit_stocks: int | None = None) -> str:
    """建立当前基准快照。

    xtquant 只返回当前合约信息，不提供历史快照，因此无法回溯历史变更。
    backfill 等同于 run 一次：首次运行建立 baseline，后续每日 run 检测变更。
    """
    result = run(end_date, limit_stocks=limit_stocks)
    send_dingtalk(result)
    return result
