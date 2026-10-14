"""通知模块（钉钉等）。"""

from __future__ import annotations

import logging
import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from data_collect.config import get_dingtalk_config
from data_collect.utils import news_digest
from data_collect.utils.retry import retry_notify

logger = logging.getLogger(__name__)


def ensure_ding_prefix(message: str) -> str:
    """确保消息包含关键短语。"""
    config = get_dingtalk_config()
    prefix = config.get("message_prefix", "XXXX")
    if prefix in message:
        return message
    return f"{prefix}：{message}"


@retry_notify
def _post_dingtalk(url: str, payload: dict) -> None:
    """实际发送请求（带重试）。"""
    response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()
    try:
        result = response.json()
    except Exception as exc:
        raise RuntimeError("钉钉返回非 JSON 响应") from exc
    if int(result.get("errcode", -1)) != 0:
        raise RuntimeError(
            f"钉钉业务错误 errcode={result.get('errcode')}: {result.get('errmsg', '')}"
        )


def send_dingtalk_immediate(message: str) -> bool:
    """立即发送钉钉，并向调用者暴露网络/配置错误。

    仅供 18:00 汇总投递等需要确认送达状态的调用方使用；不会被新闻汇总模式改道。
    `dingtalk.enabled=false` 返回 False，表示当前节点是静默节点。
    """
    config = get_dingtalk_config()
    if not config.get("enabled", True):
        logger.info("钉钉已禁用(dingtalk.enabled=false)，跳过立即发送")
        return False
    token = config["webhook_token"]
    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
    payload = {
        "msgtype": "text",
        "text": {"content": ensure_ding_prefix(message)},
    }
    _post_dingtalk(url, payload)
    return True


def send_dingtalk(message: str) -> None:
    """发送钉钉文本消息。重试耗尽后只记日志，不影响主流程。

    `dingtalk.enabled: false`（多节点 Phase A2，节点经 config.local.yaml 覆盖）
    → 静默节点：只记日志不发送——采集节点关闭后，告警出口收敛到监控节点。
    """
    if news_digest.is_digest_mode():
        try:
            details = str(message or "")
            summary = details.splitlines()[0] if details else ""
            news_digest.record_event(
                "alert", "warning", summary=summary, details=details,
            )
        except Exception:
            logger.error("新闻告警写入汇总账本失败", exc_info=True)
        return
    try:
        send_dingtalk_immediate(message)
    except Exception as exc:
        logger.error(f"钉钉通知发送失败（重试耗尽）: {exc}")


def send_email(
    subject: str,
    body: str,
    smtp_config: dict,
    recipients: list[str] | tuple[str, ...],
    *,
    attachment_path: str | None = None,
) -> None:
    """通过 SMTP_SSL 发送 UTF-8 文本邮件，可附一个文件。

    本函数不吞异常，便于新闻日报记录独立投递状态；wave3 等希望通知失败不影响
    主流程的调用方应在自身边界捕获异常。
    """
    sender = str(smtp_config.get("sender") or "").strip()
    app_key = str(smtp_config.get("app_key") or "").strip()
    recipient_list = [str(item).strip() for item in recipients if str(item).strip()]
    if not sender or not app_key or not recipient_list:
        raise ValueError("邮件配置不完整：sender/app_key/recipients 必填")

    smtp_server = smtp_config.get("smtp_server", "smtp.qq.com")
    smtp_port = int(smtp_config.get("smtp_port", 465))
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ",".join(recipient_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachment_path and os.path.isfile(attachment_path):
        with open(attachment_path, "rb") as file_obj:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(file_obj.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition", "attachment",
            filename=("utf-8", "", os.path.basename(attachment_path)),
        )
        msg.attach(part)

    with smtplib.SMTP_SSL(smtp_server, smtp_port) as smtp:
        smtp.login(sender, app_key)
        smtp.sendmail(sender, recipient_list, msg.as_string())


def guarded_send(message: str, send=None) -> bool:
    """守护式发送：包裹 send_dingtalk，成功返回 True、异常返回 False（仅记日志）。

    send_dingtalk 自身已吞掉 POST 错误，本函数主要挡配置/前缀等异常，并提供
    bool 返回值（快讯断流告警状态机据此判定是否置 alerted）。`send` 可注入调用方
    模块级的 send_dingtalk（保留其 monkeypatch 点），缺省用本模块的 send_dingtalk。
    """
    if send is None:
        send = send_dingtalk
    try:
        send(message)
        return True
    except Exception:
        logger.warning(f"钉钉发送失败（不阻塞主流程）: {message[:60]}", exc_info=True)
        return False
