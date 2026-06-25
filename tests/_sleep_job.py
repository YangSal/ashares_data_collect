"""测试用 job 模块：sleep N 秒后返回，用于验证 pipeline 的 timeout/retry。"""

import time


def run(seconds: float = 0.1, **kwargs) -> str:
    time.sleep(seconds)
    return f"slept {seconds}s"


def run_flaky(fail_times: int, marker_path: str, **kwargs) -> str:
    """读取 marker_path 中的计数器，前 fail_times 次抛错，之后成功。

    用文件当计数器是因为 ProcessPoolExecutor 每次重启子进程时
    进程内变量不持久，需要外部存储跨进程计数。
    """
    import os
    count = 0
    if os.path.exists(marker_path):
        with open(marker_path, "r") as f:
            count = int(f.read().strip() or "0")
    count += 1
    with open(marker_path, "w") as f:
        f.write(str(count))

    if count <= fail_times:
        raise RuntimeError(f"flaky failure {count}/{fail_times}")
    return f"ok after {count} attempts"
