"""监管/部委/官媒采集任务（新闻子系统 B 档：RSSHub + 列表页自写解析混合）。

源表驱动：rsshub 源与 listpage 开关在 sources.yaml 注册表（加 rsshub 源零代码，见
spec 2026-07-08）；listpage 个性解析（link_re 等）仍在本文件 _LISTPAGES。
- rsshub 类（注册表 adapter=rsshub → `_load_sources()`）：按 config `news.rsshub_bases`
  有序尝试自建 RSSHub（兼容旧 `rsshub_base`）→ feedparser → 复用 news_policy 信封。零解析；
  路由腐烂只影响该源（源级隔离+告警）。当前：发改委 `/gov/ndrc/xwdt`（2026-07-08
  实测 200/25 条；csrc/pbc/mof/xinhuanet 路由已腐烂 503，走 listpage）。
- listpage 类（`_LISTPAGES`，自写解析——用户授权）：列表页 → 每源一条链接正则
  抽文章 URL → **抓前去重**（`_id=sha1(url)` ids 查询，只抓新文章，单轮 ≤20 篇/源）
  → 逐篇抓 HTML（0.5s pace，单篇隔离）→ 标题取 <title> 剥站名后缀、**正文交
  trafilatura**（复用 utils/fulltext——自写面只有链接正则，正文解析零自写）。

健壮性全套：fetch_with_timeout 硬超时 + 源级隔离 + guarded 告警 + 全源失败 raise
+ create-only 幂等 + 归档先行（复用 news_policy._archive_source）+ verify 归档重放。
"""

from __future__ import annotations

import logging
import json
import re
import time
from typing import Dict, List

from data_collect.config import get_news_config
from data_collect.utils import fulltext
from data_collect.utils import news_archive
from data_collect.utils import news_health
from data_collect.utils import news_normalize as nn
from data_collect.utils import notify
from data_collect.utils import opensearch_utils as osu
from data_collect.utils.news_common import fetch_with_timeout
from data_collect.utils.news_common import flush_spool_safe as _flush_spool_safe
from data_collect.utils.news_common import now as _now
from data_collect.utils.news_common import strip_raw as _strip_raw
from data_collect.utils.news_common import today as _today
from data_collect.utils.news_common import verify_dates as _verify_dates
from data_collect.utils import source_adapters as sa
from data_collect.utils import source_registry
from data_collect.jobs import news_regulator_official as official

# 信封（rsshub 类）与分日归档复用 news_policy（同为 feed/信封源，单一定义）
from data_collect.jobs.news_policy import _archive_source
from data_collect.jobs.news_policy import _entry_to_envelope

logger = logging.getLogger(__name__)

_RSS_TIMEOUT_SECONDS = 60      # rsshub 类单源硬超时
_LIST_TIMEOUT_SECONDS = 180    # listpage 类整源硬超时（含逐篇抓取）
_ARTICLE_PACE = 2.0            # 分条文章页间限速（csrc 0.5s 连抓 20 篇实测触发 WAF 限流）
_MAX_NEW_PER_ROUND = 20        # 单轮单源新文章上限（首轮/积压防打爆）
_MAX_OFFICIAL_HYDRATE = 10     # 新华/Gov 单轮正文请求更保守（用户明确要求保护源站）
_MAX_OFFICIAL_BACKFILL_HYDRATE = 200  # 单命令正文请求硬上限；缩窗可续跑
_CONTENT_MAX_CHARS = 50_000    # 正文截断（与全文回填约定一致）
_MIN_CONTENT_CHARS = 120       # 正文过短（挑战页/空壳页）→ 拒收，同 URL 下轮重采

