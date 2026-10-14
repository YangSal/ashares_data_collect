"""source_adapters 单测：feed 统一抓取的防御契约 + 外层硬超时推导。"""

from types import SimpleNamespace

import pytest

from data_collect.utils import source_adapters as sa


class _Resp:
    def __init__(self, status=200, content=b"x"):
        self.status_code = status
        self.content = content


def _patch_requests(monkeypatch, resp, captured=None):
    import sys

    def get(url, **kw):
        if captured is not None:
            captured.update(kw, url=url)
        return resp

    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=get))


def _patch_feedparser(monkeypatch, entries, bozo=0):
    import sys
    feed = SimpleNamespace(entries=entries, bozo=bozo,
                           bozo_exception=ValueError("not xml"))
    monkeypatch.setitem(sys.modules, "feedparser",
                        SimpleNamespace(parse=lambda content: feed))


def test_fetch_feed_passes_headers_proxy_timeout(monkeypatch):
    """headers/proxy/timeout 全部透传（Source.timeout 接线的落点）。"""
    captured = {}
    _patch_requests(monkeypatch, _Resp(), captured)
    _patch_feedparser(monkeypatch, [{"title": "t"}])
    entries = sa.fetch_feed("https://x/f.xml", headers={"User-Agent": "ua"},
                            proxy="http://127.0.0.1:7890", timeout=42)
    assert entries == [{"title": "t"}]
    assert captured["headers"] == {"User-Agent": "ua"}
    assert captured["proxies"] == {"http": "http://127.0.0.1:7890",
                                   "https": "http://127.0.0.1:7890"}
    assert captured["timeout"] == 42


def test_fetch_feed_no_proxy_direct(monkeypatch):
    """空 proxy → 直连（不传 proxies，沿用环境行为——同原 news_us 语义）。"""
    captured = {}
    _patch_requests(monkeypatch, _Resp(), captured)
    _patch_feedparser(monkeypatch, [{"title": "t"}])
    sa.fetch_feed("https://x/f.xml")
    assert "proxies" not in captured


def test_fetch_feed_non_200_raises(monkeypatch):
    _patch_requests(monkeypatch, _Resp(status=503))
    with pytest.raises(RuntimeError, match="HTTP 503"):
        sa.fetch_feed("https://x/f.xml", label="RSS x")


def test_fetch_feed_bozo_and_empty_raises(monkeypatch):
    """bozo 且空 → 抛错（半残 feed 静默空信封比失败更糟，三 job 共同防御）。"""
    _patch_requests(monkeypatch, _Resp())
    _patch_feedparser(monkeypatch, [], bozo=1)
    with pytest.raises(RuntimeError, match="bozo"):
        sa.fetch_feed("https://x/f.xml")


def test_fetch_feed_bozo_with_entries_ok(monkeypatch):
    """bozo 但有 entries 视为可用（RSS 普遍有小毛病）。"""
    _patch_requests(monkeypatch, _Resp())
    _patch_feedparser(monkeypatch, [{"title": "t"}], bozo=1)
    assert len(sa.fetch_feed("https://x/f.xml")) == 1


def test_hard_timeout_floor_and_headroom():
    assert sa.hard_timeout(30, 60) == 60     # 常规：job 地板兜底
    assert sa.hard_timeout(90, 60) == 105    # yaml 配大超时 → 外层跟随，防死配置
    assert sa.hard_timeout(45, 60, attempts=2) == 105


def test_retry_transient_recovers_on_second_attempt(monkeypatch):
    """短暂 503 第二次恢复 → 返回成功结果，不向调用方暴露失败。"""
    attempts = 0
    delays = []

    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sa.FeedHTTPError(503, "RSSHub x HTTP 503: http://rh/x")
        return ["ok"]

    monkeypatch.setattr(sa.time, "sleep", lambda seconds: delays.append(seconds))
    assert sa.retry_transient(flaky, label="监管源 x") == ["ok"]
    assert attempts == 2
    assert delays == [5]


def test_retry_transient_does_not_retry_parse_error(monkeypatch):
    """内容/解析错误不是网络抖动 → 立即上抛，避免无意义重压源站。"""
    attempts = 0

    def broken():
        nonlocal attempts
        attempts += 1
        raise ValueError("schema changed")

    monkeypatch.setattr(sa.time, "sleep",
                        lambda seconds: pytest.fail("不应等待重试"))
    with pytest.raises(ValueError, match="schema changed"):
        sa.retry_transient(broken, label="监管源 x")
    assert attempts == 1


