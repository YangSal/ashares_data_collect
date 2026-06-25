"""CSV 导出模块。"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pandas as pd

from data_collect.config import get_export_config


def export_stock_csvs(
    df: pd.DataFrame, trade_date: str, base_dir: str | None = None
) -> Tuple[Path | None, int]:
    """
    将最终入库数据按股票导出为 CSV：base_dir/交易日/股票代码.csv
    返回：导出目录（None 表示未导出）、导出股票文件数。
    config 中 enable_csv=false 时跳过导出。
    """
    export_cfg = get_export_config()
    if not export_cfg.get("enable_csv", False):
        return None, 0

    if base_dir is None:
        base_dir = export_cfg.get("base_dir", "tempdata")

    export_dir = Path(base_dir) / trade_date
    export_dir.mkdir(parents=True, exist_ok=True)

    if df.empty:
        (export_dir / "_empty.csv").write_text("", encoding="utf-8")
        return export_dir, 0

    code_col = None
    if "stock_code" in df.columns:
        code_col = "stock_code"
    else:
        for col in df.columns:
            lower = str(col).lower()
            if "code" in lower or "代码" in str(col):
                code_col = col
                break
        if code_col is None and len(df.columns) >= 3:
            code_col = df.columns[2]

    if code_col is None:
        df.to_csv(export_dir / "all_data.csv", index=False, encoding="utf-8-sig")
        return export_dir, 1

    file_count = 0
    for stock_code, part in df.groupby(code_col, dropna=False):
        safe_code = str(stock_code).replace("/", "_").replace("\\", "_")
        out_file = export_dir / f"{safe_code}.csv"
        part.sort_values(by=[c for c in ["bar_time"] if c in part.columns]).to_csv(
            out_file, index=False, encoding="utf-8-sig",
        )
        file_count += 1
    return export_dir, file_count