# listpage 类源：source -> 配置（链接正则=唯一自写解析面；2026-07-08 实测选择器）
_LISTPAGES: Dict[str, dict] = {
    "csrc": {
        "list_url": "http://www.csrc.gov.cn/csrc/c100028/common_list.shtml",
        "base": "http://www.csrc.gov.cn",
        "link_re": re.compile(r'href="((?:https?://www\.csrc\.gov\.cn)?'
                              r'/csrc/c\d+/c\d+/content\.shtml)"'),
        "title_suffixes": ("_中国证券监督管理委员会",),
        "channel": "policy",
    },
    # pbc（央行）已移除：文章正文 JS 注入，trafilatura 抽取恒空 → 门卫拒收 0 条，
    # 但因未入库、抓前去重不命中，每小时重抓同批 URL 白锤 WAF（2026-07-08 审查）。
    # 结构性无产出的源不留在表里空转；央行货币政策经政府网/快讯/参考消息覆盖。
    # ---- 第二批（2026-07-08 实测：mof 4 链接、people 16 链接；新华网三入口
    #      全为 JS 壳(580B)，列表页方案不可行——已知限制，其要闻经政府网/快讯覆盖）
    "mof": {
        "list_url": "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/",
        "base": "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen",  # ./ 相对链接的目录
        "link_re": re.compile(r'href="(\./\d{6}/t\d+_\d+\.htm)"'),
        "title_suffixes": ("_中华人民共和国财政部", "_财政部"),
        "channel": "policy",
    },
    "people": {
        "list_url": "http://finance.people.com.cn/",
        "base": "http://finance.people.com.cn",
        "link_re": re.compile(r'href="((?:https?://finance\.people\.com\.cn)?'
                              r'/n1/20\d{2}/\d{4}/c\d+-\d+\.html)"'),
        "title_suffixes": ("--经济·科技--人民网", "--人民网", "_人民网"),
        "channel": "media",
    },
}
def _load_sources() -> tuple:
    """一次加载注册表 → (rsshub, 旧 listpage, 官方直连 Source)。

    **单次 load_all**：原先 rsshub/listpage 各一个 shim、run() 各调一次 = 每轮 2 次磁盘
    读 + 2 次 last-good 重拷（hourly 任务，评审 S1 同族浪费），合并为一次遍历。
    listpage 交叉校验（spec §13）：注册表登记但代码 _LISTPAGES 缺个性配置 → raise
    （注册表/代码脱节 fail-fast）。停用源（enabled: false）由 load_sources 自然排除。
    函数化 = 热生效：子进程每轮重读；测试 monkeypatch 本函数即解耦。
    """
    routes: Dict[str, source_registry.Source] = {}
    listpages: Dict[str, dict] = {}
    officials: Dict[str, source_registry.Source] = {}
    for s in source_registry.load_sources("news_regulator"):
        if s.adapter == "rsshub":
            routes[s.id] = s
        elif s.adapter == "listpage":
            if s.id in _LISTPAGES:
                listpages[s.id] = _LISTPAGES[s.id]
            elif official.supports_source(s.id):
                officials[s.id] = s
            else:
                raise RuntimeError(
                    f"注册表 listpage 源 {s.id} 在代码个性表 _LISTPAGES 无配置"
                    f"（注册表/代码脱节）")
        elif s.adapter == "api":
            if not official.supports_source(s.id):
                raise RuntimeError(
                    f"注册表 api 源 {s.id} 无官方采集实现（注册表/代码脱节）")
            officials[s.id] = s
    return routes, listpages, officials

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_DATE_CORE = r"(20\d{2})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
# 优先：紧跟"发布时间/来源/日期/时间"标签的日期（正文发布时间，非导航/版权）
_TIME_LABELED_RE = re.compile(r"(?:发布时间|来源|日期|时间)[：:\s]{0,6}" + _DATE_CORE)
# 兜底：HTML 内首个日期（可能是版权/导航日期，故仅在无标签命中时用）
_TIME_SNIFF_RE = re.compile(_DATE_CORE)


def _alert(message: str) -> bool:
    """钉钉告警（guarded：通知失败不影响采集主流程）。"""
    return notify.guarded_send(message)


def _load_name_dict() -> Dict[str, str]:
    """简称词典（降级契约同快讯：失败退空典仅正则打标，不阻塞采集）。"""
    try:
        return nn.load_name_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"监管简称词典加载失败，本轮打标降级: {exc!r}")
        return {}


