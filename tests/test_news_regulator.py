"""news_regulator 单测：全 mock。B 档（rsshub=发改委 + listpage=证监会/央行）。"""

import datetime
import hashlib
import time
from types import SimpleNamespace

import pytest

from data_collect.jobs import news_regulator as nr
from data_collect.jobs import news_regulator_official as official
from data_collect.utils.source_registry import Source

_FETCH_TIME = "2026-07-08 16:00:00"


def _rsshub_src(sid, route, channel):
    return Source(id=sid, adapter="rsshub", channel=channel,
                  job="news_regulator", route=route)


def _official_src(sid, adapter="listpage", channel="policy"):
    return Source(id=sid, adapter=adapter, channel=channel,
                  job="news_regulator", url=f"https://example.cn/{sid}",
                  timeout=90)


def _sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---------- listpage：链接正则锚点 ----------

def test_csrc_link_regex():
    html = ('<a href="/csrc/c100028/c1615676/content.shtml">通知一</a>'
            '<a href="/csrc/c100028/c1615671/content.shtml">通知二</a>'
            '<a href="/csrc/c100028/c1615676/content.shtml">重复</a>'
            '<a href="/csrc/other.shtml">无关</a>')
    urls = nr._extract_article_urls("csrc", html)
    assert urls == ["http://www.csrc.gov.cn/csrc/c100028/c1615676/content.shtml",
                    "http://www.csrc.gov.cn/csrc/c100028/c1615671/content.shtml"]


def test_pbc_removed_from_listpages():
    """审查修复：pbc（央行）结构性 0 产出+每小时白锤 WAF，已从源表移除。"""
    assert "pbc" not in nr._LISTPAGES


def test_mof_link_regex():
    """财政部：`./202607/txxx.htm` 相对链接 → 列表页目录绝对化。"""
    html = ('<a href="./202607/t20260707_3993044.htm">新闻一</a>'
            '<a href="./202607/t20260706_3992894.htm">新闻二</a>'
            '<a href="/other/x.htm">无关</a>')
    urls = nr._extract_article_urls("mof", html)
    assert urls == [
        "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/202607/t20260707_3993044.htm",
        "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/202607/t20260706_3992894.htm"]


def test_people_link_regex():
    html = ('<a href="http://finance.people.com.cn/n1/2026/0626/c1004-40748358.html">甲</a>'
            '<a href="/n1/2026/0708/c1004-40756129.html">乙</a>'
            '<a href="/n2/xx.html">无关</a>')
    urls = nr._extract_article_urls("people", html)
    assert urls == [
        "http://finance.people.com.cn/n1/2026/0626/c1004-40748358.html",
        "http://finance.people.com.cn/n1/2026/0708/c1004-40756129.html"]


# ---------- listpage：文章信封 ----------

_ARTICLE_HTML = ("<html><head><title>某某办法公开征求意见_中国证券监督管理委员会"
                 "</title></head><body>发布时间：2026-07-07 16:30 正文…</body></html>")


def test_article_envelope_contract(monkeypatch):
    monkeypatch.setattr(nr.fulltext, "_extract",
                        lambda html, url: "正文Ａ" + "字" * 200)
    url = "http://www.csrc.gov.cn/csrc/c100028/c1615676/content.shtml"
    env = nr._article_envelope("csrc", "policy", url, _ARTICLE_HTML,
                               _FETCH_TIME, {})
    assert env["_id"] == _sha1(url)                      # 二级 sha1(url)
    assert env["title"] == "某某办法公开征求意见"          # 站名后缀剥除
    assert env["pub_time"] == "2026-07-07 16:30:00"      # HTML 嗅探 + 补秒
    assert env["content"].startswith("正文A")             # clean_text
    assert env["channel"] == "policy" and env["source"] == "csrc"
    assert env["vec_status"] == "pending"


