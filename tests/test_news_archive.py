"""news_archive 单元测试：全部走 tmp_path，绝不触碰真实 NAS（Z 盘）与钉钉。

覆盖（计划 T3 + 审查修订）：append→load_ids roundtrip；两次 append 多 member 被
replay 完整读回；重复 _id 由 load_ids 去重；空目录 replay 为空；NAS 写失败→落
spool+标记文件 TTL 节流告警（新鲜标记不发/过期或无标记发一次/恢复后清标记发通知）；
flush_spool 幂等搬运（含文件内去重、NAS 仍不可用保留、损坏文件隔离）；
NAS+spool 双失败抛异常。测试真实读写 gzip 文件，不 mock gzip。
"""

import gzip
import json
import os
import time
from types import SimpleNamespace

import pytest

from data_collect.utils import news_archive as na


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """隔离环境：archive_base/spool_dir 指向 tmp_path，钉钉打桩。"""
    nas = tmp_path / "nas"
    spool = tmp_path / "spool"
    monkeypatch.setattr(na, "get_news_config",
                        lambda: {"archive_base": str(nas),
                                 "archive_base_posix": str(nas),
                                 "spool_dir": str(spool)})
    alerts = []
    monkeypatch.setattr(na, "send_dingtalk", lambda msg: alerts.append(msg))
    return SimpleNamespace(nas=nas, spool=spool, alerts=alerts,
                           marker=spool / ".nas_alert_ts")


def test_posix_uses_archive_base_posix(monkeypatch):
    """平台感知：Linux 用 archive_base_posix（挂载点），忽略 Windows 的 archive_base。"""
    monkeypatch.setattr(na, "get_news_config",
                        lambda: {"archive_base": "Z:\\A股冷数据\\news",
                                 "archive_base_posix": "/mnt/media/A股冷数据/news"})
    monkeypatch.setattr(na, "_is_posix", lambda: True)   # 模拟 Linux（不污染全局 os）
    na._archive_root()   # 用 posix 值，不抛错即通过


def test_posix_missing_base_raises(monkeypatch):
    """Linux 但未配 archive_base_posix → 响亮 raise（逼配挂载点，不静默走垃圾目录）。"""
    monkeypatch.setattr(na, "get_news_config",
                        lambda: {"archive_base": "Z:\\A股冷数据\\news"})
    monkeypatch.setattr(na, "_is_posix", lambda: True)
    with pytest.raises(RuntimeError, match="archive_base_posix 未配置"):
        na._archive_root()


def test_posix_base_still_windows_path_raises(monkeypatch):
    """archive_base_posix 误填成 Windows 盘符 → 仍响亮拦截。"""
    monkeypatch.setattr(na, "get_news_config",
                        lambda: {"archive_base_posix": "Z:\\x"})
    monkeypatch.setattr(na, "_is_posix", lambda: True)
    with pytest.raises(RuntimeError, match="Windows 盘符路径"):
        na._archive_root()


def mk_env(i, day="20260703"):
    """构造最小信封（archive 层只要求含 _id，其余字段原样透传）。"""
    return {"_id": f"id-{i}", "title": f"标题{i}", "source": "cls",
            "pub_time": f"{day[:4]}-{day[4:6]}-{day[6:]} 09:0{i}:00"}


