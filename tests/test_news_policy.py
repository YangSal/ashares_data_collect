"""news_policy 单测：全 mock。③ 政策/宏观四源（官方 RSS×3 + 东财财经早餐）。"""

import datetime
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from data_collect.jobs import news_policy as npo
from data_collect.utils.source_registry import Source

_FETCH_TIME = "2026-07-08 15:00:00"


# 测试源表：替代注册表，patch 到 npo._rss_sources（与真实 sources.yaml/config 解耦）
def _src(sid, url, channel):
    return Source(id=sid, adapter="rss", channel=channel, job="news_policy",
                  url=url, headers={"User-Agent": "test"})


_TEST_RSS = {
    "govcn_policy": _src("govcn_policy", "https://gov.cn/policy.xml", "policy"),
    "govcn_gwy": _src("govcn_gwy", "https://gov.cn/gwy.xml", "policy"),
    "stats": _src("stats", "https://stats.gov.cn/rss.xml", "policy"),
}


def test_rss_sources_include_pbc_but_not_miit_search_api():
    """工信部已迁入官方搜索 API，不能再由失效历史 RSS 重复拉取。"""
    sources = set(npo._rss_sources())
    assert "pbc_news" in sources
    assert "miit_policy" not in sources


def _entry(title="国务院关于印发《提振消费方案》的通知",
           link="https://www.gov.cn/zhengce/1.htm", guid=None,
           summary="方案提出支持贵州茅台等消费龙头……",
           parsed=time.struct_time((2026, 7, 8, 2, 30, 0, 0, 0, 0))):
    """合成 RSS entry（dict 即可——实现只用 .get；parsed 为 UTC struct_time）。"""
    entry = {"title": title, "link": link, "summary": summary}
    if guid is not None:
        entry["id"] = guid
    if parsed is not None:
        entry["published_parsed"] = parsed
    return entry


def _cjzc_df(rows):
    return pd.DataFrame(rows, columns=["标题", "摘要", "发布时间", "链接"])


def _cjzc_row(title="财经早餐0708", url="https://em.com/cjzc/1.html"):
    return {"标题": title, "摘要": f"{title} 摘要内容",
            "发布时间": "2026-07-08 07:30:00", "链接": url}


# ---------- 信封契约 ----------

def test_entry_envelope_contract():
    env = npo._entry_to_envelope("govcn_policy", "policy",
                                 _entry(guid="gov-123"), _FETCH_TIME,
                                 {"贵州茅台": "600519.SH"})
    assert env["_id"] == "govcn_policy-gov-123"          # 一级：source-native_id
    assert env["channel"] == "policy"
    assert env["source"] == "govcn_policy"
    assert env["pub_time"] == "2026-07-08 10:30:00"      # UTC 02:30 → 北京 10:30
    assert env["url"] == "https://www.gov.cn/zhengce/1.htm"
    assert "600519.SH" in env["stocks"]                  # 摘要提及 → 打标
    assert env["vec_status"] == "pending"
    assert "time_estimated" not in env
    assert env["raw_title"].startswith("国务院")          # 原文仅归档


def test_entry_without_guid_uses_url_id():
    env = npo._entry_to_envelope("stats", "policy", _entry(), _FETCH_TIME, {})
    import hashlib
    assert env["_id"] == hashlib.sha1(
        b"https://www.gov.cn/zhengce/1.htm").hexdigest()  # 二级：sha1(url)


def test_entry_without_time_falls_back_and_marks():
    e1 = npo._entry_to_envelope("stats", "policy", _entry(parsed=None),
                                _FETCH_TIME, {})
    e2 = npo._entry_to_envelope("stats", "policy", _entry(parsed=None),
                                "2026-07-08 16:00:00", {})
    assert e1["pub_time"] == _FETCH_TIME and e1["time_estimated"] is True
    assert e1["_id"] == e2["_id"]                        # 决定论：fetch_time 不参与 _id


def test_entry_html_summary_cleaned():
    env = npo._entry_to_envelope(
        "govcn_policy", "policy",
        _entry(summary="<p>第一段Ａ</p><p>第二段</p>"), _FETCH_TIME, {})
    assert "<p>" not in env["content"] and "第一段A" in env["content"]


def test_cjzc_envelope_contract():
    env = npo._cjzc_to_envelope(pd.Series(_cjzc_row()), _FETCH_TIME, {})
    assert env["channel"] == "media" and env["source"] == "em_cjzc"
    assert env["pub_time"] == "2026-07-08 07:30:00"
    assert env["url"] == "https://em.com/cjzc/1.html"


# ---------- run：四源合并/隔离/归档 ----------