# ======== rsshub 类 ========

# RSSHub 有序实例抓取单一实现在适配器层；别名保留供测试 monkeypatch。
_fetch_rsshub = sa.fetch_rsshub


# ======== listpage 类 ========

def _extract_article_urls(source: str, list_html: str) -> List[str]:
    """列表页 → 文章绝对 URL 列表（去重保序）。每源唯一的自写解析面。"""
    cfg = _LISTPAGES[source]
    urls: List[str] = []
    seen: set = set()
    for match in cfg["link_re"].findall(list_html):
        if match.startswith("./"):          # 目录相对链接（mof）：剥 "." 拼列表页目录
            match = match[1:]
        url = match if match.startswith("http") else f"{cfg['base']}{match}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _sniff_pub_time(html: str) -> str | None:
    """HTML 内嗅探发布时间 → 北京时间串；嗅探不到返回 None。

    优先取紧跟"发布时间/来源/日期/时间"标签的日期（正文发布时间），无标签命中
    才回退首个日期——防把导航/版权栏日期误当发布时间（2026-07-08 审查）。
    """
    m = _TIME_LABELED_RE.search(html) or _TIME_SNIFF_RE.search(html)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    return (f"{y}-{int(mo):02d}-{int(d):02d} "
            f"{int(hh or 0):02d}:{mm or '00'}:{ss or '00'}")


def _article_envelope(source: str, channel: str, url: str, html: str,
                      fetch_time: str, name_dict: Dict[str, str]) -> dict | None:
    """文章页 → 信封；抽取失败/过短/挑战页返回 None（单篇隔离，调用方跳过）。

    **挑战页门卫**（2026-07-08 csrc 实撞）：WAF 限流后服务器改发首页/挑战页——
    title 只剩站名。若不拒收，垃圾文档占住 `_id=sha1(url)`，create-only 下正确
    版本永远进不来（只能人工删）。拒收即"未采"，同 URL 下轮限流冷却后重采。
    """
    cfg = _LISTPAGES[source]
    title_m = _TITLE_RE.search(html)
    raw_title = title_m.group(1).strip() if title_m else ""
    title = raw_title
    for suffix in cfg["title_suffixes"]:
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    title = nn.clean_text(title)
    site_names = {s.lstrip("_") for s in cfg["title_suffixes"]}
    if not title or title in site_names:      # 挑战页/跳首页：标题即站名 → 拒收
        logger.debug(f"{source} 疑似挑战页（title={raw_title[:30]!r}），拒收: {url}")
        return None

    try:
        body = fulltext._extract(html, url)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"{source} 正文抽取失败 {url}: {exc!r}")
        return None
    content = nn.clean_text(body or "")[:_CONTENT_MAX_CHARS]
    if len(content) < _MIN_CONTENT_CHARS:     # 空壳/过短 → 拒收（下轮重采）
        return None

    pub_time = _sniff_pub_time(html)
    envelope = {
        "_id": nn.make_id({"source": source, "url": url,
                           "pub_time": pub_time, "title": raw_title}),
        "title": title,
        "content": content,
        "raw_title": raw_title,
        "raw_content": "",            # 原文即 HTML（过大），归档存信封即可
        "pub_time": pub_time or fetch_time,
        "fetch_time": fetch_time,
        "source": source,
        "channel": channel,
        "url": url,
        "stocks": nn.tag_stocks(f"{title} {content}", name_dict),
        "vec_status": "pending",
    }
    if pub_time is None:
        envelope["time_estimated"] = True
    return envelope


def _existing_ids(client, ids: List[str]) -> set:
    """已入库 `_id` 集合（ids 查询走别名；查询异常按空集=宁可重抓，幂等无害）。"""
    if not ids:
        return set()
    try:
        resp = osu.search_raw(client, {"query": {"ids": {"values": ids}},
                                       "size": len(ids), "_source": False})
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"监管抓前去重查询失败（按空集重抓，幂等无害）: {exc!r}")
        return set()
    hits = ((resp or {}).get("hits") or {}).get("hits") or []
    return {h["_id"] for h in hits}


