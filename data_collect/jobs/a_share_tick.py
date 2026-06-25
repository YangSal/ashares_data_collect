"""
A股Tick数据采集任务

通过 xtquant 获取逐笔 tick，按"交易日打包"为单个 Parquet+zstd 文件存储：

    base_dir / YYYY / MM / DD.parquet      （含当日全部股票，列含 stock_code）

相比每股一文件：体积更省(~20%+)，文件数从每日数千降为 1，便于 NAS 备份/同步。

写入流程：分批下载(50只/批) → 每批读取规范化 → 以 row-group 增量写入当日文件(内存可控) →
全部完成后原子重命名 .tmp→正式文件。
幂等：当日文件已存在即跳过；某交易日确无数据(已过 QMT tick 保留期)时写 .empty 标记，
避免 verify 反复重试。删除 .empty 可强制重试。

注意：QMT 服务器对 tick(分笔) 仅保留约近 3~4 周，过期日期无法再下载（download 静默返回空）。
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import List, NamedTuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

from data_collect.config import get_tick_config
from data_collect.utils.date_utils import is_market_day, date_range, minus_one_market_day
from data_collect.utils.df_utils import normalize_trade_date
from data_collect.utils.notify import send_dingtalk
from data_collect.utils.retry import retry_xtquant
from data_collect.utils.xtquant_utils import require_xtdata, get_a_share_codes, download_history_with_retry


# 五档买卖盘 list 列 → 展开后的列名映射
_LIST_COLUMNS = {
    "askPrice": "ask_price",
    "bidPrice": "bid_price",
    "askVol": "ask_vol",
    "bidVol": "bid_vol",
}

# 展开后保留的标量列（原始列名 → 目标列名）
_SCALAR_RENAME = {
    "lastPrice": "last_price",
    "lastClose": "last_close",
    "lastSettlementPrice": "last_settlement_price",
    "stockStatus": "stock_status",
    "openInt": "open_int",
    "settlementPrice": "settlement_price",
    "transactionNum": "transaction_num",
}

_DOWNLOAD_BATCH_SIZE = 50       # 每批下载的股票数

# 当日打包文件的规范 schema：保证所有 row-group、所有日文件结构一致（便于下游统一读取）
_PA_SCHEMA = pa.schema(
    [("stock_code", pa.string()), ("datetime", pa.timestamp("ns"))]
    + [(c, pa.float64()) for c in (
        "last_price", "open", "high", "low", "last_close", "amount",
        "last_settlement_price", "settlement_price", "pe",
    )]
    + [(c, pa.int64()) for c in (
        "volume", "pvolume", "tickvol", "stock_status", "open_int", "transaction_num",
    )]
    + [(f"ask_price_{i}", pa.float64()) for i in range(1, 6)]
    + [(f"bid_price_{i}", pa.float64()) for i in range(1, 6)]
    + [(f"ask_vol_{i}", pa.int64()) for i in range(1, 6)]
    + [(f"bid_vol_{i}", pa.int64()) for i in range(1, 6)]
)
_CANON_COLS = [f.name for f in _PA_SCHEMA]


class DayResult(NamedTuple):
    """单日采集结果。status: 'written'(已写入) / 'skipped'(已存在) / 'unavailable'(无数据)。"""
    stocks: int        # 当日参与的股票总数
    with_data: int     # 有数据的股票数
    rows: int          # 写入的 tick 行数
    status: str


def _expand_list_columns(df: pd.DataFrame) -> pd.DataFrame:
    """将 askPrice/bidPrice/askVol/bidVol 等 list 列展开为 _1.._5。"""
    for raw_col, target_prefix in _LIST_COLUMNS.items():
        if raw_col not in df.columns:
            continue
        first_val = df[raw_col].iloc[0] if len(df) > 0 else None
        if not isinstance(first_val, (list, tuple)):
            continue
        n = len(first_val)
        for i in range(n):
            df[f"{target_prefix}_{i + 1}"] = df[raw_col].apply(
                lambda x, idx=i: x[idx] if isinstance(x, (list, tuple)) and len(x) > idx else None
            )
        df = df.drop(columns=[raw_col])
    return df


def _normalize_tick_df(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    """标准化 tick DataFrame：展开 list 列、转换时间、重命名。"""
    if df.empty:
        return df

    df = _expand_list_columns(df)

    # 转换时间戳：UTC 毫秒 → 北京时间
    if "time" in df.columns and pd.api.types.is_numeric_dtype(df["time"]):
        df["datetime"] = pd.to_datetime(df["time"], unit="ms") + pd.Timedelta(hours=8)
    df = df.drop(columns=["time"], errors="ignore")

    # 重命名标量列
    rename_map = {k: v for k, v in _SCALAR_RENAME.items() if k in df.columns}
    if rename_map:
        df = df.rename(columns=rename_map)

    df["stock_code"] = stock_code
    return df


@retry_xtquant
def _read_one_stock_tick(stock_code: str, trade_date: str) -> pd.DataFrame:
    """从本地缓存读取单只股票指定交易日的 tick 数据。"""
    xtdata = require_xtdata()

    raw = xtdata.get_market_data_ex(
        field_list=[],
        stock_list=[stock_code],
        period="tick",
        start_time=trade_date,
        end_time=trade_date,
        count=-1,
        dividend_type="none",
        fill_data=False,
    )

    df = raw.get(stock_code) if isinstance(raw, dict) else None
    if df is None or df.empty:
        return pd.DataFrame()

    return _normalize_tick_df(df, stock_code)


# ======== 存储路径 / 幂等 ========

def _day_file_path(trade_date: str) -> Path:
    """当日打包文件路径：base_dir / YYYY / MM / DD.parquet"""
    cfg = get_tick_config()
    base = Path(cfg.get("base_dir", "tick_data"))
    dt = pd.to_datetime(trade_date)
    return base / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}.parquet"


def _empty_marker_path(trade_date: str) -> Path:
    """无数据标记路径：base_dir / YYYY / MM / DD.empty（标记该日确无 tick，避免反复重试）。"""
    return _day_file_path(trade_date).with_suffix(".empty")


def _day_done(trade_date: str) -> bool:
    """该交易日是否已处理：当日打包文件已存在，或已标记无数据。"""
    f = _day_file_path(trade_date)
    if f.exists() and f.stat().st_size > 0:
        return True
    return _empty_marker_path(trade_date).exists()


def _ensure_storage_ready() -> Path:
    """采集前自检：tick 存储目录可写且 parquet 引擎可用，否则抛明确异常。

    做一次"写一个极小 parquet 再删除"的探针，把"路径未挂载 / 缺 pyarrow"
    这类系统性问题在采集前就暴露为清晰错误，避免逐股保存失败被静默吞掉、误报为空。
    """
    cfg = get_tick_config()
    base = Path(cfg.get("base_dir", "tick_data"))
    compression = cfg.get("compression", "zstd")
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".storage_selftest.parquet"
        pd.DataFrame({"_t": [0]}).to_parquet(probe, compression=compression, index=False)
        probe.unlink(missing_ok=True)
    except Exception as exc:
        raise RuntimeError(
            f"Tick存储不可用: base_dir={base} compression={compression} -> {exc!r}。"
            f"请检查 config.yaml 的 tick_storage.base_dir 是否存在/已挂载，"
            f"并确认已安装 parquet 引擎(pip install pyarrow)。"
        ) from exc
    return base


def _batch_to_table(dfs: List[pd.DataFrame]) -> pa.Table:
    """把一批股票的规范化 df 合并并对齐到规范 schema 的 pyarrow Table。"""
    bdf = pd.concat(dfs, ignore_index=True).reindex(columns=_CANON_COLS)
    return pa.Table.from_pandas(bdf, schema=_PA_SCHEMA, preserve_index=False)


# ======== 单日采集（按日打包，增量 row-group 写入，原子落盘） ========

def _collect_one_day(
    trade_date: str,
    limit_stocks: int | None = None,
    dp=None,
) -> DayResult:
    """采集单天全部股票 tick，打包写入 base/YYYY/MM/DD.parquet。"""
    _ensure_storage_ready()

    if _day_done(trade_date):
        return DayResult(0, 0, 0, "skipped")

    codes = get_a_share_codes()
    if not codes:
        raise RuntimeError("未获取到A股股票列表，请检查QMT是否启动并已连接xtdata")
    if limit_stocks is not None and limit_stocks > 0:
        codes = codes[:limit_stocks]

    final = _day_file_path(trade_date)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(final) + ".tmp")
    compression = get_tick_config().get("compression", "zstd")

    writer = None
    rows = with_data = fail = 0

    def _handle(batch: List[str]) -> None:
        nonlocal writer, rows, with_data, fail
        download_history_with_retry(batch, "tick", trade_date, trade_date)
        dfs = []
        for code in batch:
            try:
                d = _read_one_stock_tick(code, trade_date)
                if d is not None and not d.empty:
                    dfs.append(d)
                    with_data += 1
            except Exception as exc:
                fail += 1
                logging.warning(f"Tick {code} {trade_date} 读取失败: {exc!r}")
        if dfs:
            table = _batch_to_table(dfs)
            if writer is None:
                writer = pq.ParquetWriter(str(tmp), _PA_SCHEMA, compression=compression)
            writer.write_table(table)
            rows += table.num_rows

    try:
        if dp:
            for batch in dp.iter_batch_chunks(codes, _DOWNLOAD_BATCH_SIZE, desc="Tick"):
                _handle(batch)
        else:
            total_batches = (len(codes) + _DOWNLOAD_BATCH_SIZE - 1) // _DOWNLOAD_BATCH_SIZE
            with tqdm(total=len(codes), desc=f"Tick({trade_date})", unit="股", file=sys.stdout) as pbar:
                for i in range(0, len(codes), _DOWNLOAD_BATCH_SIZE):
                    batch = codes[i : i + _DOWNLOAD_BATCH_SIZE]
                    pbar.set_postfix(batch=f"{i // _DOWNLOAD_BATCH_SIZE + 1}/{total_batches}")
                    _handle(batch)
                    pbar.update(len(batch))
    finally:
        if writer is not None:
            writer.close()

    if fail:
        logging.warning(f"Tick {trade_date} {fail} 只股票读取失败")

    if rows > 0:
        os.replace(str(tmp), str(final))    # 原子落盘，避免半成品文件
        return DayResult(len(codes), with_data, rows, "written")

    # 全天无数据：清理 tmp，写 .empty 标记，避免后续 verify 反复重试（多为已过保留期）
    if tmp.exists():
        tmp.unlink()
    marker = _empty_marker_path(trade_date)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    return DayResult(len(codes), 0, 0, "unavailable")


def read_tick(trade_date: str, stock_code: str | None = None) -> pd.DataFrame:
    """读取某交易日打包 tick；可选按 stock_code 过滤（谓词下推）。无文件返回空 DataFrame。"""
    f = _day_file_path(trade_date)
    if not (f.exists() and f.stat().st_size > 0):
        return pd.DataFrame()
    filters = [("stock_code", "=", stock_code)] if stock_code else None
    return pq.read_table(str(f), filters=filters).to_pandas()


def _legacy_day_dirs(base: Path) -> List[Path]:
    """旧版"每股一文件"布局的日目录：base/YYYY/MM/DD/(含 *.parquet)。

    新版打包文件位于 base/YYYY/MM/DD.parquet（3 层），不会被 4 层 glob 匹配到。
    """
    return sorted({f.parent for f in base.glob("*/*/*/*.parquet")})


def migrate_per_stock_to_packed(delete_old: bool = False, dry_run: bool = False) -> str:
    """一次性迁移：旧版每股文件(YYYY/MM/DD/{code}.parquet) → 当日打包(YYYY/MM/DD.parquet)。

    幂等：当日打包文件已存在则跳过。delete_old=True 时打包成功后删除旧日目录。
    dry_run=True 只统计不写入（仅读 parquet footer 计行数）。
    """
    base = _ensure_storage_ready()
    compression = get_tick_config().get("compression", "zstd")
    day_dirs = _legacy_day_dirs(base)
    if not day_dirs:
        return "未发现旧版每股布局数据，无需迁移。"

    packed = skipped = deleted = 0
    total_rows = 0

    for d in day_dirs:
        trade_date = f"{d.parent.parent.name}{d.parent.name}{d.name}"  # YYYY MM DD
        target = _day_file_path(trade_date)
        if target.exists() and target.stat().st_size > 0:
            skipped += 1
            continue
        files = sorted(d.glob("*.parquet"))
        if not files:
            continue

        if dry_run:
            rows = 0
            for pf in files:
                try:
                    rows += pq.read_metadata(str(pf)).num_rows
                except Exception:
                    pass
            packed += 1
            total_rows += rows
            continue

        tmp = Path(str(target) + ".tmp")
        writer = None
        rows = 0
        try:
            for i in range(0, len(files), _DOWNLOAD_BATCH_SIZE):
                dfs = []
                for pf in files[i : i + _DOWNLOAD_BATCH_SIZE]:
                    try:
                        sdf = pd.read_parquet(pf)
                    except Exception as exc:
                        logging.warning(f"迁移读取失败 {pf}: {exc!r}")
                        continue
                    if sdf.empty:
                        continue
                    if "stock_code" not in sdf.columns:
                        sdf["stock_code"] = pf.stem
                    dfs.append(sdf)
                if dfs:
                    table = _batch_to_table(dfs)
                    if writer is None:
                        writer = pq.ParquetWriter(str(tmp), _PA_SCHEMA, compression=compression)
                    writer.write_table(table)
                    rows += table.num_rows
        finally:
            if writer is not None:
                writer.close()

        if rows > 0:
            os.replace(str(tmp), str(target))
            packed += 1
            total_rows += rows
            if delete_old:
                shutil.rmtree(d, ignore_errors=True)
                deleted += 1
        elif tmp.exists():
            tmp.unlink()

    tag = "(dry-run) " if dry_run else ""
    msg = (
        f"Tick迁移{tag}完成：发现 {len(day_dirs)} 个旧日目录，"
        f"打包 {packed} / 跳过 {skipped}(已有打包)，共 {total_rows} 行"
    )
    if delete_old and not dry_run:
        msg += f"，删除旧目录 {deleted} 个"
    return msg + "。"


def _fmt_days(days: List[str], limit: int = 10) -> str:
    """格式化无数据日期列表（最多 limit 个）。"""
    head = ",".join(days[:limit])
    return head + ("…" if len(days) > limit else "")


# ======== Pipeline 标准接口 ========

def run(run_date: str | None = None, **kwargs) -> str:
    """每日任务：采集指定日期全部 A 股 tick → 当日打包 parquet。

    不传 run_date 时自动取上一交易日（当天 tick 经常无法获取，故默认昨日）。
    """
    if run_date is None or str(run_date).strip() == "":
        trade_date = minus_one_market_day(datetime.datetime.now())
    else:
        trade_date = normalize_trade_date(run_date)
    limit_stocks = kwargs.get("limit_stocks")

    if not is_market_day(trade_date):
        return f"{trade_date} 非交易日，Tick任务跳过。"

    r = _collect_one_day(trade_date, limit_stocks)
    if r.status == "skipped":
        return f"{trade_date} Tick当日文件已存在，跳过。"
    if r.status == "unavailable":
        return f"{trade_date} Tick无数据（可能已过QMT保留期），已写 .empty 标记。"
    return (
        f"{trade_date} Tick完成，{r.with_data}/{r.stocks} 只有数据，"
        f"{r.rows} 行 → 当日打包文件。"
    )


def run_backfill(start_date: str, end_date: str, limit_stocks: int | None = None) -> str:
    """补历史：逐交易日采集 tick，按日打包。"""
    from data_collect.utils.progress import DualProgress

    trading_days = date_range(start_date, end_date)
    written = skipped = unavailable = 0
    total_rows = 0
    unavailable_days: List[str] = []

    with DualProgress(trading_days) as dp:
        for d in dp.iter_days():
            r = _collect_one_day(d, limit_stocks, dp=dp)
            if r.status == "written":
                written += 1
                total_rows += r.rows
            elif r.status == "skipped":
                skipped += 1
            else:
                unavailable += 1
                unavailable_days.append(d)
            dp.log(f"{d}: {r.status} rows={r.rows}")

    message = (
        f"Tick补历史完成 ({start_date}~{end_date})，{len(trading_days)} 个交易日："
        f"{written} 写入 / {skipped} 跳过 / {unavailable} 无数据，共 {total_rows} 行。"
    )
    if unavailable_days:
        message += f" 无数据日(疑超保留期): {_fmt_days(unavailable_days)}"
    send_dingtalk(message)
    return message


def run_verify(start_date: str, end_date: str, **kwargs) -> str:
    """查漏补缺：检查每个交易日是否已有打包文件/无数据标记，补齐缺失日。"""
    limit_stocks = kwargs.get("limit_stocks")
    trading_days = date_range(start_date, end_date)

    missing = [d for d in trading_days if not _day_done(d)]
    if not missing:
        return f"Tick检查完成，{len(trading_days)} 个交易日均已就绪（含无数据标记）。"

    from data_collect.utils.progress import DualProgress

    written = unavailable = 0
    total_rows = 0
    unavailable_days: List[str] = []

    with DualProgress(missing) as dp:
        for d in dp.iter_days():
            r = _collect_one_day(d, limit_stocks, dp=dp)
            if r.status == "written":
                written += 1
                total_rows += r.rows
            else:  # unavailable（skipped 不会出现，因 missing 已排除 _day_done）
                unavailable += 1
                unavailable_days.append(d)
            dp.log(f"{d}: {r.status}")

    message = (
        f"Tick补缺完成，{len(missing)}/{len(trading_days)} 天缺失 → "
        f"{written} 补齐({total_rows} 行) / {unavailable} 无数据。"
    )
    if unavailable_days:
        message += f" 无数据日(疑超保留期): {_fmt_days(unavailable_days)}"
    return message


def run_evaluate(start_date: str, end_date: str, **kwargs) -> str:
    """评估 Tick 数据缺失：按交易日检查打包文件，输出缺失/无数据明细 CSV。"""
    trading_days = date_range(start_date, end_date)

    rows = []
    for d in trading_days:
        f = _day_file_path(d)
        if f.exists() and f.stat().st_size > 0:
            continue  # ok
        status = "unavailable" if _empty_marker_path(d).exists() else "missing"
        rows.append({"trade_date": d, "status": status})

    if not rows:
        return f"Tick评估完成 ({start_date}~{end_date})，{len(trading_days)} 个交易日数据完整。"

    df_missing = pd.DataFrame(rows)
    output = f"evaluate_tick_{start_date}_{end_date}.csv"
    df_missing.to_csv(output, index=False, encoding="utf-8-sig")

    miss = int((df_missing["status"] == "missing").sum())
    una = int((df_missing["status"] == "unavailable").sum())
    return (
        f"Tick评估完成 ({start_date}~{end_date})，{len(trading_days)} 个交易日："
        f"{miss} 缺失 / {una} 无数据，明细已输出: {output}"
    )
