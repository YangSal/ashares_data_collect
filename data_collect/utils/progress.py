"""进度显示工具：基于 tqdm，每天一个进度条。

被 a_share_minute, a_share_tick, wave3 的 backfill/verify 使用。
a_share_daily 直接使用 tqdm（不经过此模块）。
"""

from __future__ import annotations

import sys
from typing import List, Sequence

from tqdm import tqdm


class DualProgress:
    """每天一个 tqdm 进度条，desc 显示日期进度 N/M。"""

    def __init__(self, days: Sequence[str]):
        self._days = days
        self._day_idx = 0
        self._pbar: tqdm | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._pbar:
            self._pbar.close()
            self._pbar = None

    def iter_days(self):
        for i, day in enumerate(self._days):
            self._day_idx = i + 1
            yield day

    def iter_batch_chunks(self, codes: List[str], batch_size: int, desc: str = ""):
        if self._pbar:
            self._pbar.close()

        label = f"{self._day_idx}/{len(self._days)}"
        if desc:
            label = f"{label} {desc}"

        self._pbar = tqdm(
            total=len(codes),
            desc=label,
            unit="股",
            ncols=90,
            leave=True,
            file=sys.stdout,
        )

        for i in range(0, len(codes), batch_size):
            batch = codes[i : i + batch_size]
            yield batch
            self._pbar.update(len(batch))

        self._pbar.close()
        self._pbar = None

    def log(self, msg: str) -> None:
        tqdm.write(f"  {msg}", file=sys.stdout)
