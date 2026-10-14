"""来源健康画像、状态转换和综合快照。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from data_collect.utils import news_digest as nd
from data_collect.utils import news_health as nh
from data_collect.utils.source_registry import Source


SH = ZoneInfo("Asia/Shanghai")
TUE = datetime(2026, 9, 1, 10, 0, tzinfo=SH)


def _source(source_id="x", *, adapter="rss", channel="media",
            job="news_regulator", enabled=True, health_profile="",
            stale_hours=None):
    return Source(
        id=source_id, adapter=adapter, channel=channel, job=job,
        enabled=enabled, health_profile=health_profile,
        stale_hours=stale_hours)


def _obs(source, check_type, status, observed_at, **kwargs):
    return {
        "id": kwargs.pop("id", 1),
        "source": source,
        "check_type": check_type,
        "status": status,
        "enabled": int(kwargs.pop("enabled", True)),
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "last_data_time": kwargs.pop("last_data_time", None),
        "numeric_value": kwargs.pop("numeric_value", None),
        "detail": kwargs.pop("detail", ""),
        "collector_node": kwargs.pop("collector_node", "news-a"),
        **kwargs,
    }


def test_profile_defaults_follow_real_job_cadence():
    assert nh.profile_for(
        _source(channel="us_news", job="news_us")) == "business"
    assert nh.profile_for(
        _source(adapter="rsshub", channel="media",
                job="news_regulator")) == "continuous"
    assert nh.profile_for(
        _source(source_id="stats", channel="policy",
                job="news_policy")) == "event"
    assert nh.profile_for(
        _source(channel="cctv", job="news_cctv")) == "daily"
    assert nh.profile_for(
        _source(channel="report", job="news_regulator")) == "business"


def test_explicit_profile_and_stale_hours_override_defaults():
    source = _source(
        health_profile="daily", stale_hours=9, channel="policy",
        job="news_policy")
    assert nh.profile_for(source) == "daily"
    assert nh.effective_stale_hours(source, TUE) == 9.0


def test_business_profile_gets_weekend_and_monday_morning_grace():
    source = _source(channel="report")
    saturday = datetime(2026, 9, 5, 10, 0, tzinfo=SH)
    monday_morning = datetime(2026, 9, 7, 11, 59, tzinfo=SH)
    monday_noon = datetime(2026, 9, 7, 12, 0, tzinfo=SH)
    assert nh.effective_stale_hours(source, saturday) == 72.0
    assert nh.effective_stale_hours(source, monday_morning) == 72.0
    assert nh.effective_stale_hours(source, monday_noon) == 36.0


def test_event_profile_does_not_infer_stale():
    source = _source(
        source_id="stats", channel="policy", job="news_policy")
    assert nh.evaluate_freshness(source, None, TUE) == (
        "not_applicable", None)


def test_freshness_boundary_missing_and_malformed_time():
    source = _source(health_profile="continuous")
    exactly_16h = (TUE - timedelta(hours=16)).strftime("%Y-%m-%d %H:%M:%S")
    over_16h = (TUE - timedelta(hours=16, seconds=1)).strftime(
        "%Y-%m-%d %H:%M:%S")
    assert nh.evaluate_freshness(source, exactly_16h, TUE) == (
        "fresh", 16.0)
    status, age = nh.evaluate_freshness(source, over_16h, TUE)
    assert status == "stale" and age > 16
    assert nh.evaluate_freshness(source, None, TUE) == ("never", None)
    assert nh.evaluate_freshness(source, "not-a-time", TUE) == (
        "never", None)


def test_daily_profile_has_no_weekend_grace():
    saturday = datetime(2026, 9, 5, 10, 0, tzinfo=SH)
    source = _source(health_profile="daily")
    assert nh.effective_stale_hours(source, saturday) == 36.0


def test_observe_records_only_real_status_transitions(tmp_path):
    db = tmp_path / "digest.sqlite3"
    t1 = datetime(2026, 9, 1, 8, 0, tzinfo=SH)
    assert nh.observe(
        "caixin", "freshness", "fresh", enabled=True,
        observed_at=t1, db_path=db) is not None
    nh.observe(
        "caixin", "freshness", "fresh", enabled=True,
        observed_at=t1 + timedelta(hours=1), db_path=db)
    nh.observe(
        "caixin", "freshness", "stale", enabled=True,
        detail="http://user:pass@host:7890/path?q=token",
        observed_at=t1 + timedelta(hours=2), db_path=db)

    events = nd.list_events(t1, t1 + timedelta(hours=3), db_path=db)

    assert len(events) == 1
    assert events[0]["event_type"] == "source_health_transition"
    assert events[0]["summary"] == "freshness: fresh -> stale"
    assert events[0]["details"] == "http://host:7890/path"


def test_observe_global_zero_transition_is_critical_and_recovery_is_info(tmp_path):
    db = tmp_path / "digest.sqlite3"
    t1 = datetime(2026, 9, 1, 8, 0, tzinfo=SH)
    nh.observe("__all__", "freshness", "fresh", enabled=True,
               observed_at=t1, db_path=db)
    nh.observe("__all__", "freshness", "stale", enabled=True,
               observed_at=t1 + timedelta(hours=1), db_path=db)
    nh.observe("__all__", "freshness", "fresh", enabled=True,
               observed_at=t1 + timedelta(hours=2), db_path=db)

    events = nd.list_events(t1, t1 + timedelta(hours=3), db_path=db)

    assert [event["severity"] for event in events] == ["critical", "info"]


def test_observe_returns_none_when_ledger_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        nd, "latest_source_observation",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")))
    assert nh.observe("caixin", "smoke", "dead", enabled=True) is None


def test_redact_detail_removes_credentials_queries_and_named_secrets():
    detail = (
        "proxy http://alice:secret@host:7890/path?q=token "
        "next=https://example.com/x?a=1 token=abc123 password=hunter2")
    redacted = nh.redact_detail(detail)
    assert "http://host:7890/path" in redacted
    assert "https://example.com/x" in redacted
    assert "alice" not in redacted
    assert "secret" not in redacted
    assert "abc123" not in redacted
    assert "hunter2" not in redacted
    assert "?" not in redacted


def test_build_health_snapshot_applies_expiry_precedence_and_recovery():
    sources = [
        _source("healthy", health_profile="continuous"),
        _source("fallback", health_profile="continuous"),
        _source("warning", health_profile="continuous"),
        _source("failed", health_profile="continuous"),
        _source("unknown", health_profile="continuous"),
        _source("event", channel="policy", job="news_policy"),
        _source("disabled_ok", enabled=False, health_profile="continuous"),
    ]
    latest = []
    next_id = 1
    for source_id, freshness, smoke in [
        ("healthy", "fresh", "ok"),
        ("fallback", "fresh", "ok"),
        ("warning", "stale", "ok"),
        ("failed", "fresh", "dead"),
        ("unknown", "fresh", "ok"),
        ("event", "not_applicable", "ok"),
    ]:
        freshness_time = TUE - (
            timedelta(hours=4) if source_id == "unknown" else timedelta(hours=1))
        latest.append(_obs(
            source_id, "freshness", freshness, freshness_time,
            id=next_id, last_data_time="2026-09-01 09:00:00", numeric_value=1))
        next_id += 1
        latest.append(_obs(
            source_id, "smoke", smoke, TUE - timedelta(days=1), id=next_id))
        next_id += 1
    latest.append(_obs(
        "disabled_ok", "smoke", "ok", TUE - timedelta(days=1),
        id=next_id, enabled=False))
    window = [
        _obs("healthy", "transport", "primary", TUE - timedelta(hours=3), id=40),
        _obs("fallback", "transport", "failover", TUE - timedelta(hours=2), id=41),
        _obs("failed", "transport", "failed", TUE - timedelta(hours=1), id=42,
             detail="http://user:pass@rss:1200/path?token=x"),
    ]

    snapshot = nh.build_health_snapshot(sources, latest, window, TUE)

    assert snapshot["summary"] == {
        "healthy": 2, "failover": 1, "warning": 1,
        "failed": 1, "unknown": 1}
    assert snapshot["transport"] == {
        "primary": 1, "failover": 1, "failed": 1}
    assert snapshot["recovered_candidates"] == ["disabled_ok"]
    by_id = {row["source"]: row for row in snapshot["sources"]}
    assert by_id["healthy"]["overall"] == "healthy"
    assert by_id["fallback"]["overall"] == "failover"
    assert by_id["warning"]["overall"] == "warning"
    assert by_id["failed"]["overall"] == "failed"
    assert by_id["unknown"]["overall"] == "unknown"
    assert by_id["unknown"]["freshness"] == "unknown"
    assert by_id["event"]["freshness"] == "not_applicable"
    assert by_id["disabled_ok"]["overall"] == "disabled"
    assert "user" not in by_id["failed"]["detail"]


def test_build_health_snapshot_expires_old_smoke_observation():
    source = _source("caixin", health_profile="continuous")
    latest = [
        _obs("caixin", "freshness", "fresh", TUE - timedelta(hours=1)),
        _obs("caixin", "smoke", "ok", TUE - timedelta(days=9), id=2),
    ]

    snapshot = nh.build_health_snapshot([source], latest, [], TUE)

    row = snapshot["sources"][0]
    assert row["smoke"] == "not_run"
    assert row["overall"] == "unknown"
