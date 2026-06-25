"""xtquant 公共工具：懒加载 xtdata、获取A股股票列表、批量下载等。"""

from __future__ import annotations

import importlib
import logging
from typing import List

logger = logging.getLogger(__name__)


_xtdata_loaded = False


def require_xtdata():
    """懒加载 xtquant.xtdata 模块，首次加载后禁用 hello 打印。"""
    global _xtdata_loaded
    try:
        xtdata_module = importlib.import_module("xtquant.xtdata")
    except Exception:
        try:
            xtquant_module = importlib.import_module("xtquant")
            xtdata_module = getattr(xtquant_module, "xtdata")
        except Exception as inner_exc:
            raise ImportError("缺少 xtquant，请确认QMT环境可用") from inner_exc

    if not _xtdata_loaded:
        try:
            xtdata_module.enable_hello = False
        except Exception:
            pass
        _xtdata_loaded = True

    return xtdata_module


def get_a_share_codes() -> List[str]:
    """获取A股股票代码列表。"""
    xtdata = require_xtdata()
    try:
        codes = xtdata.get_stock_list_in_sector("沪深A股") or []
        if codes:
            return sorted(set(codes))
    except Exception as exc:
        logger.warning(f"读取 沪深A股 失败: {exc}")

    merged = set()
    for sector in ["上证A股", "深证A股", "北证A股"]:
        try:
            part = xtdata.get_stock_list_in_sector(sector) or []
            merged.update(part)
        except Exception as exc:
            logger.warning(f"读取板块 {sector} 失败: {exc}")

    return sorted(merged)


def download_history_with_retry(
    stock_list: List[str],
    period: str,
    start_time: str,
    end_time: str,
    max_retries: int = 3,
    **kwargs,
) -> None:
    """下载历史数据到本地，直接调用 xtquant 下载（内部自动处理缓存）。"""
    xtdata = require_xtdata()

    for attempt in range(1, max_retries + 1):
        try:
            xtdata.download_history_data2(
                stock_list=stock_list, period=period,
                start_time=start_time, end_time=end_time,
            )
            return
        except Exception as exc:
            action = "重试" if attempt < max_retries else "跳过"
            logger.warning(
                f"下载异常 (第{attempt}/{max_retries}次, "
                f"{len(stock_list)}只{period}): {exc}, {action}..."
            )
