"""新闻 18:00 汇总事件账本单元测试。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from data_collect.utils import news_digest as nd


SH = ZoneInfo("Asia/Shanghai")


def test_report_window_is_previous_18_to_current_18():
    start, end = nd.report_window(datetime(2026, 8, 27, 18, 0, tzinfo=SH))
    assert start == datetime(2026, 8, 26, 18, 0, tzinfo=SH)
    assert end == datetime(2026, 8, 27, 18, 0, tzinfo=SH)


def test_report_window_before_18_uses_previous_boundary():
    start, end = nd.report_window(datetime(2026, 8, 27, 17, 59, tzinfo=SH))
    assert start == datetime(2026, 8, 25, 18, 0, tzinfo=SH)
    assert end == datetime(2026, 8, 26, 18, 0, tzinfo=SH)


def test_event_query_uses_half_open_window(tmp_path):
    db = tmp_path / "digest.sqlite3"
    start = datetime(2026, 8, 26, 18, 0, tzinfo=SH)
    end = start + timedelta(days=1)
    nd.record_event("alert", "warning", summary="before", event_time=start - timedelta(seconds=1), db_path=db)
    nd.record_event("alert", "warning", summary="at-start", event_time=start, db_path=db)
    nd.record_event("alert", "warning", summary="before-end", event_time=end - timedelta(seconds=1), db_path=db)
    nd.record_event("alert", "warning", summary="at-end", event_time=end, db_path=db)

    events = nd.list_events(start, end, db_path=db)

    assert [event["summary"] for event in events] == ["at-start", "before-end"]
    assert db.exists()


def test_record_task_result_preserves_full_details(tmp_path):
    db = tmp_path / "digest.sqlite3"
    result = SimpleNamespace(
        name="news_regulator",
        success=False,
        skipped=False,
        duration=12.5,
        message="RuntimeError: RSSHub 503\nfull traceback line",
    )

    nd.record_task_result("news_hourly", result, db_path=db,
                          event_time=datetime(2026, 8, 27, 1, 0, tzinfo=SH))
    events = nd.list_events(
        datetime(2026, 8, 27, 0, 0, tzinfo=SH),
        datetime(2026, 8, 28, 0, 0, tzinfo=SH),
        db_path=db,
    )

    assert len(events) == 1
    assert events[0]["event_type"] == "task_result"
    assert events[0]["pipeline"] == "news_hourly"
    assert events[0]["task"] == "news_regulator"
    assert events[0]["success"] == 0
    assert events[0]["skipped"] == 0
    assert events[0]["duration"] == 12.5
    assert "full traceback line" in events[0]["details"]


def test_delivery_state_is_independent_per_channel(tmp_path):
    db = tmp_path / "digest.sqlite3"
    window_end = datetime(2026, 8, 27, 18, 0, tzinfo=SH)

    nd.mark_delivery(window_end, "dingtalk", "sent", db_path=db)
    nd.mark_delivery(window_end, "email", "failed", "smtp timeout", db_path=db)
    delivery = nd.get_delivery(window_end, db_path=db)

    assert delivery["dingtalk_status"] == "sent"
    assert delivery["email_status"] == "failed"
    assert delivery["email_error"] == "smtp timeout"


def test_cleanup_events_keeps_recent_rows(tmp_path):
    db = tmp_path / "digest.sqlite3"
    cutoff = datetime(2026, 8, 1, 0, 0, tzinfo=SH)
    nd.record_event("alert", summary="old", event_time=cutoff - timedelta(seconds=1), db_path=db)
    nd.record_event("alert", summary="keep", event_time=cutoff, db_path=db)

    deleted = nd.cleanup_events(cutoff, db_path=db)
    events = nd.list_events(cutoff, cutoff + timedelta(days=1), db_path=db)

    assert deleted == 1
    assert [event["summary"] for event in events] == ["keep"]


def test_source_observation_round_trip_and_latest(tmp_path):
    db = tmp_path / "digest.sqlite3"
    t1 = datetime(2026, 8, 27, 1, 0, tzinfo=SH)
    t2 = t1 + timedelta(hours=1)
    nd.record_source_observation(
        "caixin", "freshness", "fresh", enabled=True,
        last_data_time="2026-08-27 00:50:00", numeric_value=10 / 60,
        detail="primary", collector_node="news-a", observed_at=t1, db_path=db)
    nd.record_source_observation(
        "caixin", "freshness", "stale", enabled=True,
        last_data_time="2026-08-26 00:00:00", numeric_value=26,
        detail="late", collector_node="news-b", observed_at=t2, db_path=db)

    latest = nd.latest_source_observation(
        "caixin", "freshness", db_path=db)

    assert latest["status"] == "stale"
    assert latest["numeric_value"] == 26
    assert latest["enabled"] == 1
    assert latest["collector_node"] == "news-b"


def test_source_observation_queries_are_half_open_and_group_latest(tmp_path):
    db = tmp_path / "digest.sqlite3"
    start = datetime(2026, 8, 27, 1, 0, tzinfo=SH)
    end = start + timedelta(hours=2)
    rows = [
        ("caixin", "freshness", "old", start - timedelta(seconds=1)),
        ("caixin", "freshness", "fresh", start),
        ("caixin", "smoke", "ok", start + timedelta(minutes=30)),
        ("caixin", "freshness", "stale", start + timedelta(hours=1)),
        ("latepost", "smoke", "dead", end),
    ]
    for source, check_type, status, observed_at in rows:
        nd.record_source_observation(
            source, check_type, status, enabled=True,
            observed_at=observed_at, db_path=db)

    window = nd.list_source_observations(start, end, db_path=db)
    latest = nd.list_latest_source_observations(db_path=db)

    assert [(r["check_type"], r["status"]) for r in window] == [
        ("freshness", "fresh"), ("smoke", "ok"),
        ("freshness", "stale")]
    assert {(r["source"], r["check_type"], r["status"]) for r in latest} == {
        ("caixin", "freshness", "stale"),
        ("caixin", "smoke", "ok"),
        ("latepost", "smoke", "dead"),
    }
    assert nd.latest_source_observation("ghost", "smoke", db_path=db) is None


def test_source_observation_cleanup_is_independent_from_events(tmp_path):
    db = tmp_path / "digest.sqlite3"
    cutoff = datetime(2026, 8, 27, 0, 0, tzinfo=SH)
    nd.record_event(
        "alert", summary="old event",
        event_time=cutoff - timedelta(seconds=1), db_path=db)
    for status, observed_at in [
        ("old", cutoff - timedelta(seconds=1)), ("keep", cutoff)]:
        nd.record_source_observation(
            "caixin", "smoke", status, enabled=True,
            observed_at=observed_at, db_path=db)

    deleted = nd.cleanup_source_observations(cutoff, db_path=db)

    assert deleted == 1
    assert nd.latest_source_observation(
        "caixin", "smoke", db_path=db)["status"] == "keep"
    assert nd.list_events(
        cutoff - timedelta(days=1), cutoff, db_path=db)[0]["summary"] == "old event"


def test_source_observation_accepts_short_concurrent_writes(tmp_path):
    db = tmp_path / "digest.sqlite3"
    nd.record_source_observation(
        "seed", "smoke", "ok", enabled=True, db_path=db)

    def write_one(index):
        return nd.record_source_observation(
            f"source_{index}", "smoke", "ok", enabled=True,
            numeric_value=index, db_path=db)

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(write_one, range(12)))

    assert len(set(ids)) == 12
    assert len(nd.list_latest_source_observations(db_path=db)) == 13


def test_digest_mode_environment(monkeypatch):
    monkeypatch.delenv(nd.NEWS_DIGEST_MODE_ENV, raising=False)
    assert nd.is_digest_mode() is False
    nd.enable_digest_mode()
    assert nd.is_digest_mode() is True
    monkeypatch.delenv(nd.NEWS_DIGEST_MODE_ENV, raising=False)
