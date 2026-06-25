"""公共重试策略。"""

import logging

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
    retry_if_exception_type,
)

logger = logging.getLogger(__name__)

# xtquant 数据拉取：重试3次，指数退避 1s→2s→4s
retry_xtquant = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)

# 数据库操作：重试3次，指数退避 2s→4s→8s
retry_db = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=16),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)

# 网络通知（钉钉等）：重试2次，固定等待2s，失败不抛异常
retry_notify = retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
