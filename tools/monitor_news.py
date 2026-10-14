"""sink 侧新闻采集小时监控：只读 OpenSearch，写入 18:00 健康账本。

设计定位（spec 2026-07-28 §3）：**独立于采集进程**的观察者——采集节点静默
"看数据不看进程"：不需要知道有哪些节点在采，只看数据到没到（去中心化配套）。

三段内容（读别名 news；fetch_time=入库口径，与日报 news_report 的 pub_time
运营口径互补）：
1. 近 N 小时（缺省 1）各 channel 入库数；
2. **启用源断流检测**：逐注册表启用源查最新文档的 fetch_time age，按来源
   health_profile 判定并写入 SQLite 观测账本。快讯
   cls/em/sina 不在注册表（flash_sources 自治、已有独立断流监测），不覆盖；
3. vec pending 积压 + 最老 age（同日报口径）。

用法（conda py10；须在能连 OpenSearch 的机器跑——9.10）:
    python tools/monitor_news.py                 # 只打印并写18:00账本，不外发
    python tools/monitor_news.py --quiet         # 兼容旧 timer；同样不外发
    python tools/monitor_news.py --no-send       # 兼容旧 timer；同样不外发

挂 9.10 小时任务（systemd timer，款式同 README news-health 示例）：
    # news-monitor.service  ExecStart=/path/py10/bin/python tools/monitor_news.py
    # news-monitor.timer    OnCalendar=hourly  Persistent=true
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_collect.utils import news_digest, news_health

_TIME_FMT = "%Y-%m-%d %H:%M:%S"

def parse_channel_counts(resp) -> tuple:
    """聚合响应 → (总数, {channel: count})。载荷带 error → LookupError（同日报）。"""
    from data_collect.utils import opensearch_utils as osu
    if isinstance(resp, dict) and resp.get("error"):
        raise LookupError(f"channel 聚合返回 error: {resp.get('error')!r}")
    buckets = (((resp or {}).get("aggregations") or {})
               .get("by_channel") or {}).get("buckets") or []
    counts = {b.get("key"): int(b.get("doc_count") or 0)
              for b in buckets if b.get("key")}
    return osu.hits_total(resp), counts


def build_report(now: datetime.datetime, hours: int, total: int, counts: dict,
                 stale: list, never: list, pending: int, oldest_minutes,
                 vec_deferred: bool = False) -> str:
    """拼装简报串（发送串 == 返回串，测试锁定）。stale=[(源id, age小时)]。"""
    chan_seg = " ".join(f"{ch} {counts[ch]}"
                        for ch in sorted(counts, key=lambda c: (-counts[c], c)))
    stale_seg = ("断流: " + ",".join(f"{sid}({age:.0f}h)" for sid, age in stale)
                 if stale else "断流: 无")
    if never:
        stale_seg += f" | 无文档源: {','.join(never)}"
    if vec_deferred:
        pending_seg = f"pending {pending}(向量已禁用,留待补算)"
    else:
        minutes = "-" if oldest_minutes is None else str(oldest_minutes)
        pending_seg = f"pending {pending}(最老 {minutes} 分)"
    return (f"新闻小时报 {now:%m-%d %H:%M}: 近{hours}h 入库 {total}"
            f" | {chan_seg or '无入库'} | {stale_seg} | {pending_seg}")


def is_abnormal(stale: list, total: int) -> bool:
    """--quiet 的发送判定：有断流源 或 近窗全库零入库。"""
    return bool(stale) or total == 0


# ======== OpenSearch 查询（在 9.10 跑；本机 Windows 有证书 bug 会连不上） ========

def channel_counts(client, since: str) -> tuple:
    from data_collect.utils import opensearch_utils as osu
    body = {"query": {"range": {"fetch_time": {"gte": since}}},
            "aggs": {"by_channel": {"terms": {"field": "channel", "size": 100}}},
            "size": 0, "track_total_hits": True}
    return parse_channel_counts(osu.search_raw(client, body))


def latest_fetch_time(client, source_id: str):
    from data_collect.utils import opensearch_utils as osu
    body = {"query": {"term": {"source": source_id}}, "size": 1,
            "sort": [{"fetch_time": {"order": "desc"}}], "_source": ["fetch_time"]}
    hits = (((osu.search_raw(client, body) or {}).get("hits") or {})
            .get("hits") or [])
    return (hits[0].get("_source") or {}).get("fetch_time") if hits else None


def pending_backlog(client) -> tuple[int, str | None]:
    """查询全库向量 pending 数和最老 fetch_time。"""
    from data_collect.utils import opensearch_utils as osu
    body = {
        "query": {"term": {"vec_status": "pending"}},
        "size": 1,
        "sort": [{"fetch_time": {"order": "asc"}}],
        "_source": ["fetch_time"],
        "track_total_hits": True,
    }
    response = osu.search_raw(client, body)
    if isinstance(response, dict) and response.get("error"):
        raise LookupError(f"pending 查询返回 error: {response.get('error')!r}")
    hits = ((response or {}).get("hits") or {}).get("hits") or []
    oldest = (hits[0].get("_source") or {}).get("fetch_time") if hits else None
    return osu.hits_total(response), oldest


def check_sources(client, sources, now: datetime.datetime, *, db_path=None) -> list[dict]:
    """逐启用源判定 freshness，隔离单源查询错误并写观测账本。"""
    rows = []
    for s in sources:
        detail = ""
        try:
            latest = latest_fetch_time(client, s.id)
            status, age = news_health.evaluate_freshness(s, latest, now)
        except Exception as exc:  # noqa: BLE001 -- 单来源查询失败不拖累其余来源
            latest, status, age = None, "unknown", None
            detail = f"{type(exc).__name__}: {exc}"
        observation_id = news_health.observe(
            s.id, "freshness", status, enabled=s.enabled,
            last_data_time=latest, numeric_value=age, detail=detail,
            observed_at=now, db_path=db_path)
        rows.append({
            "source": s.id,
            "status": status,
            "age_hours": age,
            "last_data_time": latest,
            "observation_id": observation_id,
        })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="新闻采集小时监控（只打印并写18:00账本，不外发）")
    ap.add_argument("--hours", type=int, default=1, help="入库计数窗口小时数（缺省 1）")
    ap.add_argument("--quiet", action="store_true", help="兼容旧参数；不外发")
    ap.add_argument("--no-send", action="store_true", help="兼容旧参数；不外发")
    args = ap.parse_args(argv)

    from data_collect.config import get_news_config
    from data_collect.utils import opensearch_utils as osu
    from data_collect.utils import source_registry as sr
    from data_collect.utils.news_common import age_minutes as _age_minutes
    from data_collect.utils.news_common import now as _now

    now = _now()
    try:
        client = osu.get_client()
        since = (now - datetime.timedelta(hours=args.hours)).strftime(_TIME_FMT)
        total, counts = channel_counts(client, since)
        enabled = [s for s in sr.load_all() if s.enabled]
        source_rows = check_sources(client, enabled, now)
        global_observation_id = news_health.observe(
            "__all__", "freshness", "fresh" if total > 0 else "stale",
            enabled=True, numeric_value=total, observed_at=now)
        pending, oldest_fetch = pending_backlog(client)
    except Exception as exc:  # noqa: BLE001 —— 查询失败入账，由 18:00 汇总
        report = f"新闻小时报: 查询异常({type(exc).__name__})，请查集群/日志"
        print(report)
        try:
            news_digest.record_event(
                "alert", "error", source="opensearch", summary=report,
                details=news_health.redact_detail(f"{type(exc).__name__}: {exc}"),
                event_time=now)
        except Exception as ledger_exc:  # noqa: BLE001
            print(f"[18:00账本写入失败: {type(ledger_exc).__name__}]")
        return 1

    vec_deferred = not (get_news_config().get("embedding") or {}).get("enabled", True)
    stale = [(row["source"], row["age_hours"])
             for row in source_rows if row["status"] == "stale"]
    never = [row["source"] for row in source_rows
             if row["status"] in {"never", "unknown"}]
    report = build_report(now, args.hours, total, counts, stale, never,
                          pending, _age_minutes(oldest_fetch, now),
                          vec_deferred=vec_deferred)
    print(report)
    if args.quiet or args.no_send:
        print("[兼容参数已保留：只打印并写18:00账本，不外发]")
    ledger_failed = global_observation_id is None or any(
        row["observation_id"] is None for row in source_rows)
    return 1 if ledger_failed else 0


if __name__ == "__main__":
    sys.exit(main())