def test_retry_transient_raises_after_one_retry(monkeypatch):
    """连续读取超时 → 只重试一次，仍失败则交回源级隔离逻辑。"""
    from requests.exceptions import ReadTimeout

    attempts = 0

    def timed_out():
        nonlocal attempts
        attempts += 1
        raise ReadTimeout("read timed out")

    monkeypatch.setattr(sa.time, "sleep", lambda seconds: None)
    with pytest.raises(ReadTimeout, match="read timed out"):
        sa.retry_transient(timed_out, label="政策源 stats")
    assert attempts == 2


# ---------- 冒烟分派（manage_sources `test <id>` 统一入口） ----------

from data_collect.utils.source_registry import Source  # noqa: E402


def test_smoke_dispatch_rss(monkeypatch):
    monkeypatch.setattr(sa, "fetch_feed",
                        lambda url, **kw: [{"title": "首条标题"}])
    s = Source(id="x", adapter="rss", channel="policy", job="news_policy",
               url="https://x/rss")
    out = sa.smoke_test(s)
    assert "1 条" in out and "首条标题" in out


def test_smoke_dispatch_rsshub(monkeypatch):
    captured = []
    monkeypatch.setattr(
        sa, "fetch_rsshub",
        lambda source, **kwargs: (
            captured.append(source.id),
            sa.RSSHubResult([{"title": "t"}], 0, "http://rh:1200"))[1])
    s = Source(id="x", adapter="rsshub", channel="media",
               job="news_regulator", route="/r")
    assert "1 条" in sa.smoke_test(s)
    assert captured == ["x"]


def test_smoke_rsshub_requires_base(monkeypatch):
    import data_collect.config as cfg
    monkeypatch.setattr(cfg, "get_news_config", lambda: {})
    s = Source(id="x", adapter="rsshub", channel="media",
               job="news_regulator", route="/r")
    with pytest.raises(RuntimeError, match="rsshub_base"):
        sa.smoke_test(s)


def test_smoke_listpage_missing_config_raises():
    s = Source(id="ghost", adapter="listpage", channel="policy",
               job="news_regulator")
    with pytest.raises(RuntimeError, match="脱节"):
        sa.smoke_test(s)


@pytest.mark.parametrize("source_id,adapter", [
    ("xinhua_fortune", "listpage"),
    ("gov_bmwj", "listpage"),
    ("sse_regulation", "listpage"),
    ("miit_policy", "api"),
    ("bse_regulation", "api"),
])
def test_smoke_dispatch_official_collectors(monkeypatch, source_id, adapter):
    """官方源若仍落入旧 listpage/cninfo 分支，冒烟会报脱节。"""
    from data_collect.jobs import news_regulator_official as official
    calls = []
    timeouts = []
    monkeypatch.setattr(
        official, "smoke_source",
        lambda source: calls.append(source.id) or f"{source.id} 3 条")
    monkeypatch.setattr(
        sa, "fetch_with_timeout",
        lambda fn, timeout, label: timeouts.append(timeout) or fn())
    source = Source(id=source_id, adapter=adapter, channel="policy",
                    job="news_regulator", url="https://example.cn/list")
    assert sa.smoke_test(source) == f"{source_id} 3 条"
    assert calls == [source_id]
    assert timeouts == [sa._SMOKE_TIMEOUT]


def test_smoke_dispatch_em_cjzc(monkeypatch):
    from data_collect.jobs import news_policy

    monkeypatch.setattr(news_policy, "_fetch_cjzc", lambda: ["a", "b"])
    s = Source(id="em_cjzc", adapter="akshare", channel="media",
               job="news_policy")

    assert sa.smoke_test(s) == "财经早餐 2 行"


def test_smoke_unknown_adapter_raises():
    s = Source(id="x", adapter="ftp", channel="policy", job="news_policy")
    with pytest.raises(NotImplementedError):
        sa.smoke_test(s)


# ---------- RSSHub ordered bases / failover ----------

def test_get_rsshub_bases_prefers_ordered_list_and_supports_legacy_scalar():
    assert sa.get_rsshub_bases({
        "rsshub_bases": ["http://a:1200/", "https://b:443"]}) == (
            "http://a:1200", "https://b:443")
    assert sa.get_rsshub_bases({
        "rsshub_base": "http://old:1200/"}) == ("http://old:1200",)