def test_article_envelope_rejects_challenge_page(monkeypatch):
    """WAF 挑战/跳首页：title 即站名 → 拒收（不入库，同 URL 留待下轮重采）。

    实撞（2026-07-08）：csrc 连抓 20 篇触发限流，服务器改发首页——title 只剩
    '中国证券监督管理委员会'、正文是公告导航列表；若不拒收，垃圾文档占住
    _id=sha1(url)，create-only 下正确版本永远进不来。
    """
    monkeypatch.setattr(nr.fulltext, "_extract", lambda html, url: "字" * 200)
    env = nr._article_envelope(
        "csrc", "policy", "http://x/a.shtml",
        "<title>中国证券监督管理委员会</title>", _FETCH_TIME, {})
    assert env is None


def test_article_envelope_rejects_short_content(monkeypatch):
    monkeypatch.setattr(nr.fulltext, "_extract", lambda html, url: "太短")
    env = nr._article_envelope("csrc", "policy", "http://x/a.shtml",
                               _ARTICLE_HTML, _FETCH_TIME, {})
    assert env is None


def test_sniff_pub_time_prefers_labeled_date():
    """审查修复：优先取"发布时间"标签后的日期，不取靠前的导航/版权日期。"""
    html = ('<div class="nav">2020-01-01 版权所有</div>'
            '<div class="info">发布时间：2026-07-07 16:30</div>')
    assert nr._sniff_pub_time(html) == "2026-07-07 16:30:00"


def test_sniff_pub_time_fallback_first_date():
    """无标签命中 → 回退首个日期（best-effort）。"""
    assert nr._sniff_pub_time("<p>某文 2026-07-05 发布</p>") == "2026-07-05 00:00:00"


def test_article_envelope_time_fallback(monkeypatch):
    monkeypatch.setattr(nr.fulltext, "_extract", lambda html, url: "字" * 200)
    env = nr._article_envelope("csrc", "policy", "http://x/a.shtml",
                               "<title>标题_中国证券监督管理委员会</title>",
                               _FETCH_TIME, {})
    assert env["pub_time"] == _FETCH_TIME
    assert env["time_estimated"] is True


# ---------- run：抓前去重 / 源级隔离 ----------

def test_jiemian_collection_limits_rsshub_fanout(monkeypatch):
    """jiemian 仅在请求时限 10 篇，不改注册表中的冻结路由。"""
    captured = {}
    statuses = {}
    routes = {"jiemian": _rsshub_src(
        "jiemian", "/jiemian/lists/1", "media")}
    monkeypatch.setattr(
        nr, "_fetch_rsshub",
        lambda source, **kwargs: (
            captured.update(source=source, kwargs=kwargs),
            nr.sa.RSSHubResult([], 1, "http://backup:1200"))[1])

    assert nr._collect_source(
        "jiemian", _FETCH_TIME, {}, object(), routes,
        bases=("http://primary:1200", "http://backup:1200"),
        transport_statuses=statuses) == []
    assert captured["source"].route == "/jiemian/lists/1"
    assert captured["kwargs"] == {
        "bases": ("http://primary:1200", "http://backup:1200"),
        "route_suffix": "?limit=10",
    }
    assert statuses == {"jiemian": "failover"}

