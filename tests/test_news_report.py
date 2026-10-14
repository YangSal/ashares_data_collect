"""18:00 新闻采集日报：全 mock，不连 OpenSearch/钉钉/SMTP。"""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from data_collect.jobs import news_report as nr


SH = ZoneInfo("Asia/Shanghai")
START = datetime(2026, 8, 26, 18, 0, tzinfo=SH)
END = datetime(2026, 8, 27, 18, 0, tzinfo=SH)


def _agg_response():
    return {
        "hits": {"total": {"value": 18, "relation": "eq"}},
        "aggregations": {
            "by_channel": {"buckets": [
                {"key": "flash", "doc_count": 12},
                {"key": "policy", "doc_count": 6},
            ]},
            "by_source": {"buckets": [
                {"key": "cls", "doc_count": 10},
                {"key": "jiemian", "doc_count": 8},
            ]},
        },
    }


def _events():
    return [
        {
            "id": 1, "event_time": "2026-08-27T01:00:00+08:00",
            "event_type": "task_result", "severity": "info",
            "pipeline": "news_hourly", "task": "news_policy", "source": None,
            "success": 1, "skipped": 0, "duration": 3.2,
            "summary": "新增 6", "details": "新增 6", "collector_node": "news-node",
        },
        {
            "id": 2, "event_time": "2026-08-27T02:00:00+08:00",
            "event_type": "task_result", "severity": "error",
            "pipeline": "news_hourly", "task": "news_regulator", "source": "jiemian",
            "success": 0, "skipped": 0, "duration": 9.5,
            "summary": "RuntimeError: RSSHub HTTP 503",
            "details": "RuntimeError: RSSHub HTTP 503\nTraceback full line",
            "collector_node": "news-node",
        },
        {
            "id": 4, "event_time": "2026-08-27T02:30:00+08:00",
            "event_type": "source_health_transition", "severity": "info",
            "pipeline": None, "task": None, "source": "latepost",
            "success": None, "skipped": None, "duration": None,
            "summary": "smoke: dead -> ok", "details": "recovered",
            "collector_node": "news-node",
        },
        {
            "id": 3, "event_time": "2026-08-27T03:00:00+08:00",
            "event_type": "alert", "severity": "warning",
            "pipeline": "news_hourly", "task": "news_regulator", "source": "jiemian",
            "success": None, "skipped": None, "duration": None,
            "summary": "RuntimeError: RSSHub HTTP 503",
            "details": "RuntimeError: RSSHub HTTP 503\nsecond occurrence detail",
            "collector_node": "news-node",
        },
    ]


def _health():
    return {
        "summary": {
            "healthy": 2, "failover": 1, "warning": 1,
            "failed": 0, "unknown": 1,
        },
        "sources": [
            {"source": "healthy_media", "enabled": True,
             "profile": "continuous", "overall": "healthy",
             "freshness": "fresh", "smoke": "ok",
             "transport": "primary", "last_data_time": "2026-08-27 17:55:00",
             "age_hours": 0.1, "detail": ""},
            {"source": "healthy_event", "enabled": True,
             "profile": "event", "overall": "healthy",
             "freshness": "not_applicable", "smoke": "ok",
             "transport": "not_applicable", "last_data_time": None,
             "age_hours": None, "detail": ""},
            {"source": "em_report_stock", "enabled": True,
             "profile": "business", "overall": "failover",
             "freshness": "fresh", "smoke": "ok", "transport": "failover",
             "last_data_time": "2026-08-27 17:00:00", "age_hours": 1,
             "detail": "RSSHub failover"},
            {"source": "thepaper", "enabled": True,
             "profile": "continuous", "overall": "warning",
             "freshness": "stale", "smoke": "ok",
             "transport": "not_applicable",
             "last_data_time": "2026-08-26 12:00:00", "age_hours": 30,
             "detail": ""},
            {"source": "unknown_source", "enabled": True,
             "profile": "continuous", "overall": "unknown",
             "freshness": "unknown", "smoke": "not_run",
             "transport": "not_applicable", "last_data_time": None,
             "age_hours": None, "detail": ""},
            {"source": "latepost", "enabled": False,
             "profile": "continuous", "overall": "disabled",
             "freshness": "unknown", "smoke": "ok",
             "transport": "not_applicable", "last_data_time": None,
             "age_hours": None, "detail": "recovered"},
        ],
        "recovered_candidates": ["latepost"],
        "transport": {"primary": 20, "failover": 3, "failed": 0},
    }


