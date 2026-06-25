"""手动运行 QMT 缓存清理（清空 {qmt.install_dir}/userdata_mini/datadir）。

定时任务由 pipeline `monthly_qmt_clean`（每月1号23:00）自动执行；此脚本供手动 / dry-run。

用法：
  # 先 dry-run 看会删多少、释放多少（不实际删除）
  python tools/clean_qmt_cache.py --dry-run

  # 实际清理
  python tools/clean_qmt_cache.py
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# 允许从项目根目录直接 `python tools/clean_qmt_cache.py` 运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="清理 QMT datadir 缓存")
    parser.add_argument("--dry-run", action="store_true", help="只统计不删除")
    args = parser.parse_args()

    from data_collect.jobs.clean_qmt_cache import run

    print(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
