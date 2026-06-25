"""QMT 缓存清理任务

清空 `{qmt.install_dir}/userdata_mini/datadir` —— QMT 的本地行情数据缓存，会随历史下载
持续累积、占用磁盘。本项目的真实数据已落 PostgreSQL + Parquet 冷存，datadir 纯属可重下缓存，
定期清空回收磁盘空间（下次相关任务按需重新下载当天数据，非全量重下）。

QMT 运行时会锁定部分文件：清理时逐个删除，跳过被占用/无权限的文件并记日志，
只删 datadir 的内容、保留目录本身。
"""

from __future__ import annotations

import logging
from pathlib import Path

from data_collect.config import get_qmt_config

logger = logging.getLogger(__name__)

# datadir 相对 install_dir 的子路径
_CACHE_SUBPATH = ("userdata_mini", "datadir")


def _cache_dir() -> Path:
    """从配置定位要清理的缓存目录。未配置 qmt.install_dir 抛 RuntimeError。"""
    install_dir = get_qmt_config().get("install_dir")
    if not install_dir:
        raise RuntimeError("未配置 qmt.install_dir，无法定位 QMT 缓存目录")
    return Path(install_dir).joinpath(*_CACHE_SUBPATH)


def clean_cache(dry_run: bool = False) -> tuple[int, int, int]:
    """清空 datadir 内容（保留目录本身）。

    返回 (释放字节数, 删除文件数, 跳过文件数)。逐个删除、自底向上删空目录，
    跳过被占用/无权限的文件（QMT 运行中会锁部分文件）。
    """
    base = _cache_dir()
    if not base.exists():
        return 0, 0, 0

    freed = deleted = skipped = 0
    # 自底向上：先删文件，再删空目录
    for p in sorted(base.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if p.is_symlink() or p.is_file():
                size = p.stat().st_size
                if not dry_run:
                    p.unlink()
                freed += size
                deleted += 1
            elif p.is_dir() and not dry_run:
                try:
                    p.rmdir()  # 仅删空目录；非空（含被跳过文件）则忽略
                except OSError:
                    pass
        except Exception as exc:
            skipped += 1
            logger.warning(f"QMT缓存清理跳过 {p}: {exc!r}")
    return freed, deleted, skipped


def run(run_date: str | None = None, **kwargs) -> str:
    """Pipeline 标准接口：清空 QMT datadir 缓存。

    kwargs 支持 `dry_run=True`（只统计不删）。返回结果消息，由 pipeline 汇总发钉钉。
    """
    dry_run = bool(kwargs.get("dry_run", False))
    base = _cache_dir()
    if not base.exists():
        msg = f"QMT缓存清理：目录不存在，跳过（{base}）。"
        logger.info(msg)
        return msg

    freed, deleted, skipped = clean_cache(dry_run=dry_run)
    tag = "(dry-run) " if dry_run else ""
    msg = (
        f"QMT缓存清理{tag}完成：{base} 释放 {freed / 1024 / 1024:.1f} MB、删除 {deleted} 文件"
        + (f"、跳过占用 {skipped} 个" if skipped else "")
        + "。"
    )
    logger.info(msg)
    return msg