@pytest.mark.parametrize("news_cfg", [
    {"rsshub_bases": "http://a:1200"},
    {"rsshub_bases": []},
    {"rsshub_bases": ["http://a:1200", "http://a:1200/"]},
    {"rsshub_bases": ["http://user:pass@a:1200"]},
    {"rsshub_bases": ["ftp://a:1200"]},
    {"rsshub_bases": ["http://a:1200/prefix"]},
])
def test_get_rsshub_bases_rejects_unsafe_or_ambiguous_values(news_cfg):
    with pytest.raises(ValueError):
        sa.get_rsshub_bases(news_cfg)


def _rsshub_source():
    return Source(id="ndrc", adapter="rsshub", channel="policy",
                  job="news_regulator", route="/gov/ndrc/xwdt", timeout=9)


def test_fetch_rsshub_primary_success_does_not_touch_backup(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sa, "fetch_feed",
        lambda url, **kwargs: calls.append((url, kwargs)) or [{"title": "ok"}])

    result = sa.fetch_rsshub(
        _rsshub_source(), bases=("http://primary:1200", "http://backup:1200"))

    assert result.entries == [{"title": "ok"}]
    assert result.base_index == 0
    assert result.endpoint == "http://primary:1200"
    assert [call[0] for call in calls] == [
        "http://primary:1200/gov/ndrc/xwdt"]
    assert calls[0][1]["timeout"] == 9


@pytest.mark.parametrize("first_error", [
    sa.FeedHTTPError(503, "contains route and token"),
    sa.FeedHTTPError(404, "route absent"),
    sa.FeedHTTPError(429, "rate limited"),
])
def test_fetch_rsshub_http_failure_uses_backup(monkeypatch, first_error):
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise first_error
        return [{"title": "backup"}]

    monkeypatch.setattr(sa, "fetch_feed", fetch)
    result = sa.fetch_rsshub(
        _rsshub_source(), bases=("http://primary:1200", "http://backup:1200"))

    assert result.base_index == 1
    assert result.endpoint == "http://backup:1200"
    assert len(calls) == 2


def test_fetch_rsshub_connection_failure_uses_backup(monkeypatch):
    from requests.exceptions import ConnectionError

    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise ConnectionError("primary refused")
        return []

    monkeypatch.setattr(sa, "fetch_feed", fetch)
    result = sa.fetch_rsshub(
        _rsshub_source(), bases=("http://primary:1200", "http://backup:1200"))

    assert result.entries == []
    assert result.base_index == 1
    assert len(calls) == 2


def test_fetch_rsshub_legal_empty_primary_does_not_failover(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sa, "fetch_feed", lambda url, **kwargs: calls.append(url) or [])

    result = sa.fetch_rsshub(
        _rsshub_source(), bases=("http://primary:1200", "http://backup:1200"))

    assert result.entries == []
    assert result.base_index == 0
    assert len(calls) == 1


@pytest.mark.parametrize("error", [
    sa.FeedHTTPError(403, "forbidden"),
    ValueError("schema changed"),
])
def test_fetch_rsshub_non_failover_errors_stop_immediately(monkeypatch, error):
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        raise error

    monkeypatch.setattr(sa, "fetch_feed", fetch)
    with pytest.raises(type(error)):
        sa.fetch_rsshub(
            _rsshub_source(),
            bases=("http://primary:1200", "http://backup:1200"))
    assert len(calls) == 1


def test_fetch_rsshub_all_failures_raise_sanitized_retryable_error(monkeypatch):
    from requests.exceptions import ConnectionError

    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise sa.FeedHTTPError(503, f"HTTP 503: {url}?token=secret")
        raise ConnectionError("proxy password=hunter2")

    monkeypatch.setattr(sa, "fetch_feed", fetch)
    with pytest.raises(sa.RSSHubUnavailableError) as caught:
        sa.fetch_rsshub(
            _rsshub_source(),
            bases=("http://primary:1200", "http://backup:1200"),
            route_suffix="?limit=10")

    message = str(caught.value)
    assert "http://primary:1200" in message
    assert "http://backup:1200" in message
    assert "/gov/" not in message
    assert "secret" not in message
    assert "hunter2" not in message
    assert sa._is_transient(caught.value) is True
