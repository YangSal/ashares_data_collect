"""通知模块（钉钉等）。"""

from __future__ import annotations

import logging

import requests

from data_collect.config import get_dingtalk_config
from data_collect.utils.retry import retry_notify

logger = logging.getLogger(__name__)


def ensure_ding_prefix(message: str) -> str:
    """确保消息包含关键短语。"""
    config = get_dingtalk_config()
    prefix = config.get("message_prefix", "白白胖胖说")
    if prefix in message:
        return message
    return f"{prefix}：{message}"


@retry_notify
def _post_dingtalk(url: str, payload: dict) -> None:
    """实际发送请求（带重试）。"""
    response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()


def send_dingtalk(message: str) -> None:
    """发送钉钉文本消息。重试耗尽后只记日志，不影响主流程。"""
    config = get_dingtalk_config()
    token = config["webhook_token"]
    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
    payload = {
        "msgtype": "text",
        "text": {"content": ensure_ding_prefix(message)},
    }
    try:
        _post_dingtalk(url, payload)
    except Exception as exc:
        logger.error(f"钉钉通知发送失败（重试耗尽）: {exc}")