def _official_item_id(item) -> str:
    return nn.make_id({
        "source": item.source,
        "native_id": item.native_id or None,
        "url": item.url,
        "pub_time": item.pub_time,
        "title": item.title,
    })


def _existing_source_urls(client, source: str, urls: List[str]) -> set:
    """兼容旧 ID 的 URL 去重；查询失败须终止该源，避免迁移时产生副本。"""
    if not urls:
        return set()
    response = osu.search_raw(client, {
        "query": {"bool": {"filter": [
            {"term": {"source": source}}, {"terms": {"url": urls}}]}},
        "size": 0,
        "aggs": {"urls": {"terms": {"field": "url", "size": len(urls)}}},
    })
    if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
        raise RuntimeError(f"{source} URL 去重查询未完整返回")
    return {bucket["key"] for bucket in response["aggregations"]["urls"]["buckets"]}


def _official_item_to_envelope(item, channel: str, fetch_time: str,
                               name_dict: Dict[str, str]) -> dict:
    """官方结构化条目 → 既有不可变新闻信封。"""
    title = nn.clean_text(item.title)
    content = nn.clean_text(item.content) or nn.clean_text(
        f"{item.category}：{item.title}" if item.category else item.title)
    envelope = {
        "_id": _official_item_id(item),
        "title": title,
        "content": content,
        "raw_title": item.title,
        "raw_content": json.dumps(
            item.raw, ensure_ascii=False, default=str),
        "pub_time": item.pub_time or fetch_time,
        "fetch_time": fetch_time,
        "source": item.source,
        "channel": channel,
        "url": item.url,
        "stocks": nn.tag_stocks(f"{title} {content}", name_dict),
        "vec_status": "pending",
    }
    if not item.pub_time:
        envelope["time_estimated"] = True
    return envelope


def _collect_official_source(
        source: str, src: source_registry.Source, fetch_time: str,
        name_dict: Dict[str, str], client, *, start=None, end=None,
        backfill: bool = False, http_client=None,
        hydrate_limit: int | None = _MAX_OFFICIAL_HYDRATE) -> List[dict]:
    """官方直连单源：发现元数据，新华/Gov 仅对未知 URL 低速补正文。"""
    http = http_client or official.PoliteClient(timeout=src.timeout)
    items = official.discover_source(
        source, http, start, end, backfill=backfill)

    if source in {"miit_policy", "gov_bmwj", "govcn_gwy"}:
        urls = [item.url for item in items]
        # 旧 Gov RSS 曾保存 http URL；来源范围内同时查询两个协议。
        if source.startswith("gov"):
            urls += [url.replace("https://", "http://", 1) for url in urls]
        existing_urls = _existing_source_urls(
            client, source, urls)
        existing_urls = {url.replace("http://", "https://", 1) for url in existing_urls}
        items = [item for item in items if item.url.replace("http://", "https://", 1)
                 not in existing_urls]

    if source in {"xinhua_fortune", "gov_bmwj", "govcn_gwy", "thepaper"}:
        ids = [_official_item_id(item) for item in items]
        existing = _existing_ids(client, ids)
        candidates = [item for item, item_id in zip(items, ids)
                      if item_id not in existing]
        if source == "thepaper" and not backfill:
            hydrate_limit = _MAX_NEW_PER_ROUND
        if hydrate_limit is not None:
            candidates = candidates[:max(0, int(hydrate_limit))]
        hydrated = []
        for item in candidates:
            if source == "thepaper":
                from data_collect.jobs.news_regulator_current import hydrate_thepaper
                full_item = hydrate_thepaper(http, item)
            elif source in {"gov_bmwj", "govcn_gwy"}:
                from data_collect.jobs.news_regulator_current import hydrate_gov
                full_item = hydrate_gov(http, item)
            else:
                full_item = official.hydrate_article(http, item)
            if full_item is not None:
                hydrated.append(full_item)
        items = hydrated
        if source in {"thepaper", "gov_bmwj", "govcn_gwy"} and candidates and not items:
            raise RuntimeError(f"{source} 本轮候选正文全部不可用，未入库，留待重试")

    envelopes: List[dict] = []
    seen: set[str] = set()
    for item in items:
        envelope = _official_item_to_envelope(
            item, src.channel, fetch_time, name_dict)
        if envelope["_id"] in seen:
            continue
        seen.add(envelope["_id"])
        envelopes.append(envelope)
    logger.info(f"监管源 {source} 本轮 {len(envelopes)} 条")
    return envelopes


