from data_collect.utils import notify
from data_collect.utils.notify import ensure_ding_prefix


def test_ensure_ding_prefix_adds_prefix():
    assert ensure_ding_prefix("测试消息").startswith("XXXX")


def test_ensure_ding_prefix_keeps_existing():
    assert ensure_ding_prefix("XXXX：原消息") == "XXXX：原消息"


def test_send_dingtalk_disabled_does_not_post(monkeypatch):
    """dingtalk.enabled: false → 只记日志，绝不 POST（多节点 Phase A2 静默节点）。"""
    posts = []
    monkeypatch.setattr(notify.news_digest, "is_digest_mode", lambda: False)
    monkeypatch.setattr(notify, "get_dingtalk_config",
                        lambda: {"enabled": False, "webhook_token": "t",
                                 "message_prefix": "XXXX"})
    monkeypatch.setattr(notify, "_post_dingtalk",
                        lambda url, payload: posts.append(payload))
    notify.send_dingtalk("测试消息")
    assert posts == []


def test_send_dingtalk_default_enabled_posts(monkeypatch):
    """缺省（无 enabled 键）行为不变：照常发送。"""
    posts = []
    monkeypatch.setattr(notify.news_digest, "is_digest_mode", lambda: False)
    monkeypatch.setattr(notify, "get_dingtalk_config",
                        lambda: {"webhook_token": "t", "message_prefix": "XXXX"})
    monkeypatch.setattr(notify, "_post_dingtalk",
                        lambda url, payload: posts.append(payload))
    notify.send_dingtalk("测试消息")
    assert len(posts) == 1
    assert "XXXX" in posts[0]["text"]["content"]


def test_news_digest_mode_spools_alert_instead_of_posting(monkeypatch):
    posts = []
    events = []
    monkeypatch.setattr(notify.news_digest, "is_digest_mode", lambda: True)
    monkeypatch.setattr(notify.news_digest, "record_event",
                        lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(notify, "_post_dingtalk",
                        lambda url, payload: posts.append(payload))

    notify.send_dingtalk("监管源失败\nRuntimeError: 503")

    assert posts == []
    assert events[0][0][:2] == ("alert", "warning")
    assert events[0][1]["summary"] == "监管源失败"
    assert "RuntimeError: 503" in events[0][1]["details"]


def test_immediate_dingtalk_bypasses_digest_mode_and_exposes_error(monkeypatch):
    monkeypatch.setattr(notify.news_digest, "is_digest_mode", lambda: True)
    monkeypatch.setattr(notify, "get_dingtalk_config",
                        lambda: {"enabled": True, "webhook_token": "t",
                                 "message_prefix": "XXXX"})

    def fail_post(url, payload):
        raise RuntimeError("webhook timeout")

    monkeypatch.setattr(notify, "_post_dingtalk", fail_post)

    import pytest
    with pytest.raises(RuntimeError, match="webhook timeout"):
        notify.send_dingtalk_immediate("日报")


def test_send_email_uses_explicit_recipient_and_attachment(monkeypatch, tmp_path):
    calls = {}
    attachment = tmp_path / "result.txt"
    attachment.write_text("payload", encoding="utf-8")

    class FakeSMTP:
        def __init__(self, server, port):
            calls["endpoint"] = (server, port)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, sender, app_key):
            calls["login"] = (sender, app_key)

        def sendmail(self, sender, recipients, message):
            calls["send"] = (sender, recipients, message)

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)

    notify.send_email(
        "新闻日报", "完整详情",
        {"smtp_server": "smtp.qq.com", "smtp_port": 465,
         "sender": "sender@qq.com", "app_key": "secret"},
        ["839833678@qq.com"],
        attachment_path=str(attachment),
    )

    assert calls["endpoint"] == ("smtp.qq.com", 465)
    assert calls["login"] == ("sender@qq.com", "secret")
    assert calls["send"][0:2] == ("sender@qq.com", ["839833678@qq.com"])
    assert "839833678@qq.com" in calls["send"][2]
    assert "result.txt" in calls["send"][2]


def test_post_dingtalk_rejects_http_200_business_error(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errcode": 310000, "errmsg": "keywords not in content"}

    monkeypatch.setattr(notify.requests, "post", lambda *a, **kw: FakeResponse())

    import pytest
    with pytest.raises(RuntimeError, match="310000"):
        notify._post_dingtalk.__wrapped__("https://example.invalid", {})
