"""news_us 单测：全 mock。美国 P1 两源（Fed press_all RSS + SEC EDGAR 8-K Atom）。"""

import datetime
import time
from types import SimpleNamespace

import pytest

from data_collect.jobs import news_us as nus
from data_collect.utils.source_registry import Source

_FETCH_TIME = "2026-07-08 05:30:00"

_PROXY = "http://127.0.0.1:7890"


# 测试源表：替代注册表，patch 到 nus._rss_sources（Source 对象，含 headers/proxy）
def _src(sid, url, channel, ua):
    return Source(id=sid, adapter="rss", channel=channel, job="news_us",
                  url=url, headers={"User-Agent": ua}, proxy_url=_PROXY)


_TEST_FEEDS = {
    "fed": _src("fed", "https://fed.example/feed.xml", "us_policy", "b"),
    "sec_8k": _src("sec_8k", "https://sec.example/atom", "us_filing", "sec"),
    "nyt_business": _src("nyt_business", "https://nyt.example/biz.xml",
                         "us_news", "b"),
}


def _entry(title="Federal Reserve issues FOMC statement",
           link="https://www.federalreserve.gov/newsevents/pressreleases/monetary2026.htm",
           guid="fed-press-1",
           summary="The Federal Reserve decided to hold rates steady.",
           parsed=time.struct_time((2026, 7, 7, 18, 0, 0, 0, 0, 0))):   # UTC
    e = {"title": title, "link": link, "summary": summary}
    if guid is not None:
        e["id"] = guid
    if parsed is not None:
        e["updated_parsed"] = parsed
    return e


def test_fed_envelope_contract():
    env = nus._entry_to_envelope("fed", "us_policy", _entry(),
                                 _FETCH_TIME, {})
    assert env["_id"] == "fed-fed-press-1"               # 一级：source-native_id
    assert env["channel"] == "us_policy"
    assert env["pub_time"] == "2026-07-08 02:00:00"      # UTC 18:00 → 北京次日 02:00
    assert env["vec_status"] == "pending"


@pytest.fixture
def env(monkeypatch):
    calls = SimpleNamespace(bulk=[], archived=[], alerts=[])
    e = SimpleNamespace(calls=calls,
                        feeds={"fed": [_entry()],
                               "sec_8k": [_entry(
                                   title="8-K - ACME CORP (0000123456) (Filer)",
                                   link="https://www.sec.gov/Archives/x/1.htm",
                                   guid="urn:tag:sec.gov,2008:accession-number=0001-26-000001")]})

    def fake_feed(url, **kwargs):
        for source, src in _TEST_FEEDS.items():
            if src.url == url:
                value = e.feeds.get(source, [])   # 未预置的源默认空，不失败
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"未知 feed url: {url}")

    monkeypatch.setattr(nus, "_rss_sources", lambda: dict(_TEST_FEEDS))
    monkeypatch.setattr(nus, "_fetch_feed_entries", fake_feed)
    monkeypatch.setattr(nus, "_now",
                        lambda: datetime.datetime(2026, 7, 8, 5, 30, 0))
    monkeypatch.setattr(nus, "_flush_spool_safe", lambda: None)
    monkeypatch.setattr(nus.news_archive, "load_ids", lambda s, d: set())
    monkeypatch.setattr(nus.news_archive, "append",
                        lambda s, d, envs: len(list(envs)))
    monkeypatch.setattr(nus.osu, "get_client", lambda: object())
    monkeypatch.setattr(nus.osu, "bulk_create",
                        lambda c, docs: (calls.bulk.append(list(docs)),
                                         (len(list(docs)), 0))[1])
    monkeypatch.setattr(nus, "_alert", lambda msg: calls.alerts.append(msg))
    return e


def test_run_merges_two_sources(env):
    summary = nus.run()
    docs = env.calls.bulk[0]
    assert len(docs) == 2
    assert {d["channel"] for d in docs} == {"us_policy", "us_filing"}
    assert all("raw_title" not in d for d in docs)
    assert "美国:" in summary and "入库新增 2" in summary


