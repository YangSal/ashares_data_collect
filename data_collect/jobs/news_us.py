"""美国金融政策/公司文件采集任务（新闻子系统 us_*：P1，规划见
docs/research/2026-07-08-us-market-news-plan.md）。

两源单 job（**需代理环境**——美国官方源境内直连不通；全部拉取经
fetch_with_timeout 硬超时，代理挂起=cls 事故同款，必须兜住）：
- ``fed``：美联储官方 press_all RSS（新闻稿/FOMC 声明/讲话），channel=us_policy；
- ``sec_8k``：SEC EDGAR getcurrent Atom（8-K 重大事件报告，count=400 滚动窗），
  channel=us_filing。SEC 要求声明式 UA（含联系方式），勿改浏览器 UA。

调度 news_us 每日 05:30 北京时间（美股收盘、美东下午披露高峰已过）。P1 诚实
边界：sec_8k 为滚动窗**重点采样**非全量（全量走 daily-index，列 P2）；Fed 每日
仅数条发布，20 条窗绰绰有余。pub_time 统一 UTC→北京时间（信封契约不变）；
信封化/归档复用 news_policy 的实现（同为 feed 源，单一定义）。
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Dict, List

from data_collect.config import get_news_config
from data_collect.utils import news_archive
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

# feed 信封化与分日归档与 news_policy 完全同构——复用其实现（单一定义；
# 若将来行为分叉再各自落地，当前 YAGNI）
from data_collect.jobs.news_policy import _archive_source
from data_collect.jobs.news_policy import _entry_to_envelope

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 60     # 单源拉取硬超时地板（HTTP 超时由注册表 timeout 决定）


def _rss_sources() -> Dict[str, source_registry.Source]:
    """注册表 → {source: Source}（加减源改 sources.yaml；headers/proxy/timeout
    已由加载器解析——SEC 需声明式 UA 勿改，代理精确到源 us → news.us_proxy）。

    候选源（intl_news，enabled: false）自动排除，冒烟通过改 enabled 即上线零代码。
    函数化 = 热生效：子进程每轮重读；测试 monkeypatch 本函数即与注册表/config 解耦。
    """
    return {s.id: s for s in source_registry.load_sources("news_us")
            if s.adapter == "rss"}


def _alert(message: str) -> bool:
    """钉钉告警（guarded：通知失败不影响采集主流程）。"""
    return notify.guarded_send(message)


# feed 抓取单一实现在适配器层（注册表三期）；别名保留原私有名供测试 monkeypatch
_fetch_feed_entries = sa.fetch_feed


def _collect_source(source: str, fetch_time: str,
                    feeds: Dict[str, source_registry.Source]) -> List[dict]:
    """拉取单源并信封化（批内 `_id` 去重保首见）；异常由调用方按源隔离。

    feeds 由 run() 一次加载传入（勿在此调 shim——每调一次=一次磁盘读+last-good
    重拷，评审 S1）。美国源不做 A 股简称打标（词典无意义），传空典——tag_stocks
    仅剩代码正则，英文文本天然不命中，stocks=[]。
    """
    src = feeds[source]
    entries = fetch_with_timeout(
        lambda: _fetch_feed_entries(src.url, headers=src.headers,
                                    proxy=src.proxy_url, timeout=src.timeout,
                                    label=f"feed {source}"),
        sa.hard_timeout(src.timeout, _FETCH_TIMEOUT_SECONDS),
        f"美国源 {source} 拉取")
    envelopes: List[dict] = []
    seen: set = set()
    for entry in (entries or []):
        env = _entry_to_envelope(source, src.channel, entry, fetch_time, {})
        if env["_id"] in seen:
            continue
        seen.add(env["_id"])
        envelopes.append(env)
    logger.info(f"美国源 {source} 本轮 {len(envelopes)} 条")
    return envelopes


# 代理 URL 脱敏：http://user:pass@host:port → http://***@host:port。告警/异常串会进
# 钉钉（多人群可见），若 us_proxy 配了带凭据的代理，ProxyError 消息会泄露账密（审查 S5）
_PROXY_CRED_RE = re.compile(r"(://)[^@/\s]+@")


def _redact(text: str) -> str:
    """抹除文本中代理 URL 的 user:pass@ 段（进钉钉/日志前脱敏）。"""
    return _PROXY_CRED_RE.sub(r"\1***@", str(text))


# ======== Pipeline 标准接口 ========

def run(run_date: str | None = None, **kwargs) -> str:
    """每日任务：Fed+SEC 采集 → 归档 → create-only 入库（源级隔离+guarded 告警）。"""
    _flush_spool_safe()
    fetch_time = _now().strftime("%Y-%m-%d %H:%M:%S")

    feeds = _rss_sources()                    # 每轮加载一次（子进程模型=天然热生效）
    sources = tuple(feeds)
    # 代理让位（多节点 Phase A4）：声明了命名代理但本节点无该代理的源 → 跳过，
    # 让给有代理能力的节点（不算失败不告警；重叠由 create-only 幂等兜底）
    skipped_sources = [s for s in sources
                       if source_registry.proxy_missing(feeds[s])]
    active_sources = tuple(s for s in sources if s not in skipped_sources)
    if skipped_sources:
        logger.info(f"美国源让位跳过（本节点无代理）: {','.join(skipped_sources)}")
    envelopes_by_source: Dict[str, List[dict]] = {}
    failed_sources: List[str] = []
    errors: List[str] = []
    for source in active_sources:
        try:
            envelopes_by_source[source] = _collect_source(source, fetch_time, feeds)
        except Exception as exc:  # noqa: BLE001
            failed_sources.append(source)
            errors.append(_redact(f"{source}: {exc!r}"))   # 脱敏代理凭据（S5）
            logger.warning(f"美国源 {source} 采集失败（源级隔离，继续其余源）: "
                           f"{_redact(repr(exc))}")
    if active_sources and len(failed_sources) == len(active_sources):
        raise RuntimeError(f"美国源全部采集失败（检查代理？）: {'; '.join(errors)}")
    if failed_sources:
        _alert(f"美国源 {','.join(failed_sources)} 本轮采集失败（其余源正常，"
               f"多半是代理问题）: {'; '.join(errors)[:300]}")

    # 逐源归档隔离（S2）：单源归档失败（NAS 故障/权限）不阻塞其余源入库——入库与归档
    # 解耦，create-only 幂等，归档缺口可由 spool/下轮补；原 dict 推导任一源抛错则
    # new_counts 未绑定、后续 bulk_create 整体不执行，采集阶段的源级隔离白做。
    new_counts: Dict[str, int] = {}
    for source, envelopes in envelopes_by_source.items():
        try:
            new_counts[source] = _archive_source(source, envelopes)
        except Exception as exc:  # noqa: BLE001
            new_counts[source] = 0
            logger.warning(f"美国源 {source} 归档失败（不阻塞入库，spool 待下轮搬回）: "
                           f"{_redact(repr(exc))}")

    all_envelopes = [env for source in sources
                     for env in envelopes_by_source.get(source, [])]
    ok = dup = 0
    if all_envelopes:
        ok, dup = osu.bulk_create(osu.get_client(),
                                  [_strip_raw(env) for env in all_envelopes])

    parts = []
    for source in sources:
        if source in skipped_sources:
            parts.append(f"{source} 让位")
        elif source in failed_sources:
            parts.append(f"{source} 失败")
        else:
            parts.append(f"{source} {len(envelopes_by_source[source])}条"
                         f"(新{new_counts[source]})")
    summary = f"美国: {' '.join(parts)}, 入库新增 {ok} 重复 {dup}"
    if skipped_sources:
        summary += f", 让位跳过[{','.join(skipped_sources)}]（本节点无代理）"
    if failed_sources:
        summary += f", 失败源[{','.join(failed_sources)}]"
    logger.info(summary)
    return summary


def run_verify(start_date: str, end_date: str, **kwargs) -> str:
    """查漏补缺：归档重放 → create-only 补库（自然日语义，同 news_policy）。"""
    days_back = int(get_news_config().get("verify_days_back", 3))
    dates = _verify_dates(days_back, _today(), include_today=True)
    window = (dates[0], dates[-1])

    replayed: List[dict] = []
    for source in tuple(_rss_sources()):
        try:
            replayed.extend(news_archive.replay(source, window))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"美国 verify 重放 {source} 失败（跳过该源）: {exc!r}")

    ok = dup = 0
    if replayed:
        ok, dup = osu.bulk_create(osu.get_client(),
                                  [_strip_raw(env) for env in replayed])
    summary = (f"美国 verify({window[0]}~{window[1]}): 重放 {len(replayed)} 条, "
               f"补录 {ok} 重复 {dup}")
    logger.info(summary)
    return summary