def _collect_listpage(source: str, fetch_time: str,
                      name_dict: Dict[str, str], client) -> List[dict]:
    """listpage 类单源：列表页 → 抓前去重 → 逐篇抓取信封化。"""
    cfg = _LISTPAGES[source]
    list_html = fulltext._http_get(cfg["list_url"])
    if not list_html:
        raise RuntimeError(f"{source} 列表页抓取失败: {cfg['list_url']}")
    urls = _extract_article_urls(source, list_html)
    if not urls:
        raise RuntimeError(f"{source} 列表页 0 文章链接（疑改版）: {cfg['list_url']}")

    ids = [nn.make_id({"source": source, "url": u}) for u in urls]
    existing = _existing_ids(client, ids)
    new_urls = [u for u, i in zip(urls, ids) if i not in existing][:_MAX_NEW_PER_ROUND]
    logger.info(f"监管源 {source} 列表 {len(urls)} 篇，其中新 {len(new_urls)} 篇")

    envelopes: List[dict] = []
    for url in new_urls:
        html = fulltext._http_get(url)
        if html:
            env = _article_envelope(source, cfg["channel"], url, html,
                                    fetch_time, name_dict)
            if env:
                envelopes.append(env)
            else:
                logger.debug(f"监管源 {source} 单篇信封化失败（跳过）: {url}")
        else:
            logger.debug(f"监管源 {source} 单篇抓取失败（跳过）: {url}")
        time.sleep(_ARTICLE_PACE)
    return envelopes


def _collect_source(source: str, fetch_time: str, name_dict: Dict[str, str],
                    client, routes: Dict[str, source_registry.Source],
                    officials: Dict[str, source_registry.Source] | None = None, *,
                    bases=None, transport_statuses=None) -> List[dict]:
    """单源采集分派（硬超时包裹）；异常由调用方按源隔离。

    routes 由 run() 一次加载传入（勿在此调 shim——26 源循环内每调一次=一次磁盘读
    +last-good 重拷，评审 S1）。listpage 分支只会收到 _load_sources() 里的启用名字。
    """
    officials = officials or {}
    if source in routes:
        src = routes[source]
        if isinstance(bases, BaseException):
            raise RuntimeError(f"{source} RSSHub 实例配置不可用: {bases}") from bases
        ordered_bases = bases if bases is not None else sa.get_rsshub_bases()
        route_suffix = "?limit=10" if source == "jiemian" else ""
        result = fetch_with_timeout(
            lambda: _fetch_rsshub(
                src, bases=ordered_bases, route_suffix=route_suffix),
            sa.hard_timeout(
                src.timeout, _RSS_TIMEOUT_SECONDS, attempts=len(ordered_bases)),
            f"监管源 {source} 拉取")
        if transport_statuses is not None:
            transport_statuses[source] = (
                "primary" if result.base_index == 0 else "failover")
        entries = result.entries
        envelopes: List[dict] = []
        seen: set = set()
        for entry in (entries or []):
            env = _entry_to_envelope(source, src.channel, entry, fetch_time,
                                     name_dict)
            if env["_id"] in seen:
                continue
            seen.add(env["_id"])
            envelopes.append(env)
        logger.info(f"监管源 {source} 本轮 {len(envelopes)} 条")
        return envelopes

    if source in officials:
        src = officials[source]
        return fetch_with_timeout(
            lambda: _collect_official_source(
                source, src, fetch_time, name_dict, client),
            sa.hard_timeout(src.timeout, _LIST_TIMEOUT_SECONDS),
            f"监管源 {source} 官方直连拉取")

    return fetch_with_timeout(
        lambda: _collect_listpage(source, fetch_time, name_dict, client),
        _LIST_TIMEOUT_SECONDS, f"监管源 {source} 拉取")