@pytest.fixture
def env(monkeypatch):
    calls = SimpleNamespace(bulk=[], alerts=[], fetched_pages=[], health=[])
    e = SimpleNamespace(
        calls=calls,
        list_html={"csrc": '<a href="/csrc/c100028/c1/content.shtml">一</a>'
                           '<a href="/csrc/c100028/c2/content.shtml">二</a>',
                   "mof": '<a href="./202607/t20260708_1.htm">财政</a>',
                   "people": '<a href="/n1/2026/0708/c1004-1.html">人民</a>'},
        existing_ids=set(),
        rsshub_entries=[{"title": "发改委新闻", "link": "https://ndrc.gov.cn/1.htm",
                         "id": "ndrc-1", "summary": "成品油价格调整",
                         "published_parsed": time.struct_time(
                             (2026, 7, 8, 2, 0, 0, 0, 0, 0))}])

    def fake_http_get(url):
        calls.fetched_pages.append(url)
        for source, cfg in nr._LISTPAGES.items():
            if url == cfg["list_url"]:
                return e.list_html[source]
        return _ARTICLE_HTML          # 文章页

    # 注册表 shim 打桩：只留 ndrc（rsshub）+ 全部 listpage，语义等价原"路由感知"
    monkeypatch.setattr(nr, "_load_sources",
                        lambda: ({"ndrc": _rsshub_src("ndrc", "/gov/ndrc/xwdt",
                                                      "policy")},
                                 dict(nr._LISTPAGES), {}))
    monkeypatch.setattr(nr.fulltext, "_http_get", fake_http_get)
    monkeypatch.setattr(nr.fulltext, "_extract", lambda html, url: "字" * 200)
    monkeypatch.setattr(
        nr, "_fetch_rsshub",
        lambda source, **kwargs: nr.sa.RSSHubResult(
            e.rsshub_entries if source.id == "ndrc" else [],
            0, "http://192.168.9.10:1200"))
    monkeypatch.setattr(nr, "_existing_ids",
                        lambda client, ids: {i for i in ids if i in e.existing_ids})
    monkeypatch.setattr(nr, "_archive_source", lambda s, envs: len(envs))
    monkeypatch.setattr(nr, "_now",
                        lambda: datetime.datetime(2026, 7, 8, 16, 0, 0))
    monkeypatch.setattr(nr, "_flush_spool_safe", lambda: None)
    monkeypatch.setattr(nr, "get_news_config",
                        lambda: {"rsshub_base": "http://192.168.9.10:1200",
                                 "verify_days_back": 1})
    monkeypatch.setattr(nr.osu, "get_client", lambda: object())
    monkeypatch.setattr(nr.osu, "bulk_create",
                        lambda c, docs: (calls.bulk.append(list(docs)),
                                         (len(list(docs)), 0))[1])
    monkeypatch.setattr(nr, "_alert", lambda msg: calls.alerts.append(msg))
    monkeypatch.setattr(
        nr.news_health, "observe",
        lambda source, check_type, status, **kwargs:
        calls.health.append((source, check_type, status, kwargs)) or len(calls.health))
    monkeypatch.setattr(nr.nn, "load_name_dict", lambda: {})
    monkeypatch.setattr(nr.time, "sleep", lambda s: None)
    return e


def test_run_merges_sources(env):
    summary = nr.run()
    docs = env.calls.bulk[0]
    # ndrc 1 + csrc 2 + mof 1 + people 1（其余 rsshub 源路由感知返回空）
    assert len(docs) == 5
    assert {d["source"] for d in docs} == {"ndrc", "csrc", "mof", "people"}
    assert all("raw_title" not in d for d in docs)
    assert "监管:" in summary and "入库新增 5" in summary
    assert "失败源" not in summary          # listpage 全配好 → 无失败源
    assert [item[:3] for item in env.calls.health] == [
        ("ndrc", "transport", "primary")]


def test_run_skips_existing_articles(env):
    """抓前去重：已入库的文章 URL 不再抓正文页。"""
    env.existing_ids = {_sha1("http://www.csrc.gov.cn/csrc/c100028/c1/content.shtml")}
    nr.run()
    fetched_articles = [u for u in env.calls.fetched_pages if "c1/content" in u]
    assert fetched_articles == []                        # c1 被去重，未抓
    docs = env.calls.bulk[0]
    assert len([d for d in docs if d["source"] == "csrc"]) == 1   # 只剩 c2
    assert len(docs) == 4                                          # ndrc1+csrc1+mof1+people1