def test_collection_metrics_use_fetch_time_half_open_window(monkeypatch):
    searches = []
    responses = [
        _agg_response(),
        {"hits": {"total": {"value": 4}, "hits": [
            {"_source": {"fetch_time": "2026-08-26 19:00:00"}}
        ]}},
    ]
    monkeypatch.setattr(nr, "_get_client", lambda: object())
    monkeypatch.setattr(nr, "_search_raw",
                        lambda client, body: searches.append(body) or responses.pop(0))

    metrics = nr._collection_metrics(START, END)

    query_range = searches[0]["query"]["range"]["fetch_time"]
    assert query_range == {"gte": "2026-08-26 18:00:00", "lt": "2026-08-27 18:00:00"}
    assert searches[0]["aggs"]["by_source"]["terms"]["field"] == "source"
    assert metrics["total"] == 18
    assert metrics["channels"] == {"flash": 12, "policy": 6}
    assert metrics["sources"] == {"cls": 10, "jiemian": 8}
    assert metrics["pending"] == 4
    assert metrics["pending_oldest"] == "2026-08-26 19:00:00"


def test_report_contains_task_stats_deduplicated_errors_and_full_timeline(monkeypatch):
    monkeypatch.setattr(nr, "get_news_config",
                        lambda: {"digest": {"dingtalk_max_chars": 18000}})
    metrics = {
        "total": 18, "channels": {"flash": 12, "policy": 6},
        "sources": {"cls": 10, "jiemian": 8},
        "pending": 4, "pending_oldest": "2026-08-26 19:00:00",
    }

    dingtalk, email = nr._build_report(
        START, END, metrics, _events(), health=_health())

    assert "2026-08-26 18:00" in dingtalk and "2026-08-27 18:00" in dingtalk
    assert "采集 18" in dingtalk and "flash 12" in dingtalk and "jiemian 8" in dingtalk
    assert "任务 2次" in dingtalk and "1成功" in dingtalk and "1失败" in dingtalk
    assert "RSSHub HTTP 503" in dingtalk and "×2" in dingtalk
    assert "来源健康 5启用：2健康，1降级，1异常，0失败，1未知" in dingtalk
    assert "thepaper" in dingtalk and "em_report_stock" in dingtalk
    assert "healthy_media" not in dingtalk
    assert "RSSHub：主实例 20，备用成功 3，失败 0" in dingtalk
    assert "恢复候选：latepost" in dingtalk
    assert len(dingtalk) <= 18000
    assert "完整事件时间线" in email
    assert "来源健康全量明细" in email
    assert "healthy_media" in email and "healthy_event" in email
    assert "smoke: dead -> ok" in email
    assert "Traceback full line" in email
    assert "second occurrence detail" in email
    assert email.index("2026-08-27T01:00") < email.index("2026-08-27T03:00")


def test_source_health_metrics_loads_latest_and_window_observations(monkeypatch):
    sources = [SimpleNamespace(id="caixin")]
    latest = [{"source": "caixin", "check_type": "smoke"}]
    window = [{"source": "caixin", "check_type": "transport"}]
    captured = {}
    monkeypatch.setattr(nr.source_registry, "load_all", lambda: sources)
    monkeypatch.setattr(
        nr.news_digest, "list_latest_source_observations", lambda: latest)
    monkeypatch.setattr(
        nr.news_digest, "list_source_observations",
        lambda start, end: window)
    monkeypatch.setattr(
        nr.news_health, "build_health_snapshot",
        lambda source_rows, latest_rows, window_rows, now: captured.update(
            sources=source_rows, latest=latest_rows, window=window_rows,
            now=now) or {"summary": {}})

    result = nr._source_health_metrics(START, END, END)

    assert result == {"summary": {}}
    assert captured == {
        "sources": sources, "latest": latest, "window": window, "now": END}


def test_source_health_metrics_returns_query_error(monkeypatch, caplog):
    monkeypatch.setattr(
        nr.source_registry, "load_all",
        lambda: (_ for _ in ()).throw(OSError(
            "ledger http://alice:hunter2@host:9200/news?token=abc123 unavailable")))

    error = nr._source_health_metrics(START, END, END)["query_error"]

    assert "alice" not in error
    assert "hunter2" not in error
    assert "abc123" not in error
    assert "http://host:9200/news" in error
    assert "alice" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "abc123" not in caplog.text