@pytest.fixture
def env(monkeypatch):
    calls = SimpleNamespace(bulk=[], archived=[], alerts=[])
    e = SimpleNamespace(calls=calls,
                        rss={"govcn_policy": [_entry(guid="a1")],
                             "govcn_gwy": [_entry(guid="a2",
                                                  link="https://gov.cn/gwy/2.htm")],
                             "stats": [_entry(guid="a3",
                                              link="https://stats.gov.cn/3.htm")]},
                        cjzc=_cjzc_df([_cjzc_row()]))

    def fake_rss(url, **kwargs):
        for source, src in _TEST_RSS.items():
            if src.url == url:
                value = e.rss[source]
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"未知 RSS url: {url}")

    def fake_cjzc():
        if isinstance(e.cjzc, Exception):
            raise e.cjzc
        return e.cjzc

    monkeypatch.setattr(npo, "_rss_sources", lambda: dict(_TEST_RSS))
    monkeypatch.setattr(npo.source_registry, "is_enabled", lambda source: True)
    monkeypatch.setattr(npo, "_fetch_rss_entries", fake_rss)
    monkeypatch.setattr(npo, "_fetch_cjzc", fake_cjzc)
    monkeypatch.setattr(npo, "_now",
                        lambda: datetime.datetime(2026, 7, 8, 15, 0, 0))
    monkeypatch.setattr(npo, "_flush_spool_safe", lambda: None)
    monkeypatch.setattr(npo.news_archive, "load_ids", lambda s, d: set())
    monkeypatch.setattr(npo.news_archive, "append",
                        lambda s, d, envs: (calls.archived.append(
                            (s, d, list(envs))), len(list(envs)))[1])
    monkeypatch.setattr(npo.osu, "get_client", lambda: object())
    monkeypatch.setattr(npo.osu, "bulk_create",
                        lambda c, docs: (calls.bulk.append(list(docs)),
                                         (len(list(docs)), 0))[1])
    monkeypatch.setattr(npo, "_alert", lambda msg: calls.alerts.append(msg))
    monkeypatch.setattr(npo.nn, "load_name_dict", lambda: {})
    return e


def test_run_merges_four_sources(env):
    summary = npo.run()
    assert len(env.calls.bulk) == 1
    docs = env.calls.bulk[0]
    assert len(docs) == 4
    assert {d["channel"] for d in docs} == {"policy", "media"}
    assert all("raw_title" not in d for d in docs)       # 入库剥 raw_*
    archived_sources = {a[0] for a in env.calls.archived}
    assert archived_sources == {"govcn_policy", "govcn_gwy", "stats", "em_cjzc"}
    assert "政策:" in summary and "入库新增 4" in summary


def test_run_skips_disabled_cjzc_source(env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        npo.source_registry, "is_enabled", lambda source: source != "em_cjzc")
    monkeypatch.setattr(
        npo, "_fetch_cjzc", lambda: calls.append("em_cjzc") or env.cjzc)

    summary = npo.run()

    assert calls == []
    assert "em_cjzc" not in {d["source"] for d in env.calls.bulk[0]}
    assert "入库新增 3" in summary


def test_run_isolates_failed_source_and_alerts(env):
    env.rss["stats"] = RuntimeError("RSS HTTP 503")
    summary = npo.run()
    assert "stats 失败" in summary and "失败源[stats]" in summary
    assert len(env.calls.bulk[0]) == 3                   # 其余三源照常
    assert len(env.calls.alerts) == 1                    # 单源失败告警一次


def test_run_transient_timeout_recovers_without_alert(env, monkeypatch):
    """stats 首次读取超时但重试恢复 → 照常入库且不报错。"""
    from requests.exceptions import ReadTimeout

    original_fetch = npo._fetch_rss_entries
    attempts = 0

    def flaky(url, **kwargs):
        nonlocal attempts
        if url == _TEST_RSS["stats"].url:
            attempts += 1
            if attempts == 1:
                raise ReadTimeout("read timed out")
        return original_fetch(url, **kwargs)

    monkeypatch.setattr(npo, "_fetch_rss_entries", flaky)
    monkeypatch.setattr(npo.sa.time, "sleep", lambda seconds: None)
    summary = npo.run()
    assert "stats 失败" not in summary
    assert env.calls.alerts == []
    assert "stats" in {d["source"] for d in env.calls.bulk[0]}
    assert attempts == 2


def test_run_isolates_hanging_source(env, monkeypatch):
    import time as _time
    monkeypatch.setattr(npo, "_FETCH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(npo, "_fetch_cjzc", lambda: _time.sleep(3))
    summary = npo.run()
    assert "em_cjzc 失败" in summary
    assert len(env.calls.bulk[0]) == 3


def test_run_all_sources_failed_raises(env):
    for s in ("govcn_policy", "govcn_gwy", "stats"):
        env.rss[s] = RuntimeError("boom")
    env.cjzc = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="全部采集失败"):
        npo.run()


def test_run_dedups_within_batch(env):
    env.rss["govcn_policy"] = [_entry(guid="dup"), _entry(guid="dup")]
    npo.run()
    ids = [d["_id"] for d in env.calls.bulk[0]]
    assert len(ids) == len(set(ids))


def test_archive_groups_by_pub_date(env):
    env.rss["govcn_policy"] = [
        _entry(guid="d1", parsed=time.struct_time((2026, 7, 7, 2, 0, 0, 0, 0, 0))),
        _entry(guid="d2", parsed=time.struct_time((2026, 7, 8, 2, 0, 0, 0, 0, 0))),
    ]
    npo.run()
    dates = {a[1] for a in env.calls.archived if a[0] == "govcn_policy"}
    assert dates == {"20260707", "20260708"}


# ---------- verify：归档重放补库 ----------

def test_verify_replays_archive(env, monkeypatch):
    monkeypatch.setattr(npo, "get_news_config", lambda: {"verify_days_back": 1})
    monkeypatch.setattr(npo, "_today", lambda: datetime.date(2026, 7, 8))
    replayed = {"govcn_policy": [npo._entry_to_envelope(
        "govcn_policy", "policy", _entry(guid="old"), _FETCH_TIME, {})]}
    monkeypatch.setattr(npo.news_archive, "replay",
                        lambda s, dr: iter(replayed.get(s, [])))
    summary = npo.run_verify("20990101", "20990102")     # 框架窗口被忽略
    assert len(env.calls.bulk) == 1
    assert env.calls.bulk[0][0]["_id"] == "govcn_policy-old"
    assert "raw_title" not in env.calls.bulk[0][0]
    assert "重放" in summary
