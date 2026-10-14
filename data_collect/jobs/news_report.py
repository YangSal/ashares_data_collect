"""每天 18:00 发送一次新闻采集钉钉摘要和完整邮件。

统计窗口固定为北京时间 `[前一日18:00, 当日18:00)`。采集量来自 OpenSearch
`fetch_time`，运行细节来自本地 SQLite 事件账本。钉钉和邮件分别记录投递状态，
pipeline 重试时只补发失败通道。
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from data_collect.config import _load_config, get_dingtalk_config, get_news_config
from data_collect.utils import news_digest, news_health, source_registry
from data_collect.utils.notify import send_dingtalk_immediate, send_email


logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_RECIPIENT = "839833678@qq.com"


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _get_client():
    # 延迟导入：普通单元测试和仅查事件账本时不加载 opensearch-py/aiohttp。
    from data_collect.utils.opensearch_utils import get_client
    return get_client()


def _search_raw(client, body: dict):
    from data_collect.utils.opensearch_utils import search_raw
    return search_raw(client, body)


def _hits_total(response: dict) -> int:
    total = ((response or {}).get("hits") or {}).get("total", 0)
    return int(total.get("value", 0) if isinstance(total, dict) else total or 0)


def _buckets(response: dict, name: str) -> dict[str, int]:
    buckets = (((response or {}).get("aggregations") or {}).get(name) or {}).get("buckets") or []
    return {
        str(item["key"]): int(item.get("doc_count") or 0)
        for item in buckets if item.get("key") not in (None, "")
    }


def _os_time(value: datetime) -> str:
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _collection_metrics(start: datetime, end: datetime) -> dict[str, Any]:
    """查询窗口采集量（fetch_time）以及全库 pending 积压。"""
    client = _get_client()
    aggregate_body = {
        "query": {"range": {"fetch_time": {"gte": _os_time(start), "lt": _os_time(end)}}},
        "aggs": {
            "by_channel": {"terms": {"field": "channel", "size": 100}},
            "by_source": {"terms": {"field": "source", "size": 500}},
        },
        "size": 0,
        "track_total_hits": True,
    }
    aggregate = _search_raw(client, aggregate_body)
    if isinstance(aggregate, dict) and aggregate.get("error"):
        raise RuntimeError(f"OpenSearch 聚合返回 error: {aggregate['error']!r}")

    pending_body = {
        "query": {"term": {"vec_status": "pending"}},
        "size": 1,
        "sort": [{"fetch_time": {"order": "asc"}}],
        "_source": ["fetch_time"],
        "track_total_hits": True,
    }
    pending_response = _search_raw(client, pending_body)
    if isinstance(pending_response, dict) and pending_response.get("error"):
        raise RuntimeError(f"OpenSearch pending 查询返回 error: {pending_response['error']!r}")
    hits = ((pending_response or {}).get("hits") or {}).get("hits") or []
    oldest = (hits[0].get("_source") or {}).get("fetch_time") if hits else None

    return {
        "total": _hits_total(aggregate),
        "channels": _buckets(aggregate, "by_channel"),
        "sources": _buckets(aggregate, "by_source"),
        "pending": _hits_total(pending_response),
        "pending_oldest": oldest,
    }


def _source_health_metrics(
    start: datetime,
    end: datetime,
    now: datetime,
) -> dict[str, Any]:
    """加载当前/窗口观测并合成健康快照；失败显式返回且不阻断日报。"""
    try:
        sources = source_registry.load_all()
        latest = news_digest.list_latest_source_observations()
        window = news_digest.list_source_observations(start, end)
        return news_health.build_health_snapshot(
            sources, latest, window, now)
    except Exception as exc:  # noqa: BLE001 -- 日报其他区域及投递必须继续
        safe_error = news_health.redact_detail(f"{type(exc).__name__}: {exc}")
        logger.error("新闻日报来源健康查询失败: %s", safe_error)
        return {"query_error": safe_error}


def _count_segment(values: dict[str, int], empty: str = "无") -> str:
    if not values:
        return empty
    return "，".join(
        f"{key} {value}" for key, value in
        sorted(values.items(), key=lambda item: (-item[1], item[0]))
    )


def _event_is_error(event: dict[str, Any]) -> bool:
    return (
        event.get("success") == 0
        or event.get("severity") in {"warning", "error", "critical"}
        or event.get("event_type") in {"alert", "retry", "delivery_error"}
    )


def _error_fingerprint(event: dict[str, Any]) -> tuple[str, str, str, str]:
    summary = str(event.get("summary") or event.get("details") or "未知错误").splitlines()[0]
    summary = re.sub(r"\s+", " ", summary).strip()
    return (
        str(event.get("pipeline") or "-"),
        str(event.get("task") or "-"),
        str(event.get("source") or "-"),
        summary,
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n…（钉钉篇幅限制，完整详情见邮件）"
    return text[:max(0, limit - len(suffix))] + suffix


def _health_state_text(row: dict[str, Any]) -> str:
    """把一条来源状态压缩为钉钉可读的异常维度。"""
    parts = []
    freshness = str(row.get("freshness") or "unknown")
    if freshness not in {"fresh", "not_applicable"}:
        age = row.get("age_hours")
        age_text = "" if age is None else f"({float(age):.0f}h)"
        parts.append(f"freshness {freshness}{age_text}")
    smoke = str(row.get("smoke") or "unknown")
    if smoke != "ok":
        parts.append(f"smoke {smoke}")
    transport = str(row.get("transport") or "not_applicable")
    if transport not in {"primary", "not_applicable"}:
        parts.append(f"transport {transport}")
    return ", ".join(parts) or str(row.get("overall") or "unknown")


def _append_health_summary(lines: list[str], health: dict[str, Any]) -> None:
    if health.get("query_error"):
        lines.append(f"来源健康查询失败：{health['query_error']}")
        return
    summary = health.get("summary") or {}
    enabled = sum(int(summary.get(key) or 0) for key in (
        "healthy", "failover", "warning", "failed", "unknown"))
    lines.append(
        f"来源健康 {enabled}启用：{int(summary.get('healthy') or 0)}健康，"
        f"{int(summary.get('failover') or 0)}降级，"
        f"{int(summary.get('warning') or 0)}异常，"
        f"{int(summary.get('failed') or 0)}失败，"
        f"{int(summary.get('unknown') or 0)}未知")
    abnormal = [
        row for row in (health.get("sources") or [])
        if row.get("enabled") and row.get("overall") != "healthy"]
    if abnormal:
        lines.append("非健康来源：")
        for row in abnormal:
            lines.append(
                f"- {row.get('source')} [{row.get('overall')}] "
                f"{_health_state_text(row)}")
    transport = health.get("transport") or {}
    lines.append(
        f"RSSHub：主实例 {int(transport.get('primary') or 0)}，"
        f"备用成功 {int(transport.get('failover') or 0)}，"
        f"失败 {int(transport.get('failed') or 0)}")
    recovered = health.get("recovered_candidates") or []
    if recovered:
        lines.append(f"恢复候选：{','.join(recovered)}")


def _health_email_detail(health: dict[str, Any]) -> list[str]:
    if health.get("query_error"):
        return ["", f"来源健康全量明细：查询失败 {health['query_error']}"]
    lines = ["", "来源健康全量明细："]
    for row in health.get("sources") or []:
        lines.append(
            f"- {row.get('source')} | enabled={bool(row.get('enabled'))} "
            f"| profile={row.get('profile')} | overall={row.get('overall')} "
            f"| latest={row.get('last_data_time') or '-'} "
            f"| freshness={row.get('freshness')} | smoke={row.get('smoke')} "
            f"| transport={row.get('transport')} "
            f"| detail={row.get('detail') or '-'}")
    if len(lines) == 2:
        lines.append("- 无来源状态")
    return lines


def _build_report(
    start: datetime,
    end: datetime,
    metrics: dict[str, Any],
    events: list[dict[str, Any]],
    health: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """构建 `(钉钉摘要, 邮件完整正文)`。"""
    tasks = [event for event in events if event.get("event_type") == "task_result"]
    success_count = sum(event.get("success") == 1 and not event.get("skipped") for event in tasks)
    failed_count = sum(event.get("success") == 0 for event in tasks)
    skipped_count = sum(bool(event.get("skipped")) for event in tasks)
    pipeline_stats: dict[str, Counter] = defaultdict(Counter)
    for event in tasks:
        stats = pipeline_stats[str(event.get("pipeline") or "-")]
        stats["total"] += 1
        stats["duration_ms"] += int(float(event.get("duration") or 0) * 1000)
        if event.get("skipped"):
            stats["skipped"] += 1
        elif event.get("success") == 1:
            stats["success"] += 1
        else:
            stats["failed"] += 1

    errors = [event for event in events if _event_is_error(event)]
    grouped_errors = Counter(_error_fingerprint(event) for event in errors)
    lines = [
        f"新闻采集日报 [{start:%Y-%m-%d %H:%M} - {end:%Y-%m-%d %H:%M}) 北京时间",
        f"采集 {int(metrics.get('total') or 0)}：{_count_segment(metrics.get('channels') or {})}",
        f"来源：{_count_segment(metrics.get('sources') or {})}",
        f"任务 {len(tasks)}次：{success_count}成功，{failed_count}失败，{skipped_count}跳过",
    ]
    pending_oldest = metrics.get("pending_oldest") or "-"
    lines.append(f"向量 pending {int(metrics.get('pending') or 0)}，最老 {pending_oldest}")
    if metrics.get("query_error"):
        lines.append(f"OpenSearch查询失败：{metrics['query_error']}")
    if health is not None:
        _append_health_summary(lines, health)

    if pipeline_stats:
        lines.append("Pipeline明细：")
        for pipeline_name in sorted(pipeline_stats):
            stats = pipeline_stats[pipeline_name]
            lines.append(
                f"- {pipeline_name}: {stats['total']}次，{stats['success']}成功，"
                f"{stats['failed']}失败，{stats['skipped']}跳过，"
                f"{stats['duration_ms'] / 1000:.1f}s"
            )

    lines.append(f"错误/告警：{len(errors)}次，{len(grouped_errors)}类")
    if grouped_errors:
        for (pipeline_name, task, source, summary), count in grouped_errors.most_common():
            repeat = f" ×{count}" if count > 1 else ""
            lines.append(f"- [{pipeline_name}/{task}/{source}] {summary[:500]}{repeat}")
    else:
        lines.append("- 无")

    full_summary = "\n".join(lines)
    digest_cfg = get_news_config().get("digest") or {}
    dingtalk = _truncate(full_summary, int(digest_cfg.get("dingtalk_max_chars", 18000)))

    timeline = ["", "完整事件时间线（北京时间，按发生顺序）："]
    if not events:
        timeline.append("无事件记录")
    for event in events:
        identity = "/".join(filter(None, [
            str(event.get("pipeline") or ""),
            str(event.get("task") or ""),
            str(event.get("source") or ""),
        ])) or "-"
        state = (
            "跳过" if event.get("skipped") else
            "成功" if event.get("success") == 1 else
            "失败" if event.get("success") == 0 else "-"
        )
        duration = "" if event.get("duration") is None else f" {float(event['duration']):.1f}s"
        timeline.append(
            f"\n[{event.get('event_time')}] {event.get('event_type')}/{event.get('severity')} "
            f"{identity} {state}{duration}\n"
            f"摘要: {event.get('summary') or '-'}\n"
            f"详情:\n{event.get('details') or '-'}"
        )
    email_health = _health_email_detail(health) if health is not None else []
    return dingtalk, full_summary + "\n" + "\n".join(email_health + timeline)


def _smtp_config() -> dict:
    return dict(((_load_config().get("wave3") or {}).get("email") or {}))


def _record_delivery_error(channel: str, exc: Exception) -> None:
    try:
        news_digest.record_event(
            "delivery_error", "error", task="news_digest", source=channel,
            summary=f"{channel}投递失败: {exc}", details=repr(exc),
        )
    except Exception:
        logger.error("日报投递错误写账失败", exc_info=True)


def run(run_date: str | None = None, **kwargs) -> str:
    """生成最近完整窗口日报，并分别确保钉钉和邮件各成功一次。"""
    if not get_dingtalk_config().get("enabled", True):
        return "新闻采集18:00日报：静默节点(dingtalk.enabled=false)，钉钉和邮件均不发送"

    report_now = _now()
    start, end = news_digest.report_window(report_now)
    delivery = news_digest.get_delivery(end)
    if delivery.get("dingtalk_status") == "sent" and delivery.get("email_status") == "sent":
        return f"新闻采集日报 {end:%Y-%m-%d} 已完成双通道投递"

    events = news_digest.list_events(start, end)
    try:
        metrics = _collection_metrics(start, end)
    except Exception as exc:
        logger.error("新闻日报 OpenSearch 查询失败", exc_info=True)
        metrics = {
            "total": 0, "channels": {}, "sources": {},
            "pending": 0, "pending_oldest": None,
            "query_error": f"{type(exc).__name__}: {exc}",
        }
        synthetic = {
            "id": None, "event_time": report_now.isoformat(timespec="seconds"),
            "event_type": "alert", "severity": "error", "pipeline": "news_digest",
            "task": "news_digest", "source": "opensearch", "success": None,
            "skipped": None, "duration": None, "summary": "OpenSearch查询失败",
            "details": f"{type(exc).__name__}: {exc}", "collector_node": "-",
        }
        events = [*events, synthetic]
        try:
            news_digest.record_event(
                "alert", "error", pipeline="news_digest", task="news_digest",
                source="opensearch", summary="OpenSearch查询失败",
                details=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            logger.error("OpenSearch 查询错误写账失败", exc_info=True)

    health = _source_health_metrics(start, end, report_now)
    dingtalk_body, email_body = _build_report(
        start, end, metrics, events, health=health)
    failures: list[str] = []

    if delivery.get("dingtalk_status") != "sent":
        try:
            if not send_dingtalk_immediate(dingtalk_body):
                raise RuntimeError("当前节点钉钉发送被禁用")
            news_digest.mark_delivery(end, "dingtalk", "sent")
        except Exception as exc:
            news_digest.mark_delivery(end, "dingtalk", "failed", str(exc))
            _record_delivery_error("dingtalk", exc)
            failures.append(f"dingtalk: {exc}")

    if delivery.get("email_status") != "sent":
        digest_cfg = get_news_config().get("digest") or {}
        recipient = str(digest_cfg.get("email_recipient") or DEFAULT_RECIPIENT).strip()
        try:
            send_email(
                f"新闻采集日报 {end:%Y-%m-%d}", email_body,
                _smtp_config(), [recipient],
            )
            news_digest.mark_delivery(end, "email", "sent")
        except Exception as exc:
            news_digest.mark_delivery(end, "email", "failed", str(exc))
            _record_delivery_error("email", exc)
            failures.append(f"email: {exc}")

    retention_days = int((get_news_config().get("digest") or {}).get("retention_days", 30))
    cutoff = end - timedelta(days=retention_days)
    news_digest.cleanup_events(cutoff)
    news_digest.cleanup_source_observations(cutoff)
    if failures:
        raise RuntimeError("新闻日报投递失败；将仅重试失败通道: " + "; ".join(failures))
    return dingtalk_body