def test_us_news_media_channel(env):
    """权威媒体源 → channel=us_news（NYT/WSJ/彭博/卫报/MarketWatch，2026-07-09 加）。"""
    env.feeds["nyt_business"] = [_entry(title="Global Economic Output Looks Slower",
                                        link="https://nyt.com/x.html", guid="nyt-1",
                                        summary="Rich abstract text.")]
    nus.run()
    docs = env.calls.bulk[0]
    nyt = [d for d in docs if d["source"] == "nyt_business"]
    assert len(nyt) == 1 and nyt[0]["channel"] == "us_news"
    assert {d["channel"] for d in docs} == {"us_policy", "us_filing", "us_news"}


def test_run_isolates_failed_source(env):
    env.feeds["sec_8k"] = RuntimeError("proxy down")
    summary = nus.run()
    assert "sec_8k 失败" in summary
    assert len(env.calls.bulk[0]) == 1                   # fed 照常
    assert len(env.calls.alerts) == 1


def test_run_all_failed_raises(env, monkeypatch):
    """全部源失败（如代理全挂）→ raise。让所有源都抛。"""
    monkeypatch.setattr(nus, "_fetch_feed_entries",
                        lambda url, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="全部采集失败"):
        nus.run()


def test_run_proxy_missing_source_skipped(env, monkeypatch):
    """声明代理但本节点无代理的源 → 让位跳过：不失败不告警，其余源照采（A4）。"""
    feeds = dict(_TEST_FEEDS)
    feeds["sec_8k"] = Source(id="sec_8k", adapter="rss", channel="us_filing",
                             job="news_us", url="https://sec.example/atom",
                             proxy_name="us", proxy_url="")
    monkeypatch.setattr(nus, "_rss_sources", lambda: feeds)
    summary = nus.run()
    assert "sec_8k 让位" in summary and "让位跳过[sec_8k]" in summary
    assert env.calls.alerts == []                        # 让位≠失败，不告警
    docs = env.calls.bulk[0]
    assert {d["source"] for d in docs} == {"fed"}        # 其余源照采


def test_run_all_proxy_missing_returns_quietly(env, monkeypatch):
    """全部源让位（无代理节点只配了美国源）→ 静默返回摘要，不 raise 不告警（A4）。"""
    feeds = {sid: Source(id=sid, adapter="rss", channel=s.channel, job="news_us",
                         url=s.url, proxy_name="us", proxy_url="")
             for sid, s in _TEST_FEEDS.items()}
    monkeypatch.setattr(nus, "_rss_sources", lambda: feeds)
    summary = nus.run()
    assert "让位跳过" in summary
    assert env.calls.bulk == [] and env.calls.alerts == []


def test_redact_strips_proxy_credentials():
    """代理 URL 的 user:pass@ 段进钉钉前必须脱敏（S5）。"""
    msg = "ProxyError: http://alice:s3cr3t@127.0.0.1:7890 refused"
    out = nus._redact(msg)
    assert "s3cr3t" not in out and "alice" not in out
    assert "***@127.0.0.1:7890" in out


def test_archive_failure_does_not_block_indexing(env, monkeypatch):
    """单源归档失败不阻塞其余源入库（S2）：sec_8k 归档抛错，两源仍全部入库。"""
    real_archive = nus._archive_source

    def flaky(source, envelopes):
        if source == "sec_8k":
            raise RuntimeError("NAS down")
        return real_archive(source, envelopes)

    monkeypatch.setattr(nus, "_archive_source", flaky)
    nus.run()
    docs = env.calls.bulk[0]
    assert {d["channel"] for d in docs} == {"us_policy", "us_filing"}   # 两源都入库


def test_verify_replays_archive(env, monkeypatch):
    monkeypatch.setattr(nus, "get_news_config", lambda: {"verify_days_back": 1})
    monkeypatch.setattr(nus, "_today", lambda: datetime.date(2026, 7, 8))
    monkeypatch.setattr(nus.news_archive, "replay",
                        lambda s, dr: iter([nus._entry_to_envelope(
                            "fed", "us_policy", _entry(), _FETCH_TIME, {})])
                        if s == "fed" else iter([]))
    summary = nus.run_verify("20990101", "20990102")
    assert env.calls.bulk[0][0]["_id"] == "fed-fed-press-1"
    assert "重放" in summary