def test_run_isolates_failed_source(env, monkeypatch):
    monkeypatch.setattr(
        nr, "_fetch_rsshub",
        lambda source, **kwargs: (_ for _ in ()).throw(
            nr.sa.RSSHubUnavailableError("primary=HTTP 503")))
    monkeypatch.setattr(nr.sa.time, "sleep", lambda seconds: None)
    summary = nr.run()
    assert "ndrc 失败" in summary
    assert len(env.calls.alerts) == 1
    assert {d["source"] for d in env.calls.bulk[0]} == {"csrc", "mof", "people"}
    assert [item[:3] for item in env.calls.health] == [
        ("ndrc", "transport", "failed")]


def test_run_transient_rsshub_failure_recovers_without_alert(env, monkeypatch):
    """源级重试成功不应进入 18:00 错误汇总。"""
    attempts = 0

    def flaky(source, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise nr.sa.RSSHubUnavailableError(
                "RSSHub ndrc 全部实例失败: primary=HTTP 503")
        return nr.sa.RSSHubResult(
            env.rsshub_entries, 0, "http://192.168.9.10:1200")

    monkeypatch.setattr(nr, "_fetch_rsshub", flaky)
    monkeypatch.setattr(nr.sa.time, "sleep", lambda seconds: None)
    summary = nr.run()
    assert "ndrc 失败" not in summary
    assert env.calls.alerts == []
    assert "ndrc" in {d["source"] for d in env.calls.bulk[0]}
    assert attempts == 2
    assert [item[:3] for item in env.calls.health] == [
        ("ndrc", "transport", "primary")]


def test_run_reports_backup_instance_without_marking_source_failed(env, monkeypatch):
    monkeypatch.setattr(
        nr, "_fetch_rsshub",
        lambda source, **kwargs: nr.sa.RSSHubResult(
            env.rsshub_entries, 1, "http://backup:1200"))

    summary = nr.run()

    assert "ndrc 失败" not in summary
    assert "备用实例[ndrc]" in summary
    assert [item[:3] for item in env.calls.health] == [
        ("ndrc", "transport", "failover")]


def test_run_rsshub_base_missing(env, monkeypatch):
    """未配置 rsshub_base → rsshub 类源计失败并提示配置，listpage 源照常。"""
    monkeypatch.setattr(nr, "get_news_config", lambda: {"verify_days_back": 1})
    summary = nr.run()
    assert "ndrc 失败" in summary
    assert len(env.calls.bulk[0]) == 4                   # csrc 2 + mof 1 + people 1
    assert [item[:3] for item in env.calls.health] == [
        ("ndrc", "transport", "failed")]


def test_run_all_failed_raises(env, monkeypatch):
    monkeypatch.setattr(nr.fulltext, "_http_get", lambda url: None)
    monkeypatch.setattr(
        nr, "_fetch_rsshub",
        lambda source, **kwargs: (_ for _ in ()).throw(
            nr.sa.RSSHubUnavailableError("boom")))
    monkeypatch.setattr(nr.sa.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="全部采集失败"):
        nr.run()


def test_verify_replays_archive(env, monkeypatch):
    monkeypatch.setattr(nr, "_today", lambda: datetime.date(2026, 7, 8))
    monkeypatch.setattr(nr.news_archive, "replay",
                        lambda s, dr: iter([{"_id": "x", "title": "t",
                                             "raw_title": "t", "source": s}])
                        if s == "csrc" else iter([]))
    summary = nr.run_verify("20990101", "20990102")
    assert env.calls.bulk[0][0]["_id"] == "x"
    assert "raw_title" not in env.calls.bulk[0][0]
    assert "重放" in summary


def test_load_sources_groups_official_collectors(monkeypatch):
    """官方 listpage/API 必须进入独立分派，不能落入旧正则表。"""
    sources = [
        _rsshub_src("ndrc", "/gov/ndrc/xwdt", "policy"),
        Source(id="csrc", adapter="listpage", channel="policy",
               job="news_regulator"),
        _official_src("xinhua_fortune", channel="media"),
        _official_src("gov_bmwj"),
        _official_src("sse_regulation"),
        _official_src("miit_policy", adapter="api"),
        _official_src("bse_regulation", adapter="api"),
    ]
    monkeypatch.setattr(nr.source_registry, "load_sources",
                        lambda job, **kw: sources)
    routes, legacy, officials = nr._load_sources()
    assert set(routes) == {"ndrc"}
    assert set(legacy) == {"csrc"}
    assert set(officials) == {
        "miit_policy", "xinhua_fortune", "gov_bmwj",
        "sse_regulation", "bse_regulation"}


def test_official_item_envelope_contract():
    item = official.OfficialItem(
        source="bse_regulation", native_id="discipline-123",
        title="关于给予纪律处分的决定", pub_time="2026-08-28 00:00:00",
        url="https://www.bse.cn/uploads/action.pdf", category="纪律处分",
        content="纪律处分：关于给予纪律处分的决定", raw={"id": 123})
    env = nr._official_item_to_envelope(item, "policy", _FETCH_TIME, {})
    assert env["_id"] == "bse_regulation-discipline-123"
    assert env["channel"] == "policy"
    assert env["content"].startswith("纪律处分")
    assert '"id": 123' in env["raw_content"]
    assert env["vec_status"] == "pending"


def test_miit_preserves_legacy_document_and_collects_other_articles(monkeypatch):
    items = [official.OfficialItem(
        "miit_policy", key, key, "2026-08-31 00:00:00",
        f"https://www.miit.gov.cn/zwgk/zcwj/wjfb/tz/art/2026/art_{key}.html")
        for key in ("old", "new-a", "new-b")]
    monkeypatch.setattr(official, "discover_source", lambda *a, **kw: items)
    queries = []

    def search(client, body):
        queries.append(body)
        return {"aggregations": {"urls": {"buckets": [{"key": items[0].url}]}}}

    monkeypatch.setattr(nr.osu, "search_raw", search)
    rows = nr._collect_official_source(
        "miit_policy", _official_src("miit_policy", adapter="api"),
        _FETCH_TIME, {}, object(), http_client=object())
    assert [row["_id"] for row in rows] == ["miit_policy-new-a", "miit_policy-new-b"]
    assert {"term": {"source": "miit_policy"}} in queries[0]["query"]["bool"]["filter"]
    assert {"terms": {"url": [item.url for item in items]}} in queries[0]["query"]["bool"]["filter"]


def test_miit_legacy_dedup_failure_does_not_create_duplicate(monkeypatch):
    item = official.OfficialItem("miit_policy", "id", "文件", "2026-08-31 00:00:00",
                                 "https://www.miit.gov.cn/zwgk/zcwj/wjfb/a.html")
    monkeypatch.setattr(official, "discover_source", lambda *a, **kw: [item])

    def unavailable(*a, **kw):
        raise RuntimeError("search unavailable")

    monkeypatch.setattr(nr.osu, "search_raw", unavailable)
    with pytest.raises(RuntimeError, match="search unavailable"):
        nr._collect_official_source(
            "miit_policy", _official_src("miit_policy", adapter="api"),
            _FETCH_TIME, {}, object(), http_client=object())


@pytest.mark.parametrize("partial", [{"timed_out": True}, {"_shards": {"failed": 1}}])
def test_url_migration_dedup_rejects_partial_search(monkeypatch, partial):
    monkeypatch.setattr(nr.osu, "search_raw", lambda *a, **kw: {
        "aggregations": {"urls": {"buckets": []}}, **partial})
    with pytest.raises(RuntimeError, match="未完整返回"):
        nr._existing_source_urls(object(), "miit_policy", ["https://www.miit.gov.cn/a.html"])


def test_paper_collector_preserves_old_guid_and_isolates_article_failure(monkeypatch):
    from data_collect.jobs import news_regulator_current as current
    items = [official.OfficialItem(
        "thepaper", f"https://m.thepaper.cn/detail/{key}", f"标题{key}",
        "2026-10-14 08:00:00", f"https://m.thepaper.cn/newsDetail_forward_{key}")
        for key in (1, 2, 3)]
    monkeypatch.setattr(official, "discover_source", lambda *a, **kw: items)
    monkeypatch.setattr(nr.osu, "search_raw", lambda *a, **kw: {
        "hits": {"hits": [{"_id": "thepaper-https://m.thepaper.cn/detail/1"}]}})
    calls = []
    def hydrate(client, item):
        calls.append(item.native_id)
        return None if item == items[1] else official.dataclasses.replace(item, content="正文" * 100)
    monkeypatch.setattr(current, "hydrate_thepaper", hydrate)
    rows = nr._collect_official_source(
        "thepaper", _official_src("thepaper", adapter="api", channel="media"),
        _FETCH_TIME, {}, object(), http_client=object())
    assert calls == [item.native_id for item in items[1:]]
    assert [row["_id"] for row in rows] == ["thepaper-https://m.thepaper.cn/detail/3"]


def test_collect_official_hydrates_only_unknown_articles(monkeypatch):
    from data_collect.jobs import news_regulator_current as current
    src = _official_src("gov_bmwj")
    known = official.OfficialItem(
        "gov_bmwj", "1", "已入库", "2026-08-28 00:00:00",
        "https://www.gov.cn/zhengce/zhengceku/202608/content_1.htm")
    fresh = official.OfficialItem(
        "gov_bmwj", "2", "新文件", "2026-08-29 00:00:00",
        "https://www.gov.cn/zhengce/zhengceku/202608/content_2.htm")
    hydrated = official.dataclasses.replace(fresh, content="正文" * 100)
    hydrated_calls = []
    monkeypatch.setattr(nr.osu, "search_raw", lambda *a, **kw: {
        "aggregations": {"urls": {"buckets": [{"key": known.url.replace("https:", "http:")}]}}})
    monkeypatch.setattr(official, "discover_source",
                        lambda *args, **kwargs: [known, fresh])
    monkeypatch.setattr(current, "hydrate_gov",
                        lambda client, item: hydrated_calls.append(item.native_id)
                        or hydrated)
    monkeypatch.setattr(
        nr, "_existing_ids",
        lambda client, ids: {nr.nn.make_id({"source": "gov_bmwj",
                                             "native_id": "1"})})
    rows = nr._collect_official_source(
        "gov_bmwj", src, _FETCH_TIME, {}, object(), http_client=object())
    assert hydrated_calls == ["2"]
    assert [row["title"] for row in rows] == ["新文件"]


def test_collect_official_caps_normal_article_hydration_at_ten(monkeypatch):
    """正常轮询若超过十篇正文请求，会违反源站保护硬约束。"""
    src = _official_src("xinhua_fortune", channel="media")
    items = [official.OfficialItem(
        "xinhua_fortune", str(index), f"文章{index}",
        "2026-08-29 00:00:00",
        f"https://www.news.cn/fortune/20260829/{index}/c.html")
        for index in range(11)]
    hydrated_calls = []
    monkeypatch.setattr(official, "discover_source",
                        lambda *args, **kwargs: items)
    monkeypatch.setattr(
        official, "hydrate_article",
        lambda client, item: hydrated_calls.append(item.native_id)
        or official.dataclasses.replace(item, content="正文" * 100))
    monkeypatch.setattr(nr, "_existing_ids", lambda client, ids: set())
    rows = nr._collect_official_source(
        "xinhua_fortune", src, _FETCH_TIME, {}, object(),
        http_client=object())
    assert len(rows) == 10
    assert hydrated_calls == [str(index) for index in range(10)]


def test_run_records_official_transport_and_merges_documents(env, monkeypatch):
    src = _official_src("sse_regulation")
    monkeypatch.setattr(
        nr, "_load_sources",
        lambda: ({"ndrc": _rsshub_src("ndrc", "/gov/ndrc/xwdt", "policy")},
                 dict(nr._LISTPAGES), {"sse_regulation": src}))
    item = official.OfficialItem(
        "sse_regulation", "20260828_1", "监管案例",
        "2026-08-28 00:00:00",
        "https://www.sse.com.cn/regulation/trading/case/c/c_20260828_1.shtml",
        "交易监管案例", "交易监管案例：监管案例")
    monkeypatch.setattr(official, "discover_source",
                        lambda source, *args, **kwargs: [item])
    summary = nr.run()
    assert "sse_regulation 1条" in summary
    assert "sse_regulation" in {doc["source"] for doc in env.calls.bulk[0]}
    assert ("sse_regulation", "transport", "primary") in [
        call[:3] for call in env.calls.health]


def test_run_isolates_failed_official_source(env, monkeypatch):
    src = _official_src("sse_regulation")
    monkeypatch.setattr(
        nr, "_load_sources",
        lambda: ({}, {"people": nr._LISTPAGES["people"]},
                 {"sse_regulation": src}))
    monkeypatch.setattr(
        official, "discover_source",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            official.OfficialStructureError("sse changed")))
    summary = nr.run()
    assert "sse_regulation 失败" in summary
    assert {doc["source"] for doc in env.calls.bulk[0]} == {"people"}
    assert ("sse_regulation", "transport", "failed") in [
        call[:3] for call in env.calls.health]


