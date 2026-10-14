"""采集适配器层（注册表三期，backlog 候选1）："怎么采"的统一实现与冒烟分派。

两层架构（适配器归代码、源归配置）的代码侧收口：
- feed 类（rss/rsshub 同为 feedparser 协议）单一实现 ``fetch_feed()``——收口
  news_policy/news_us/news_regulator 三处 feedparser+bozo 防御克隆。各 job 以
  模块级别名引用（如 ``_fetch_rss_entries = fetch_feed``）：既复用实现又保留
  原私有名，测试 monkeypatch 不破（news_common 同款约定）。
- ``hard_timeout()``：Source.timeout（HTTP 层）接线后，外层守护线程硬超时的
  配套推导——防 yaml 把 timeout 配得比外层地板还大时被外层先杀成死配置。
- ``smoke_test()``：manage_sources `test <id>` 的统一分派入口。
  listpage/akshare/api 个性逻辑仍归各 job/utils（勿配置化），冒烟实现函数内
  lazy import 引用（jobs → 本模块 → (lazy) jobs，无环）。
"""

from __future__ import annotations

import datetime
import dataclasses
import logging
import time
from urllib.parse import urlsplit

from data_collect.utils.news_common import fetch_with_timeout

logger = logging.getLogger(__name__)

DEFAULT_HTTP_TIMEOUT = 30   # fetch_feed 缺省（生产调用方显式传 Source.timeout）
SOURCE_RETRY_DELAY_SECONDS = 5