# ======== Pipeline 标准接口 ========

def run(run_date: str | None = None, **kwargs) -> str:
    """每小时任务：三源采集 → 归档 → create-only 入库（源级隔离+guarded 告警）。"""
    _flush_spool_safe()
    fetch_time = _now().strftime("%Y-%m-%d %H:%M:%S")
    name_dict = _load_name_dict()
    client = osu.get_client()

    routes, listpages, officials = _load_sources()  # 每轮注册表只加载一次
    sources = tuple(routes) + tuple(listpages) + tuple(officials)
    try:
        rsshub_bases = (sa.get_rsshub_bases(get_news_config())
                        if routes else ())
    except Exception as exc:  # noqa: BLE001 -- RSSHub 源失败，listpage 仍继续
        rsshub_bases = exc
    transport_statuses: Dict[str, str] = {}
    envelopes_by_source: Dict[str, List[dict]] = {}
    failed_sources: List[str] = []
    errors: List[str] = []
    for source in sources:
        try:
            envelopes_by_source[source] = sa.retry_transient(
                lambda source=source: _collect_source(
                    source, fetch_time, name_dict, client, routes,
                    officials,
                    bases=rsshub_bases,
                    transport_statuses=transport_statuses),
                label=f"监管源 {source}")
            if source in routes:
                status = transport_statuses[source]
                news_health.observe(
                    source, "transport", status, enabled=routes[source].enabled,
                    detail=f"RSSHub {status}")
            elif source in officials:
                news_health.observe(
                    source, "transport", "primary",
                    enabled=officials[source].enabled,
                    detail="official direct primary")
        except Exception as exc:  # noqa: BLE001
            failed_sources.append(source)
            errors.append(f"{source}: {exc!r}")
            logger.warning(f"监管源 {source} 采集失败（源级隔离，继续其余源）: {exc!r}")
            if source in routes:
                news_health.observe(
                    source, "transport", "failed", enabled=routes[source].enabled,
                    detail=f"{type(exc).__name__}: {exc}")
            elif source in officials:
                news_health.observe(
                    source, "transport", "failed",
                    enabled=officials[source].enabled,
                    detail=f"{type(exc).__name__}: {exc}")
    if len(failed_sources) == len(sources):
        raise RuntimeError(f"监管源全部采集失败: {'; '.join(errors)}")
    if failed_sources:
        _alert(f"监管源 {','.join(failed_sources)} 本轮采集失败（其余源正常）: "
               f"{'; '.join(errors)[:300]}")

    new_counts = {source: _archive_source(source, envelopes)
                  for source, envelopes in envelopes_by_source.items()}

    all_envelopes = [env for source in sources
                     for env in envelopes_by_source.get(source, [])]
    ok = dup = 0
    if all_envelopes:
        ok, dup = osu.bulk_create(client,
                                  [_strip_raw(env) for env in all_envelopes])

    parts = []
    for source in sources:
        if source in failed_sources:
            parts.append(f"{source} 失败")
        else:
            parts.append(f"{source} {len(envelopes_by_source[source])}条"
                         f"(新{new_counts[source]})")
    summary = f"监管: {' '.join(parts)}, 入库新增 {ok} 重复 {dup}"
    if failed_sources:
        summary += f", 失败源[{','.join(failed_sources)}]"
    failover_sources = [source for source in routes
                        if transport_statuses.get(source) == "failover"]
    if failover_sources:
        summary += f", 备用实例[{','.join(failover_sources)}]"
    logger.info(summary)
    return summary


