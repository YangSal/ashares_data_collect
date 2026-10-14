"""新闻来源健康画像、观测转换和日报综合状态。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from data_collect.utils import news_digest


logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

PROFILE_DEFAULT_HOURS = {
    "continuous": 16.0,
    "business": 36.0,
    "daily": 36.0,
    "event": None,
}
FRESHNESS_MAX_OBSERVATION_AGE_HOURS = 3.0
SMOKE_MAX_OBSERVATION_AGE_HOURS = 8 * 24.0
_DETAIL_LIMIT = 2000
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"\b(token|password|passwd|api[_-]?key|secret)\s*=\s*[^\s;,&]+",
    re.IGNORECASE,
)


def _aware_shanghai(value: datetime | None = None) -> datetime:
    current = value or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        return current.replace(tzinfo=SHANGHAI)
    return current.astimezone(SHANGHAI)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware_shanghai(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _aware_shanghai(parsed)


def profile_for(source) -> str:
    """按显式画像、job 周期、channel 的顺序推导健康画像。"""
    explicit = str(getattr(source, "health_profile", "") or "").strip()
    if explicit:
        return explicit
    job = str(getattr(source, "job", "") or "")
    channel = str(getattr(source, "channel", "") or "")
    if job == "news_cctv" or channel == "cctv":
        return "daily"
    if job in {"news_announcement", "news_stock", "news_us"}:
        return "business"
    if channel == "report":
        return "business"
    if channel in {"media", "flash"}:
        return "continuous"
    if channel in {"policy", "us_policy"}:
        return "event"
    return "event"


def effective_stale_hours(source, now: datetime | None = None) -> float | None:
    """返回当前日历位置下的断流阈值；事件型来源不做断流推断。"""
    profile = profile_for(source)
    default = PROFILE_DEFAULT_HOURS[profile]
    if default is None:
        return None
    configured = getattr(source, "stale_hours", None)
    hours = float(configured) if configured is not None else float(default)
    current = _aware_shanghai(now)
    weekend_grace = (
        current.weekday() in {5, 6}
        or (current.weekday() == 0 and current.hour < 12)
    )
    if profile == "business" and weekend_grace:
        return max(hours, 72.0)
    return hours


def evaluate_freshness(
    source,
    latest_fetch_time: str | datetime | None,
    now: datetime | None = None,
) -> tuple[str, float | None]:
    """把 OpenSearch 最近 fetch_time 转成来源新鲜度状态和小时 age。"""
    threshold = effective_stale_hours(source, now)
    if threshold is None:
        return "not_applicable", None
    latest = _parse_time(latest_fetch_time)
    if latest is None:
        return "never", None
    age_hours = max(
        0.0, (_aware_shanghai(now) - latest).total_seconds() / 3600.0)
    return ("fresh" if age_hours <= threshold else "stale"), age_hours


def redact_detail(text: Any) -> str:
    """移除 URL 凭据/查询串及常见命名密钥，并限制账本详情长度。"""
    value = str(text or "")

    def _redact_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ").,;]":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = f":{parsed.port}" if parsed.port is not None else ""
            safe = f"{parsed.scheme}://{host}{port}{parsed.path or ''}"
        except (TypeError, ValueError):
            safe = "<redacted-url>"
        return safe + trailing

    value = _URL_RE.sub(_redact_url, value)
    value = _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", value)
    return value[:_DETAIL_LIMIT]


def observe(
    source_id: str,
    check_type: str,
    status: str,
    *,
    enabled: bool,
    last_data_time: str | None = None,
    numeric_value: float | None = None,
    detail: str = "",
    observed_at: datetime | None = None,
    db_path: str | Path | None = None,
    severity: str = "warning",
) -> int | None:
    """写一条观测；仅真实状态变化追加日报事件，账本失败不阻断采集。"""
    when = _aware_shanghai(observed_at)
    safe_detail = redact_detail(detail)
    try:
        previous = news_digest.latest_source_observation(
            source_id, check_type, db_path=db_path)
        observation_id = news_digest.record_source_observation(
            source_id, check_type, status, enabled=enabled,
            last_data_time=last_data_time, numeric_value=numeric_value,
            detail=safe_detail, observed_at=when, db_path=db_path)
        old_status = previous["status"] if previous else None
        if old_status is not None and old_status != status:
            transition_severity = severity
            if source_id == "__all__" and status == "stale":
                transition_severity = "critical"
            elif source_id == "__all__" and status == "fresh":
                transition_severity = "info"
            news_digest.record_event(
                "source_health_transition", transition_severity,
                source=source_id,
                summary=f"{check_type}: {old_status} -> {status}",
                details=safe_detail, event_time=when, db_path=db_path)
        return observation_id
    except Exception:  # noqa: BLE001 -- 健康账本不能反向阻断采集
        logger.exception("来源健康观测写入失败: source=%s check_type=%s",
                         source_id, check_type)
        return None


def _expired(row: dict[str, Any] | None, max_hours: float,
             now: datetime) -> bool:
    if not row:
        return True
    observed = _parse_time(row.get("observed_at"))
    if observed is None:
        return True
    age = (_aware_shanghai(now) - observed).total_seconds() / 3600.0
    return age > max_hours


def _latest_by_dimension(
    observations: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in observations:
        key = (str(row.get("source") or ""), str(row.get("check_type") or ""))
        previous = latest.get(key)
        if previous is None or int(row.get("id") or 0) >= int(previous.get("id") or 0):
            latest[key] = row
    return latest


def build_health_snapshot(
    sources,
    latest_observations: Iterable[dict[str, Any]],
    window_observations: Iterable[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """生成 18:00 报告使用的当前来源状态及窗口 transport 统计。"""
    current = _aware_shanghai(now)
    latest = _latest_by_dimension(latest_observations)
    window_rows = list(window_observations)
    window_transport = [
        row for row in window_rows if row.get("check_type") == "transport"]
    transport_summary = {"primary": 0, "failover": 0, "failed": 0}
    for row in window_transport:
        status = str(row.get("status") or "")
        if status in transport_summary:
            transport_summary[status] += 1
    current_transport = _latest_by_dimension(window_transport)

    summary = {
        "healthy": 0, "failover": 0, "warning": 0,
        "failed": 0, "unknown": 0,
    }
    rows: list[dict[str, Any]] = []
    recovered_candidates: list[str] = []

    for source in sorted(sources, key=lambda item: item.id):
        profile = profile_for(source)
        freshness_row = latest.get((source.id, "freshness"))
        smoke_row = latest.get((source.id, "smoke"))
        transport_row = current_transport.get((source.id, "transport"))

        if profile == "event":
            freshness = "not_applicable"
        elif _expired(
                freshness_row, FRESHNESS_MAX_OBSERVATION_AGE_HOURS, current):
            freshness = "unknown"
        else:
            freshness = str(freshness_row.get("status") or "unknown")

        if _expired(smoke_row, SMOKE_MAX_OBSERVATION_AGE_HOURS, current):
            smoke = "not_run"
        else:
            smoke = str(smoke_row.get("status") or "unknown")
        transport = (
            str(transport_row.get("status") or "not_applicable")
            if transport_row else "not_applicable"
        )

        if not source.enabled:
            overall = "disabled"
            if smoke == "ok":
                recovered_candidates.append(source.id)
        elif smoke == "dead" or transport == "failed":
            overall = "failed"
        elif freshness == "unknown" or smoke in {"unknown", "not_run"}:
            overall = "unknown"
        elif freshness in {"stale", "never"} or smoke == "sick":
            overall = "warning"
        elif transport == "failover":
            overall = "failover"
        else:
            overall = "healthy"

        if source.enabled:
            summary[overall] += 1

        detail_parts = []
        for observation in (freshness_row, smoke_row, transport_row):
            if observation and observation.get("detail"):
                safe = redact_detail(observation["detail"])
                if safe not in detail_parts:
                    detail_parts.append(safe)
        rows.append({
            "source": source.id,
            "enabled": bool(source.enabled),
            "profile": profile,
            "overall": overall,
            "freshness": freshness,
            "smoke": smoke,
            "transport": transport,
            "last_data_time": (
                freshness_row.get("last_data_time") if freshness_row else None),
            "age_hours": (
                freshness_row.get("numeric_value") if freshness_row else None),
            "detail": " | ".join(detail_parts),
        })

    return {
        "summary": summary,
        "sources": rows,
        "recovered_candidates": sorted(recovered_candidates),
        "transport": transport_summary,
    }
