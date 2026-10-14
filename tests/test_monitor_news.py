"""tools/monitor_news.py 纯函数测试（不触网络；OS 查询函数不在此测）。"""
import datetime
import importlib.util
import os

from data_collect.utils.source_registry import Source

# tools/ 不是包，按路径加载模块（同 test_health_check_sources 先例）
_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "monitor_news.py")
_spec = importlib.util.spec_from_file_location("monitor_news", _PATH)
mn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mn)

_TUE = datetime.datetime(2026, 7, 28, 18, 0, 0)     # 周二


def _source(source_id, profile):
    return Source(id=source_id, adapter="rss", channel="media",
                  job="news_regulator", health_profile=profile)


def test_check_sources_uses_profiles_and_records_each_observation(monkeypatch):
    sources = [
        _source("event", "event"),
        _source("fresh", "continuous"),
        _source("stale", "continuous"),
    ]
    latest = {
        "event": None,
        "fresh": "2026-07-28 12:00:00",
        "stale": "2026-07-27 12:00:00",
    }
    recorded = []
    monkeypatch.setattr(mn, "latest_fetch_time",
                        lambda client, source_id: latest[source_id])
    monkeypatch.setattr(
        mn.news_health, "observe",
        lambda source, check_type, status, **kwargs:
        recorded.append((source, check_type, status, kwargs)) or len(recorded))

    rows = mn.check_sources(object(), sources, _TUE)

    assert [(row["source"], row["status"]) for row in rows] == [
        ("event", "not_applicable"),
        ("fresh", "fresh"),
        ("stale", "stale"),
    ]
    assert [item[:3] for item in recorded] == [
        ("event", "freshness", "not_applicable"),
        ("fresh", "freshness", "fresh"),
        ("stale", "freshness", "stale"),
    ]
    assert rows[2]["age_hours"] == 30.0


def test_check_sources_isolates_one_source_query_failure(monkeypatch):
    sources = [_source("broken", "continuous"),
               _source("fresh", "continuous")]
    monkeypatch.setattr(
        mn, "latest_fetch_time",
        lambda client, source_id: (_ for _ in ()).throw(RuntimeError("OS down"))
        if source_id == "broken" else "2026-07-28 12:00:00")
    monkeypatch.setattr(mn.news_health, "observe", lambda *args, **kwargs: 1)

    rows = mn.check_sources(object(), sources, _TUE)

    assert [(row["source"], row["status"]) for row in rows] == [
        ("broken", "unknown"), ("fresh", "fresh")]


def test_parse_channel_counts():
    resp = {"hits": {"total": {"value": 132}},
            "aggregations": {"by_channel": {"buckets": [
                {"key": "flash", "doc_count": 90},
                {"key": "stock", "doc_count": 30},
                {"key": "policy", "doc_count": 12}]}}}
    total, counts = mn.parse_channel_counts(resp)
    assert total == 132
    assert counts == {"flash": 90, "stock": 30, "policy": 12}


def test_parse_channel_counts_error_payload_raises():
    import pytest
    with pytest.raises(LookupError):
        mn.parse_channel_counts({"error": {"type": "index_not_found_exception"}})


def test_build_report_normal_and_stale():
    ok = mn.build_report(_TUE, 1, 132, {"flash": 90, "stock": 30},
                         stale=[], never=[], pending=45, oldest_minutes=32)
    assert ok == ("新闻小时报 07-28 18:00: 近1h 入库 132 | flash 90 stock 30 | "
                  "断流: 无 | pending 45(最老 32 分)")
    bad = mn.build_report(_TUE, 1, 0, {}, stale=[("csrc", 19.4)],
                          never=["latepost"], pending=0, oldest_minutes=None,
                          vec_deferred=True)
    assert "近1h 入库 0 | 无入库" in bad
    assert "断流: csrc(19h) | 无文档源: latepost" in bad
    assert "pending 0(向量已禁用,留待补算)" in bad


def test_is_abnormal():
    assert mn.is_abnormal([("csrc", 20.0)], 100) is True    # 有断流
    assert mn.is_abnormal([], 0) is True                     # 零入库
    assert mn.is_abnormal([], 100) is False


def test_main_legacy_flags_only_print_and_write_ledger(
        monkeypatch, capsys):
    from data_collect import config
    from data_collect.utils import news_common, notify, opensearch_utils
    from data_collect.utils import source_registry

    observations = []
    monkeypatch.setattr(opensearch_utils, "get_client", lambda: object())
    monkeypatch.setattr(mn, "channel_counts",
                        lambda client, since: (5, {"media": 5}))
    monkeypatch.setattr(mn, "latest_fetch_time",
                        lambda client, source_id: "2026-07-28 12:00:00")
    monkeypatch.setattr(source_registry, "load_all",
                        lambda: [_source("caixin", "continuous")])
    monkeypatch.setattr(mn, "pending_backlog", lambda client: (0, None))
    monkeypatch.setattr(news_common, "now", lambda: _TUE)
    monkeypatch.setattr(config, "get_news_config",
                        lambda: {"embedding": {"enabled": True}})
    monkeypatch.setattr(
        mn.news_health, "observe",
        lambda source, check_type, status, **kwargs:
        observations.append((source, check_type, status)) or len(observations))
    monkeypatch.setattr(
        notify, "guarded_send",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("监控工具不得外发钉钉")))

    assert mn.main(["--quiet"]) == 0
    assert mn.main(["--no-send"]) == 0

    assert observations == [
        ("caixin", "freshness", "fresh"),
        ("__all__", "freshness", "fresh"),
        ("caixin", "freshness", "fresh"),
        ("__all__", "freshness", "fresh"),
    ]
    assert "只打印并写18:00账本，不外发" in capsys.readouterr().out


def test_main_records_global_query_error_without_synthetic_source_rows(
        monkeypatch):
    from data_collect.utils import news_common, opensearch_utils

    events = []
    observations = []
    monkeypatch.setattr(opensearch_utils, "get_client", lambda: object())
    monkeypatch.setattr(
        mn, "channel_counts",
        lambda client, since: (_ for _ in ()).throw(ConnectionError("OS down")))
    monkeypatch.setattr(news_common, "now", lambda: _TUE)
    monkeypatch.setattr(
        mn.news_digest, "record_event",
        lambda *args, **kwargs: events.append((args, kwargs)) or 1)
    monkeypatch.setattr(
        mn.news_health, "observe",
        lambda *args, **kwargs: observations.append((args, kwargs)) or 1)

    assert mn.main([]) == 1

    assert observations == []
    assert len(events) == 1
    assert events[0][1]["source"] == "opensearch"


def test_main_returns_nonzero_when_observation_ledger_write_fails(monkeypatch):
    from data_collect import config
    from data_collect.utils import news_common, opensearch_utils, source_registry

    monkeypatch.setattr(opensearch_utils, "get_client", lambda: object())
    monkeypatch.setattr(mn, "channel_counts", lambda client, since: (5, {}))
    monkeypatch.setattr(mn, "latest_fetch_time",
                        lambda client, source_id: "2026-07-28 12:00:00")
    monkeypatch.setattr(source_registry, "load_all",
                        lambda: [_source("caixin", "continuous")])
    monkeypatch.setattr(mn, "pending_backlog", lambda client: (0, None))
    monkeypatch.setattr(news_common, "now", lambda: _TUE)
    monkeypatch.setattr(config, "get_news_config", lambda: {})
    monkeypatch.setattr(mn.news_health, "observe", lambda *args, **kwargs: None)

    assert mn.main([]) == 1