class FeedHTTPError(RuntimeError):
    """Feed HTTP 非 200；保留状态码供源级重试策略精确分类。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = int(status_code)


class RSSHubUnavailableError(RuntimeError):
    """有序 RSSHub 实例均遇到可降级故障；可由来源级边界重试整条链。"""


def _is_transient(exc: Exception) -> bool:
    """429/5xx 和 requests 连接/超时属短暂故障；其余立即上抛。"""
    if isinstance(exc, RSSHubUnavailableError):
        return True
    if isinstance(exc, FeedHTTPError):
        return exc.status_code == 429 or 500 <= exc.status_code <= 599
    try:
        from requests import exceptions as request_errors
    except ImportError:  # pragma: no cover - fetch_feed 本身也依赖 requests
        return False
    return isinstance(exc, (request_errors.ConnectionError,
                            request_errors.Timeout))


def retry_transient(fetch_fn, *, label="源", retries: int = 1,
                    delay: float = SOURCE_RETRY_DELAY_SECONDS):
    """对可恢复的源级网络错误有上限重试；默认 5s 后仅重试一次。"""
    attempt = 0
    while True:
        try:
            return fetch_fn()
        except Exception as exc:  # noqa: BLE001 - 分类后原样重抛
            if attempt >= retries or not _is_transient(exc):
                raise
            attempt += 1
            logger.warning(
                f"{label} 短暂失败（{exc!r}），{delay:g}s 后进行"
                f"第 {attempt} 次源级重试")
            time.sleep(delay)


def fetch_feed(url, *, headers=None, proxy="", timeout=DEFAULT_HTTP_TIMEOUT,
               label="feed") -> list:
    """拉取并解析 RSS/Atom → entries 列表（lazy import feedparser/requests）。

    防御（原三 job 克隆的共同语义）：非 200 抛错；bozo（解析半残）且无 entries
    抛错——半残 feed 静默产出空信封比失败更糟；bozo 但有 entries 视为可用
    （RSS 普遍有小毛病，feedparser 容错解析出的条目仍完整）。
    proxy 精确到源（空=直连）：不用全局环境变量——NO_PROXY 漏国内域名会重演
    cls 挂起事故（见 news_us 模块说明）。
    """
    import feedparser
    import requests

    kwargs: dict = {"headers": headers or {}, "timeout": timeout}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    resp = requests.get(url, **kwargs)
    if resp.status_code != 200:
        raise FeedHTTPError(
            resp.status_code, f"{label} HTTP {resp.status_code}: {url}")
    feed = feedparser.parse(resp.content)
    entries = list(feed.entries or [])
    if not entries and getattr(feed, "bozo", 0):
        raise RuntimeError(
            f"{label} 解析失败（bozo={getattr(feed, 'bozo_exception', None)!r}）: {url}")
    return entries


def hard_timeout(http_timeout, floor, attempts=1) -> float:
    """外层 fetch_with_timeout 硬超时：不低于 job 地板 floor，且给足 HTTP 超时
    +15s 解析余量（yaml 把 timeout 配大时外层跟随，不产生"配了也白配"死配置）。"""
    return max(floor, int(http_timeout) * max(1, int(attempts)) + 15)


@dataclasses.dataclass(frozen=True)
class RSSHubResult:
    entries: list
    base_index: int
    endpoint: str


def _normalize_rsshub_bases(values) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError("news.rsshub_bases 必须是非空 URL 列表")
    if not values:
        raise ValueError("news.rsshub_bases 不能为空")
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("RSSHub base 必须是非空字符串")
        raw = value.strip()
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"RSSHub base 端口非法: {raw!r}") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"RSSHub base 必须是 HTTP(S) URL: {raw!r}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("RSSHub base 不得包含用户凭据")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("RSSHub base 只能包含 scheme://host:port")
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        endpoint = f"{parsed.scheme}://{host}{f':{port}' if port is not None else ''}"
        if endpoint in normalized:
            raise ValueError(f"RSSHub base 重复: {endpoint}")
        normalized.append(endpoint)
    return tuple(normalized)


def get_rsshub_bases(news_cfg=None) -> tuple[str, ...]:
    """解析有序实例；新列表优先，未配置时兼容旧 ``rsshub_base``。"""
    if news_cfg is None:
        from data_collect.config import get_news_config
        news_cfg = get_news_config()
    if "rsshub_bases" in news_cfg:
        return _normalize_rsshub_bases(news_cfg.get("rsshub_bases"))
    legacy = str(news_cfg.get("rsshub_base") or "").strip()
    if not legacy:
        raise RuntimeError(
            "需配置 news.rsshub_bases 或兼容键 news.rsshub_base")
    return _normalize_rsshub_bases((legacy,))


def _can_failover(exc: Exception) -> bool:
    if isinstance(exc, FeedHTTPError):
        return exc.status_code in {404, 429} or 500 <= exc.status_code <= 599
    try:
        from requests import exceptions as request_errors
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, (request_errors.ConnectionError,
                            request_errors.Timeout))


def _failure_kind(exc: Exception) -> str:
    if isinstance(exc, FeedHTTPError):
        return f"HTTP {exc.status_code}"
    return type(exc).__name__


def fetch_rsshub(source, *, bases=None, route_suffix="") -> RSSHubResult:
    """依次请求 RSSHub 实例；只对网络/404/429/5xx 做实例级降级。"""
    ordered = (get_rsshub_bases() if bases is None
               else _normalize_rsshub_bases(tuple(bases)))
    if route_suffix and not str(route_suffix).startswith("?"):
        raise ValueError("RSSHub route_suffix 必须以 ? 开头")
    route = str(source.route or "")
    suffix = str(route_suffix or "")
    failures = []
    for index, endpoint in enumerate(ordered):
        try:
            entries = fetch_feed(
                f"{endpoint}{route}{suffix}", timeout=source.timeout,
                label=f"RSSHub {source.id}")
            return RSSHubResult(entries, index, endpoint)
        except Exception as exc:  # noqa: BLE001 -- 分类后决定降级或原样上抛
            if not _can_failover(exc):
                raise
            failures.append((endpoint, _failure_kind(exc)))
    summary = "; ".join(f"{endpoint}={kind}" for endpoint, kind in failures)
    raise RSSHubUnavailableError(
        f"RSSHub {source.id} 全部实例失败: {summary}")


# ======== 冒烟分派（manage_sources `test <id>` 统一入口） ========

_SMOKE_TIMEOUT = 60       # 冒烟统一硬超时（CLI 诊断工具不许挂死，评审 S2）


def _first_title(entries) -> str:
    return str((entries[0].get("title") if entries else "") or "")[:60]


def _smoke_rss(s) -> str:
    entries = fetch_feed(s.url, headers=s.headers, proxy=s.proxy_url,
                         timeout=s.timeout, label=f"rss {s.id}")
    return f"{len(entries)} 条 | 首条: {_first_title(entries)}"


def _smoke_rsshub(s) -> str:
    result = fetch_rsshub(s)
    return f"{len(result.entries)} 条 | 首条: {_first_title(result.entries)}"


def _smoke_listpage(s) -> str:
    """listpage 个性配置（link_re）在 news_regulator，lazy import 无环。"""
    from data_collect.jobs.news_regulator import _LISTPAGES, _extract_article_urls
    from data_collect.utils import fulltext
    cfg = _LISTPAGES.get(s.id)
    if cfg is None:
        from data_collect.jobs import news_regulator_official as official
        if official.supports_source(s.id):
            return fetch_with_timeout(
                lambda: official.smoke_source(s), _SMOKE_TIMEOUT,
                f"official {s.id} 冒烟")
        raise RuntimeError(f"listpage 源 {s.id} 在代码个性表 _LISTPAGES 无配置"
                           f"（注册表/代码脱节）")
    html = fetch_with_timeout(lambda: fulltext._http_get(cfg["list_url"]),
                              30, f"listpage {s.id} 冒烟")
    urls = _extract_article_urls(s.id, html or "")
    return f"列表页 {len(urls)} 链接 | 首条: {urls[0] if urls else '-'}"


def _smoke_akshare(s) -> str:
    """akshare 单源 job 冒烟：按 id 分派到 job 取数函数（真拉不归档不入库）。"""
    if s.id == "cctv":
        from data_collect.jobs.news_cctv import _fetch_cctv
        date8 = (datetime.date.today()
                 - datetime.timedelta(days=1)).strftime("%Y%m%d")
        df = fetch_with_timeout(lambda: _fetch_cctv(date8), _SMOKE_TIMEOUT,
                                f"akshare {s.id} 冒烟")
        return f"昨日({date8}) {0 if df is None else len(df)} 行"
    if s.id == "em_stock":
        from data_collect.jobs.news_stock import _fetch_stock_news
        df = fetch_with_timeout(lambda: _fetch_stock_news("000001"),
                                _SMOKE_TIMEOUT, f"akshare {s.id} 冒烟")
        return f"探针票 000001 {0 if df is None else len(df)} 条"
    if s.id == "em_cjzc":
        from data_collect.jobs.news_policy import _fetch_cjzc
        df = fetch_with_timeout(_fetch_cjzc, _SMOKE_TIMEOUT,
                                f"akshare {s.id} 冒烟")
        return f"财经早餐 {0 if df is None else len(df)} 行"
    raise NotImplementedError(f"akshare 源 {s.id} 无冒烟实现"
                              f"（在 source_adapters 补一个分支）")


def _smoke_api(s) -> str:
    if s.id == "cninfo":
        from data_collect.utils.cninfo import fetch_announcements
        date_iso = datetime.date.today().strftime("%Y-%m-%d")
        rows = fetch_with_timeout(lambda: fetch_announcements(date_iso), 120,
                                  f"api {s.id} 冒烟")
        return f"今日({date_iso}) {len(rows or [])} 条公告"
    from data_collect.jobs import news_regulator_official as official
    if official.supports_source(s.id):
        return fetch_with_timeout(
            lambda: official.smoke_source(s), _SMOKE_TIMEOUT,
            f"official {s.id} 冒烟")
    raise NotImplementedError(f"api 源 {s.id} 无冒烟实现"
                              f"（在 source_adapters 补一个分支）")


_SMOKE_FNS = {"rss": _smoke_rss, "rsshub": _smoke_rsshub,
              "listpage": _smoke_listpage, "akshare": _smoke_akshare,
              "api": _smoke_api}


def smoke_test(source) -> str:
    """单源冒烟（真拉一次，不归档不入库）：返回单行结果串，失败原样抛。

    与生产同路径：headers/proxy/timeout 全部取自 Source（加载器已解析）。
    """
    fn = _SMOKE_FNS.get(source.adapter)
    if fn is None:
        raise NotImplementedError(f"adapter={source.adapter} 冒烟未支持")
    return fn(source)