def run_verify(start_date: str, end_date: str, **kwargs) -> str:
    """查漏补缺：归档重放 → create-only 补库（自然日语义，同 news_policy）。"""
    days_back = int(get_news_config().get("verify_days_back", 3))
    dates = _verify_dates(days_back, _today(), include_today=True)
    window = (dates[0], dates[-1])

    routes, listpages, officials = _load_sources()
    replayed: List[dict] = []
    for source in (*routes, *listpages, *officials):
        try:
            replayed.extend(news_archive.replay(source, window))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"监管 verify 重放 {source} 失败（跳过该源）: {exc!r}")

    ok = dup = 0
    if replayed:
        ok, dup = osu.bulk_create(osu.get_client(),
                                  [_strip_raw(env) for env in replayed])
    summary = (f"监管 verify({window[0]}~{window[1]}): 重放 {len(replayed)} 条, "
               f"补录 {ok} 重复 {dup}")
    logger.info(summary)
    return summary


def run_backfill(start_date: str, end_date: str, **kwargs) -> str:
    """低速回补本期官方直连源；窗口最多 90 个自然日。"""
    start, end = official.validate_window(start_date, end_date)
    _flush_spool_safe()
    fetch_time = _now().strftime("%Y-%m-%d %H:%M:%S")
    name_dict = _load_name_dict()
    client = osu.get_client()
    _routes, _listpages, officials = _load_sources()
    selected = {
        source: src for source, src in officials.items()
        if source in official.BACKFILL_SOURCE_IDS
    }
    if not selected:
        raise RuntimeError("监管官方源回补无已启用来源")

    envelopes_by_source: Dict[str, List[dict]] = {}
    failed_sources: List[str] = []
    errors: List[str] = []
    for source, src in selected.items():
        try:
            envelopes_by_source[source] = _collect_official_source(
                source, src, fetch_time, name_dict, client,
                start=start, end=end, backfill=True,
                hydrate_limit=_MAX_OFFICIAL_BACKFILL_HYDRATE)
            news_health.observe(
                source, "transport", "primary", enabled=src.enabled,
                detail="official direct backfill primary")
        except Exception as exc:  # noqa: BLE001 -- 源级隔离，保留其余成果
            failed_sources.append(source)
            errors.append(f"{source}: {exc!r}")
            logger.warning(
                f"监管官方源 {source} 回补失败（继续其余源）: {exc!r}")
            news_health.observe(
                source, "transport", "failed", enabled=src.enabled,
                detail=f"backfill {type(exc).__name__}: {exc}")

    if len(failed_sources) == len(selected):
        raise RuntimeError(f"监管官方源回补全部失败: {'; '.join(errors)}")
    if failed_sources:
        _alert(
            f"监管官方源 {','.join(failed_sources)} 回补失败（其余源正常）: "
            f"{'; '.join(errors)[:300]}")

    new_counts = {
        source: _archive_source(source, envelopes)
        for source, envelopes in envelopes_by_source.items()
    }
    all_envelopes = [
        envelope for source in selected
        for envelope in envelopes_by_source.get(source, [])
    ]
    ok = dup = 0
    if all_envelopes:
        ok, dup = osu.bulk_create(
            client, [_strip_raw(envelope) for envelope in all_envelopes])

    parts: List[str] = []
    for source in selected:
        if source in failed_sources:
            parts.append(f"{source} 失败")
            continue
        rows = envelopes_by_source.get(source, [])
        dates = sorted({str(row.get("pub_time") or "")[:10]
                        for row in rows if row.get("pub_time")})
        covered = f" {dates[0]}~{dates[-1]}" if dates else ""
        parts.append(
            f"{source} {len(rows)}条(新{new_counts.get(source, 0)}){covered}")
    summary = (
        f"监管官方源回补({start_date}~{end_date}): {' '.join(parts)}, "
        f"入库新增 {ok} 重复 {dup}")
    if failed_sources:
        summary += f", 失败源[{','.join(failed_sources)}]"
    logger.info(summary)
    return summary