@pytest.fixture
def delivery_env(monkeypatch):
    state = {"dingtalk_status": "pending", "email_status": "pending"}
    calls = SimpleNamespace(
        dingtalk=[], email=[], marks=[], cleaned=[], cleaned_health=[])
    monkeypatch.setattr(nr, "_now", lambda: END)
    monkeypatch.setattr(nr.news_digest, "list_events", lambda start, end: _events())
    monkeypatch.setattr(nr.news_digest, "get_delivery", lambda end: dict(state))

    def mark(end, channel, status, error=None):
        state[f"{channel}_status"] = status
        state[f"{channel}_error"] = error
        calls.marks.append((channel, status, error))

    monkeypatch.setattr(nr.news_digest, "mark_delivery", mark)
    monkeypatch.setattr(nr.news_digest, "cleanup_events",
                        lambda before: calls.cleaned.append(before) or 0)
    monkeypatch.setattr(nr.news_digest, "cleanup_source_observations",
                        lambda before: calls.cleaned_health.append(before) or 0)
    monkeypatch.setattr(nr.news_digest, "record_event", lambda *a, **kw: 1)
    monkeypatch.setattr(nr, "_collection_metrics", lambda start, end: {
        "total": 18, "channels": {"flash": 12}, "sources": {"cls": 18},
        "pending": 0, "pending_oldest": None,
    })
    monkeypatch.setattr(nr, "_source_health_metrics",
                        lambda start, end, now: _health())
    monkeypatch.setattr(nr, "get_dingtalk_config",
                        lambda: {"enabled": True, "webhook_token": "token"})
    monkeypatch.setattr(nr, "get_news_config", lambda: {"digest": {
        "email_recipient": "839833678@qq.com", "retention_days": 30,
        "dingtalk_max_chars": 18000,
    }})
    monkeypatch.setattr(nr, "_load_config", lambda: {"wave3": {"email": {
        "smtp_server": "smtp.qq.com", "smtp_port": 465,
        "sender": "sender@qq.com", "app_key": "secret",
        "recipients": ["must-not-use@example.com"],
    }}})
    monkeypatch.setattr(nr, "send_dingtalk_immediate",
                        lambda message: calls.dingtalk.append(message) or True)
    monkeypatch.setattr(nr, "send_email",
                        lambda subject, body, cfg, recipients: calls.email.append(
                            (subject, body, cfg, recipients)))
    return state, calls


def test_run_sends_each_channel_once_to_fixed_recipient(delivery_env):
    state, calls = delivery_env
    summary = nr.run()
    assert "采集 18" in summary
    assert len(calls.dingtalk) == 1
    assert len(calls.email) == 1
    assert calls.email[0][3] == ["839833678@qq.com"]
    assert state["dingtalk_status"] == "sent"
    assert state["email_status"] == "sent"
    assert calls.cleaned
    assert calls.cleaned_health == calls.cleaned


def test_retry_only_resends_failed_email(delivery_env, monkeypatch):
    state, calls = delivery_env

    def fail_email(*args):
        raise RuntimeError("smtp timeout")

    monkeypatch.setattr(nr, "send_email", fail_email)
    with pytest.raises(RuntimeError, match="email"):
        nr.run()
    assert state["dingtalk_status"] == "sent"
    assert state["email_status"] == "failed"
    assert len(calls.dingtalk) == 1

    monkeypatch.setattr(nr, "send_email",
                        lambda subject, body, cfg, recipients: calls.email.append(
                            (subject, body, cfg, recipients)))
    nr.run()
    assert len(calls.dingtalk) == 1
    assert len(calls.email) == 1
    assert state["email_status"] == "sent"


def test_retry_only_resends_failed_dingtalk(delivery_env, monkeypatch):
    state, calls = delivery_env

    def fail_dingtalk(message):
        raise RuntimeError("webhook timeout")

    monkeypatch.setattr(nr, "send_dingtalk_immediate", fail_dingtalk)
    with pytest.raises(RuntimeError, match="dingtalk"):
        nr.run()
    assert state["dingtalk_status"] == "failed"
    assert state["email_status"] == "sent"
    assert len(calls.email) == 1

    monkeypatch.setattr(nr, "send_dingtalk_immediate",
                        lambda message: calls.dingtalk.append(message) or True)
    nr.run()
    assert len(calls.dingtalk) == 1
    assert len(calls.email) == 1
    assert state["dingtalk_status"] == "sent"


def test_disabled_node_sends_neither_channel(delivery_env, monkeypatch):
    state, calls = delivery_env
    monkeypatch.setattr(nr, "get_dingtalk_config", lambda: {"enabled": False})
    summary = nr.run()
    assert "静默节点" in summary
    assert calls.dingtalk == [] and calls.email == []
    assert state["dingtalk_status"] == "pending"


def test_opensearch_failure_is_visible_in_both_reports(delivery_env, monkeypatch):
    state, calls = delivery_env
    monkeypatch.setattr(nr, "_collection_metrics",
                        lambda start, end: (_ for _ in ()).throw(RuntimeError("cluster timeout")))
    nr.run()
    assert "OpenSearch查询失败" in calls.dingtalk[0]
    assert "cluster timeout" in calls.email[0][1]


def test_health_query_failure_is_visible_without_blocking_delivery(
        delivery_env, monkeypatch):
    state, calls = delivery_env
    monkeypatch.setattr(
        nr, "_source_health_metrics",
        lambda start, end, now: {"query_error": "OSError: ledger locked"})

    nr.run()

    assert "来源健康查询失败" in calls.dingtalk[0]
    assert "ledger locked" in calls.email[0][1]
    assert state["dingtalk_status"] == "sent"
    assert state["email_status"] == "sent"
