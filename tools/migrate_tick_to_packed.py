"""一次性迁移脚本：tick 旧版"每股一文件" → "当日打包"。

将 base_dir/YYYY/MM/DD/{code}.parquet 合并为 base_dir/YYYY/MM/DD.parquet。
读取 config.yaml 的 tick_storage.base_dir（即你的 Z: 归档路径）。

用法：
  # 先 dry-run 看会处理哪些日子、多少行（不写入、不删除）
  python tools/migrate_tick_to_packed.py --dry-run

  # 实际打包（旧目录保留，安全）
  python tools/migrate_tick_to_packed.py

  # 打包并删除旧的每股目录（确认无误后再用）
  python tools/migrate_tick_to_packed.py --delete-old
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# 允许从项目根目录直接 `python tools/migrate_tick_to_packed.py` 运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="tick 旧版每股文件 → 当日打包 迁移")
    parser.add_argument("--delete-old", action="store_true", help="打包成功后删除旧的每股目录")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args()

    from data_collect.jobs.a_share_tick import migrate_per_stock_to_packed

    print(migrate_per_stock_to_packed(delete_old=args.delete_old, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
