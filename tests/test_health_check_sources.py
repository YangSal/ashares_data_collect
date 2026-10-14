"""tools/health_check_sources.py 纯函数测试（不触网络）。"""
import importlib.util
import os

# tools/ 不是包，按路径加载模块
_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "health_check_sources.py")
_spec = importlib.util.spec_from_file_location("health_check_sources", _PATH)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)


def test_parse_count_picks_unit_bound_int_not_dates_or_codes():
    assert hc.parse_count("60 条 | 首条: 某标题") == 60
    assert hc.parse_count("昨日(20260726) 12 行") == 12          # 跳过日期 20260726
    assert hc.parse_count("探针票 000001 8 条") == 8              # 跳过股票码 000001
    assert hc.parse_count("今日(2026-07-27) 1140 条公告") == 1140  # 跳过日期
    assert hc.parse_count("列表页 25 链接 | 首条: http://x") == 25
    assert hc.parse_count("0 条") == 0
    assert hc.parse_count("TimeoutError: 拉取超时") is None       # 无条目单位
    assert hc.parse_count("") is None


def test_classify():
    assert hc.classify(False, "RuntimeError: HTTP 503") == ("DEAD", None)
    assert hc.classify(True, "0 条") == ("SICK", 0)
    assert hc.classify(True, "60 条 | 首条: x") == ("OK", 60)
    assert hc.classify(True, "无数字结果串") == ("OK", None)      # 解析不出不判病


def _r(id, verdict, enabled=True):
    return {"id": id, "adapter": "rss", "channel": "media", "job": "news_regulator",
            "enabled": enabled, "verdict": verdict, "count": None, "elapsed": 1.0,
            "detail": "x"}


def test_summarize_buckets():
    results = [
        _r("a", "OK"), _r("b", "DEAD"), _r("c", "SICK"),
        _r("d", "DEAD", enabled=False),   # 停用死源：不进 enabled_dead
        _r("stats", "OK", enabled=False),  # 停用但健康 → 恢复源
    ]
    s = hc.summarize(results)
    assert s["total"] == 5 and s["ok"] == 2
    assert [r["id"] for r in s["enabled_dead"]] == ["b"]
    assert [r["id"] for r in s["enabled_sick"]] == ["c"]
    assert [r["id"] for r in s["recovered"]] == ["stats"]


def test_build_alert_none_when_all_healthy():
    s = hc.summarize([_r("a", "OK"), _r("b", "DEAD", enabled=False)])
    assert hc.build_alert(s) is None       # 只有停用死源 → 不告警


def test_build_alert_fires_on_enabled_dead_and_recovered():
    s = hc.summarize([_r("b", "DEAD"), _r("stats", "OK", enabled=False)])
    msg = hc.build_alert(s)
    assert msg is not None
    assert "启用中死源[b]" in msg
    assert "已停但恢复可重启[stats]" in msg


def test_format_report_contains_summary_and_flags():
    results = [_r("b", "DEAD"), _r("stats", "OK", enabled=False)]
    s = hc.summarize(results)
    out = hc.format_report(results, s)
    assert "启用中死源: b" in out
    assert "已停但恢复(可重启用): stats" in out
    assert "DEAD 1" in out


def test_record_results_persists_verdict_mapping_and_elapsed(monkeypatch):
    recorded = []
    results = [
        _r("dead", "DEAD"),
        _r("sick", "SICK"),
        _r("ok", "OK"),
        _r("disabled_ok", "OK", enabled=False),
    ]
    results[0]["elapsed"] = 2.5
    results[0]["detail"] = "RuntimeError: HTTP 503"
    monkeypatch.setattr(
        hc.news_health, "observe",
        lambda source, check_type, status, **kwargs:
        recorded.append((source, check_type, status, kwargs)) or len(recorded))

    written = hc.record_results(results)

    assert written == 4
    assert [item[:3] for item in recorded] == [
        ("dead", "smoke", "dead"),
        ("sick", "smoke", "sick"),
        ("ok", "smoke", "ok"),
        ("disabled_ok", "smoke", "ok"),
    ]
    assert recorded[0][3]["numeric_value"] == 2.5
    assert recorded[0][3]["detail"] == "RuntimeError: HTTP 503"
    assert recorded[-1][3]["enabled"] is False


def test_probe_one_redacts_credentials_before_console_or_ledger(monkeypatch):
    from data_collect.utils import source_adapters as sa
    from data_collect.utils.source_registry import Source

    source = Source(id="secret", adapter="rss", channel="media",
                    job="news_regulator")
    monkeypatch.setattr(
        sa, "smoke_test",
        lambda item: (_ for _ in ()).throw(RuntimeError(
            "proxy http://alice:hunter2@host:7890/feed?token=abc123")))

    result = hc.probe_one(source)

    assert result["verdict"] == "DEAD"
    assert "alice" not in result["detail"]
    assert "hunter2" not in result["detail"]
    assert "abc123" not in result["detail"]
    assert "http://host:7890/feed" in result["detail"]


def test_alert_flag_never_calls_webhook_and_still_returns_dead_exit(
        monkeypatch, capsys):
    from data_collect.utils import notify
    from data_collect.utils.source_registry import Source

    source = Source(id="dead", adapter="rss", channel="media",
                    job="news_regulator")
    recorded = []
    monkeypatch.setattr(hc.sr, "load_all", lambda: [source])
    monkeypatch.setattr(hc, "probe_one", lambda item: _r(item.id, "DEAD"))
    monkeypatch.setattr(
        hc.news_health, "observe",
        lambda source, check_type, status, **kwargs:
        recorded.append((source, status)) or 1)
    monkeypatch.setattr(
        notify, "send_dingtalk",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("主动健康探测不得即时发钉钉")))

    assert hc.main(["--alert"]) == 1

    assert recorded == [("dead", "dead")]
    assert "只记录到18:00日报，不外发" in capsys.readouterr().out


def test_main_returns_nonzero_when_healthy_result_cannot_be_recorded(monkeypatch):
    from data_collect.utils.source_registry import Source

    source = Source(id="ok", adapter="rss", channel="media",
                    job="news_regulator")
    monkeypatch.setattr(hc.sr, "load_all", lambda: [source])
    monkeypatch.setattr(hc, "probe_one", lambda item: _r(item.id, "OK"))
    monkeypatch.setattr(hc.news_health, "observe", lambda *args, **kwargs: None)

    assert hc.main([]) == 1
