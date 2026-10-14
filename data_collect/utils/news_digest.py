"""新闻采集 18:00 汇总通知的本地事件账本。

新闻入口开启汇总模式后，任务结果和原本准备即时发出的告警都写入 SQLite。
数据库只保存运维事件，不替代新闻原始归档或 OpenSearch。
"""

from __future__ import annotations

import os
import socket
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from data_collect.config import get_news_config


NEWS_DIGEST_MODE_ENV = "DATA_COLLECT_NEWS_DIGEST_MODE"
SHANGHAI = ZoneInfo("Asia/Shanghai")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def enable_digest_mode() -> None:
    """开启新闻通知汇总模式；环境变量会被任务子进程继承。"""
    os.environ[NEWS_DIGEST_MODE_ENV] = "1"


def is_digest_mode() -> bool:
    return os.environ.get(NEWS_DIGEST_MODE_ENV) == "1"


def _aware_shanghai(value: datetime | None = None) -> datetime:
    value = value or datetime.now(SHANGHAI)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def report_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """返回最近一个完整的 `[前日18:00, 当日18:00)` 北京时间窗口。"""
    current = _aware_shanghai(now)
    today_boundary = datetime.combine(current.date(), time(18, 0), tzinfo=SHANGHAI)
    end = today_boundary if current >= today_boundary else today_boundary - timedelta(days=1)
    return end - timedelta(days=1), end


def _event_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    digest_cfg = (get_news_config().get("digest") or {})
    configured = Path(digest_cfg.get("event_db", "output/news_digest.sqlite3"))
    return configured if configured.is_absolute() else _PROJECT_ROOT / configured