def _block(path):
    """把该路径占位成文件，令其无法作为目录使用（模拟 NAS/spool 不可用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("占位", encoding="utf-8")


def _write_spool(paths, source, date, envelopes):
    """直接在 spool 目录构造积压文件（模拟历史降级留下的内容），返回文件路径。"""
    p = (paths.spool / "raw" / source / date[:4] / date[4:6] / f"{date[6:8]}.jsonl.gz")
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "ab") as f:
        for env in envelopes:
            f.write((json.dumps(env, ensure_ascii=False) + "\n").encode("utf-8"))
    return p


# ---------- 路径 ----------

def test_archive_path_layout(paths):
    p = na.archive_path("cls", "20260703")
    assert p == paths.nas / "raw" / "cls" / "2026" / "07" / "03.jsonl.gz"


def test_archive_path_bad_date_raises(paths):
    with pytest.raises(ValueError):
        na.archive_path("cls", "2026-07-03")
    with pytest.raises(ValueError):
        na.archive_path("cls", "２０２６０７０３")   # 全角数字 isdigit=True，须拦截
    with pytest.raises(ValueError):
        na.archive_path("", "20260703")
    with pytest.raises(ValueError):
        na.archive_path("..", "20260703")          # 禁止逃出 raw/
    with pytest.raises(ValueError):
        na.archive_path("a/b", "20260703")


def test_spool_root_default_under_project(monkeypatch):
    """spool_dir 缺省时落 <项目根>/news_spool（.gitignore 已排除）。"""
    monkeypatch.setattr(na, "get_news_config", lambda: {"archive_base": "unused"})
    assert na._spool_root() == na._PROJECT_ROOT / "news_spool"


# ---------- roundtrip ----------

def test_append_then_load_ids_roundtrip(paths):
    envs = [mk_env(i) for i in range(3)]
    assert na.append("cls", "20260703", envs) == 3
    assert na.load_ids("cls", "20260703") == {"id-0", "id-1", "id-2"}


def test_load_ids_missing_file_empty(paths):
    assert na.load_ids("cls", "20260703") == set()


def test_append_empty_returns_zero_and_no_file(paths):
    assert na.append("cls", "20260703", []) == 0
    assert not na.archive_path("cls", "20260703").exists()


def test_append_missing_id_raises(paths):
    long_body = "很长的正文" * 200
    with pytest.raises(ValueError, match="_id") as excinfo:
        na.append("cls", "20260703", [{"title": "无 id", "content": long_body}])
    assert long_body not in str(excinfo.value)       # 报错不携带信封全文
    assert len(str(excinfo.value)) < 300             # 只给 keys/截断摘要


def test_append_writes_readable_utf8_jsonl(paths):
    na.append("cls", "20260703", [mk_env(1)])
    with gzip.open(na.archive_path("cls", "20260703"), "rt", encoding="utf-8") as f:
        raw = f.read()
    assert "标题1" in raw                          # ensure_ascii=False：中文原样可读
    assert json.loads(raw.strip())["_id"] == "id-1"


# ---------- 多 member 追加 + replay ----------

def test_two_appends_pure_append_and_replay_all(paths):
    batch1 = [mk_env(0), mk_env(1)]
    batch2 = [mk_env(2)]
    na.append("cls", "20260703", batch1)
    first_bytes = na.archive_path("cls", "20260703").read_bytes()
    na.append("cls", "20260703", batch2)
    data = na.archive_path("cls", "20260703").read_bytes()
    assert data[:len(first_bytes)] == first_bytes   # 追加不改写：旧字节原样保留
    assert len(data) > len(first_bytes)
    got = list(na.replay("cls", ("20260703", "20260703")))
    assert got == batch1 + batch2                   # 跨 gzip member 完整读回


def test_append_does_not_dedup_but_load_ids_does(paths):
    na.append("cls", "20260703", [mk_env(1)])
    na.append("cls", "20260703", [mk_env(1)])       # append 不去重（去重责任在调用方）
    assert len(list(na.replay("cls", ("20260703", "20260703")))) == 2
    assert na.load_ids("cls", "20260703") == {"id-1"}


# ---------- replay ----------

def test_replay_empty_dir(paths):
    assert list(na.replay("cls", ("20260101", "20260105"))) == []


def test_replay_range_inclusive_skips_missing_days(paths):
    na.append("cls", "20260701", [mk_env(1, "20260701")])
    na.append("cls", "20260703", [mk_env(3, "20260703")])
    got = list(na.replay("cls", ("20260701", "20260703")))   # 0702 无文件，静默跳过
    assert [e["_id"] for e in got] == ["id-1", "id-3"]       # 含两端、按日期序


# ---------- spool 降级 ----------

def test_nas_fail_falls_to_spool_alert_once(paths):
    _block(paths.nas)
    assert na.append("cls", "20260703", [mk_env(0), mk_env(1)]) == 2   # 返回值照常
    assert na.append("cls", "20260703", [mk_env(2)]) == 1
    spool_file = paths.spool / "raw" / "cls" / "2026" / "07" / "03.jsonl.gz"
    assert spool_file.exists()                       # 同相对路径、同格式落 spool
    with gzip.open(spool_file, "rt", encoding="utf-8") as f:
        ids = [json.loads(line)["_id"] for line in f if line.strip()]
    assert ids == ["id-0", "id-1", "id-2"]
    assert len(paths.alerts) == 1                    # TTL 内只告警一次，不刷屏
    assert paths.marker.exists()                     # 节流标记已落盘（跨进程有效）


def test_alert_fresh_marker_suppresses_dingtalk(paths):
    """标记 mtime 新鲜：即使新进程（无内存状态）也不重发——只写 spool 不发钉钉。"""
    paths.spool.mkdir(parents=True)
    paths.marker.write_text("2026-07-03T09:00:00", encoding="utf-8")   # 刚告警过
    _block(paths.nas)
    assert na.append("cls", "20260703", [mk_env(0)]) == 1
    assert paths.alerts == []                        # 不发钉钉，仅日志


def test_alert_expired_marker_resends_and_refreshes(paths):
    """标记 mtime 超过 TTL：重发一次钉钉并刷新标记 mtime。"""
    paths.spool.mkdir(parents=True)
    paths.marker.write_text("stale", encoding="utf-8")
    old = time.time() - na._ALERT_TTL_SECONDS - 60   # 过期 1 分钟
    os.utime(paths.marker, (old, old))
    _block(paths.nas)
    assert na.append("cls", "20260703", [mk_env(0)]) == 1
    assert len(paths.alerts) == 1                    # 过期后重发
    assert paths.marker.stat().st_mtime > old + 60   # 标记 mtime 已刷新


def test_alert_failure_does_not_block_spool(paths, monkeypatch):
    _block(paths.nas)

    def boom(msg):
        raise RuntimeError("钉钉不可用")

    monkeypatch.setattr(na, "send_dingtalk", boom)
    assert na.append("cls", "20260703", [mk_env(0)]) == 1    # 告警失败不影响写 spool
    assert (paths.spool / "raw" / "cls" / "2026" / "07" / "03.jsonl.gz").exists()


def test_double_failure_raises(paths):
    _block(paths.nas)
    _block(paths.spool)
    with pytest.raises(RuntimeError):
        na.append("cls", "20260703", [mk_env(0)])


# ---------- flush_spool ----------

def test_flush_spool_moves_and_idempotent(paths):
    _block(paths.nas)
    na.append("cls", "20260703", [mk_env(0), mk_env(1)])
    paths.nas.unlink()                               # NAS 恢复
    assert na.flush_spool() == 2
    assert na.load_ids("cls", "20260703") == {"id-0", "id-1"}
    assert not (paths.spool / "raw" / "cls" / "2026" / "07" / "03.jsonl.gz").exists()
    assert not (paths.spool / "raw" / "cls").exists()   # 空目录清理
    assert na.flush_spool() == 0                     # 幂等：再跑无事发生


def test_flush_spool_recovery_clears_marker_and_notifies(paths):
    """全部搬回（spool 清空）→ 删降级标记 + 发一条恢复通知；再跑不重复通知。"""
    _block(paths.nas)
    na.append("cls", "20260703", [mk_env(0), mk_env(1)])
    assert paths.marker.exists() and len(paths.alerts) == 1   # 降级中
    paths.nas.unlink()                               # NAS 恢复
    assert na.flush_spool() == 2
    assert not paths.marker.exists()                 # 标记清除
    assert len(paths.alerts) == 2                    # 降级告警 + 恢复通知
    assert "恢复" in paths.alerts[-1] and "2" in paths.alerts[-1]
    assert na.flush_spool() == 0
    assert len(paths.alerts) == 2                    # 无标记不重发恢复通知


def test_flush_spool_recovery_notify_failure_harmless(paths, monkeypatch):
    """恢复通知发送失败：不抛，标记仍已清除（不会下轮重发）。"""
    _block(paths.nas)
    na.append("cls", "20260703", [mk_env(0)])
    paths.nas.unlink()

    def boom(msg):
        raise RuntimeError("钉钉不可用")

    monkeypatch.setattr(na, "send_dingtalk", boom)
    assert na.flush_spool() == 1                     # 不受通知失败影响
    assert not paths.marker.exists()


def test_flush_spool_dedups_against_nas_and_within_file(paths):
    na.append("cls", "20260703", [mk_env(0)])        # NAS 已有 id-0
    # 积压含与 NAS 重复的 id-0、文件内重复的 id-1
    _write_spool(paths, "cls", "20260703", [mk_env(0), mk_env(1), mk_env(1)])
    assert na.flush_spool() == 1                     # 只搬缺失的 id-1
    replayed = [e["_id"] for e in na.replay("cls", ("20260703", "20260703"))]
    assert sorted(replayed) == ["id-0", "id-1"]      # NAS 归档无重复


def test_flush_spool_nas_still_down_keeps_files_and_marker(paths):
    _block(paths.nas)
    na.append("cls", "20260703", [mk_env(0)])        # 降级：spool + 标记 + 1 条告警
    assert na.flush_spool() == 0                     # NAS 仍不可用：不抛
    assert (paths.spool / "raw" / "cls" / "2026" / "07" / "03.jsonl.gz").exists()
    assert paths.marker.exists()                     # 降级状态保持（无恢复通知）
    assert len(paths.alerts) == 1


def test_flush_spool_corrupt_file_kept_others_moved(paths):
    """损坏 spool 文件（非 gzip）：保留待人工介入，同轮其余好文件照常搬回。"""
    bad = paths.spool / "raw" / "cls" / "2026" / "07" / "02.jsonl.gz"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a gzip file")
    good = _write_spool(paths, "cctv", "20260703", [mk_env(1)])
    assert na.flush_spool() == 1                     # 只计好文件
    assert bad.exists()                              # 损坏文件保留
    assert not good.exists()                         # 好文件已搬回并删除
    assert na.load_ids("cctv", "20260703") == {"id-1"}


def test_flush_spool_no_spool_dir(paths):
    assert na.flush_spool() == 0


def test_flush_spool_multiple_sources_and_days(paths):
    _write_spool(paths, "cls", "20260702", [mk_env(0, "20260702")])
    _write_spool(paths, "cctv", "20260703", [mk_env(1)])
    assert na.flush_spool() == 2
    assert na.load_ids("cls", "20260702") == {"id-0"}
    assert na.load_ids("cctv", "20260703") == {"id-1"}