def test_backfill_rejects_over_90_days_before_loading_sources():
    with pytest.raises(ValueError, match="90 days"):
        nr.run_backfill("20260101", "20260430")


def test_backfill_isolates_sources_and_persists_success(env, monkeypatch):
    sources = {sid: _official_src(
        sid, adapter="api" if sid in {"miit_policy", "bse_regulation"}
        else "listpage",
        channel="media" if sid == "xinhua_fortune" else "policy")
        for sid in official.BACKFILL_SOURCE_IDS}
    monkeypatch.setattr(nr, "_load_sources", lambda: ({}, {}, sources))
    order = []
    hydrate_limits = []

    def collect(source, src, fetch_time, name_dict, client, **kwargs):
        hydrate_limits.append(kwargs.get("hydrate_limit"))
        if source == "sse_regulation":
            raise official.OfficialStructureError("sse changed")
        return [{"_id": source, "title": source, "content": source,
                 "raw_title": source, "raw_content": "{}",
                 "pub_time": "2026-08-28 00:00:00",
                 "fetch_time": fetch_time, "source": source,
                 "channel": src.channel, "url": src.url,
                 "stocks": [], "vec_status": "pending"}]

    monkeypatch.setattr(nr, "_collect_official_source", collect)
    monkeypatch.setattr(
        nr, "_archive_source",
        lambda source, rows: order.append(("archive", source)) or len(rows))
    monkeypatch.setattr(
        nr.osu, "bulk_create",
        lambda client, rows: order.append(("bulk", len(rows))) or (len(rows), 0))
    summary = nr.run_backfill("20260801", "20260829")
    assert "失败源[sse_regulation]" in summary
    assert "20260801~20260829" in summary
    assert [name for name, _value in order][-1] == "bulk"
    assert len(env.calls.alerts) == 1
    assert set(hydrate_limits) == {nr._MAX_OFFICIAL_BACKFILL_HYDRATE}


def test_load_sources_rejects_unknown_listpage_id(monkeypatch):
    """注册表登记了 listpage 源但代码个性表缺配置 → 响亮 raise（脱节 fail-fast）。"""
    from data_collect.utils.source_registry import Source
    ghost = Source(id="ghost", adapter="listpage", channel="policy",
                   job="news_regulator")
    monkeypatch.setattr(nr.source_registry, "load_sources",
                        lambda job, **kw: [ghost])
    with pytest.raises(RuntimeError, match="脱节"):
        nr._load_sources()