@contextmanager
def _connect(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    path = _event_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time TEXT NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            pipeline TEXT,
            task TEXT,
            source TEXT,
            success INTEGER,
            skipped INTEGER,
            duration REAL,
            summary TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            collector_node TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time);
        CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, event_time);

        CREATE TABLE IF NOT EXISTS source_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            check_type TEXT NOT NULL,
            status TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            last_data_time TEXT,
            numeric_value REAL,
            detail TEXT NOT NULL DEFAULT '',
            collector_node TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_source_obs_source_type_time
            ON source_observations(source, check_type, observed_at);
        CREATE INDEX IF NOT EXISTS idx_source_obs_time
            ON source_observations(observed_at);

        CREATE TABLE IF NOT EXISTS deliveries (
            window_end TEXT PRIMARY KEY,
            dingtalk_status TEXT NOT NULL DEFAULT 'pending',
            dingtalk_error TEXT,
            dingtalk_updated_at TEXT,
            email_status TEXT NOT NULL DEFAULT 'pending',
            email_error TEXT,
            email_updated_at TEXT
        );
        """
    )
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_event(
    event_type: str,
    severity: str = "info",
    *,
    pipeline: str | None = None,
    task: str | None = None,
    source: str | None = None,
    success: bool | None = None,
    skipped: bool | None = None,
    duration: float | None = None,
    summary: str = "",
    details: str = "",
    collector_node: str | None = None,
    event_time: datetime | None = None,
    db_path: str | Path | None = None,
) -> int:
    """追加一条事件并返回自增 id。"""
    when = _aware_shanghai(event_time).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO events (
                event_time, event_type, severity, pipeline, task, source,
                success, skipped, duration, summary, details, collector_node
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                when, event_type, severity, pipeline, task, source,
                None if success is None else int(success),
                None if skipped is None else int(skipped),
                duration, str(summary or ""), str(details or ""),
                collector_node or socket.gethostname(),
            ),
        )
        return int(cursor.lastrowid)


def record_task_result(
    pipeline: str,
    result: Any,
    *,
    event_time: datetime | None = None,
    db_path: str | Path | None = None,
) -> int:
    """把 pipeline.TaskResult 完整写入事件账本，避免依赖 pipeline 反向导入。"""
    message = str(getattr(result, "message", "") or "")
    first_line = message.splitlines()[0] if message else ""
    success = bool(getattr(result, "success", False))
    skipped = bool(getattr(result, "skipped", False))
    severity = "info" if success else ("warning" if skipped else "error")
    return record_event(
        "task_result",
        severity,
        pipeline=pipeline,
        task=str(getattr(result, "name", "")),
        success=success,
        skipped=skipped,
        duration=float(getattr(result, "duration", 0.0) or 0.0),
        summary=first_line,
        details=message,
        event_time=event_time,
        db_path=db_path,
    )


def list_events(
    start: datetime,
    end: datetime,
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """按时间正序读取 `[start, end)` 内事件。"""
    start_text = _aware_shanghai(start).isoformat(timespec="seconds")
    end_text = _aware_shanghai(end).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE event_time >= ? AND event_time < ? "
            "ORDER BY event_time, id",
            (start_text, end_text),
        ).fetchall()
    return [dict(row) for row in rows]


def record_source_observation(
    source: str,
    check_type: str,
    status: str,
    *,
    enabled: bool,
    last_data_time: str | None = None,
    numeric_value: float | None = None,
    detail: str = "",
    collector_node: str | None = None,
    observed_at: datetime | None = None,
    db_path: str | Path | None = None,
) -> int:
    """追加来源健康观测并返回自增 id。"""
    when = _aware_shanghai(observed_at).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO source_observations (
                observed_at, source, check_type, status, enabled,
                last_data_time, numeric_value, detail, collector_node
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                when, str(source), str(check_type), str(status), int(enabled),
                last_data_time, numeric_value, str(detail or ""),
                collector_node or socket.gethostname(),
            ),
        )
        return int(cursor.lastrowid)


def latest_source_observation(
    source: str,
    check_type: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """读取来源某一检查维度最后写入的一条观测。"""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM source_observations "
            "WHERE source = ? AND check_type = ? ORDER BY id DESC LIMIT 1",
            (source, check_type),
        ).fetchone()
    return None if row is None else dict(row)


def list_latest_source_observations(
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """读取每个 ``(source, check_type)`` 最后写入的一条观测。"""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT o.* FROM source_observations o
            JOIN (
              SELECT source, check_type, MAX(id) AS max_id
              FROM source_observations GROUP BY source, check_type
            ) latest ON latest.max_id = o.id
            ORDER BY o.source, o.check_type
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_source_observations(
    start: datetime,
    end: datetime,
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """按时间正序读取 ``[start, end)`` 内来源观测。"""
    start_text = _aware_shanghai(start).isoformat(timespec="seconds")
    end_text = _aware_shanghai(end).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM source_observations "
            "WHERE observed_at >= ? AND observed_at < ? "
            "ORDER BY observed_at, id",
            (start_text, end_text),
        ).fetchall()
    return [dict(row) for row in rows]


def _window_key(window_end: datetime) -> str:
    return _aware_shanghai(window_end).isoformat(timespec="seconds")


def get_delivery(
    window_end: datetime,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """读取窗口投递状态；首次读取会建立 pending 行。"""
    key = _window_key(window_end)
    with _connect(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO deliveries(window_end) VALUES (?)", (key,))
        row = conn.execute(
            "SELECT * FROM deliveries WHERE window_end = ?", (key,)
        ).fetchone()
    return dict(row)


def mark_delivery(
    window_end: datetime,
    channel: str,
    status: str,
    error: str | None = None,
    *,
    db_path: str | Path | None = None,
) -> None:
    """更新 dingtalk/email 其中一个通道，不触碰另一通道。"""
    if channel not in {"dingtalk", "email"}:
        raise ValueError(f"未知投递通道: {channel}")
    if status not in {"pending", "sent", "failed"}:
        raise ValueError(f"未知投递状态: {status}")
    key = _window_key(window_end)
    now_text = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO deliveries(window_end) VALUES (?)", (key,))
        conn.execute(
            f"UPDATE deliveries SET {channel}_status = ?, {channel}_error = ?, "
            f"{channel}_updated_at = ? WHERE window_end = ?",
            (status, error, now_text, key),
        )


def cleanup_events(
    before: datetime,
    *,
    db_path: str | Path | None = None,
) -> int:
    """删除严格早于 cutoff 的事件，并清理对应旧投递记录。"""
    cutoff = _aware_shanghai(before).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM events WHERE event_time < ?", (cutoff,))
        conn.execute("DELETE FROM deliveries WHERE window_end < ?", (cutoff,))
        return int(cursor.rowcount)


def cleanup_source_observations(
    before: datetime,
    *,
    db_path: str | Path | None = None,
) -> int:
    """删除严格早于 cutoff 的来源观测，返回独立删除行数。"""
    cutoff = _aware_shanghai(before).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM source_observations WHERE observed_at < ?", (cutoff,))
        return int(cursor.rowcount)
