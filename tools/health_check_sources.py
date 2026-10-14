"""全源健康探测 CLI —— 复用生产适配器冒烟并写入 18:00 日报账本。

设计要点：**不自己 re-GET**（那会重演本机 HTTPS 证书 bug + 代理 env 污染），而是调
``source_adapters.smoke_test(s)``——headers/proxy/timeout 全取自 Source，与真实采集同路径。

⚠ **建议在新闻生产机 9.10 上跑**：那里 SSL 正常、有 mihomo 代理（us/intl 源才探得到）。
Windows 本机跑：HTTPS 政策源会因本机证书 bug 假失败、us/intl 源无代理探不到（会误报 DEAD）。
不覆盖：快讯 cls/em/sina（自治于 config `news.flash_sources`，不在注册表，有独立断流监测）。

判定：冒烟抛错=DEAD；返回 0 条目=SICK；返回 >0 条目=OK。**探停用源亦有价值**——能发现
已停源是否恢复（如 stats 07-16 停、07-27 探测发现恢复）。

用法（conda py10）:
    python tools/health_check_sources.py                    # 探全部（含停用）
    python tools/health_check_sources.py --job news_regulator
    python tools/health_check_sources.py --channel media --skip-disabled
    python tools/health_check_sources.py --alert            # 兼容旧 timer；不外发

退出码：有"启用中且 DEAD"的源 → 1（供 cron 判失败告警），否则 0。
全量一轮约数分钟（逐源串行、真拉网络；勿高频跑）。挂周任务示例见 README「数据源注册表」节。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_collect.utils import news_health
from data_collect.utils import source_registry as sr

# 冒烟结果串里的条目数：紧邻"条/行/链接"的整数（避开日期/股票码等其它数字）
_COUNT_RE = re.compile(r"(\d+)\s*(?:条|行|链接)")


def parse_count(detail: str):
    """从冒烟结果串提取条目数；无法解析返回 None。"""
    m = _COUNT_RE.search(detail or "")
    return int(m.group(1)) if m else None


def classify(ok: bool, detail: str):
    """(冒烟是否成功, 结果串) → (判定, 条目数)。"""
    if not ok:
        return "DEAD", None
    n = parse_count(detail)
    if n == 0:
        return "SICK", 0
    return "OK", n


def probe_one(source) -> dict:
    """单源冒烟（真拉一次，走生产适配器；不归档不入库）。"""
    from data_collect.utils import source_adapters as sa
    t0 = time.monotonic()
    try:
        detail = sa.smoke_test(source)
        ok = True
    except Exception as exc:  # noqa: BLE001 —— 冒烟任何失败都要归为 DEAD 记录
        detail = f"{type(exc).__name__}: {exc}"
        ok = False
    detail = news_health.redact_detail(detail)
    verdict, n = classify(ok, detail)
    return {"id": source.id, "adapter": source.adapter, "channel": source.channel,
            "job": source.job, "enabled": source.enabled, "verdict": verdict,
            "count": n, "elapsed": round(time.monotonic() - t0, 1),
            "detail": detail.replace("\n", " ")[:88]}


def summarize(results: list) -> dict:
    dead = [r for r in results if r["verdict"] == "DEAD"]
    sick = [r for r in results if r["verdict"] == "SICK"]
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r["verdict"] == "OK"),
        "dead": dead,
        "sick": sick,
        "enabled_dead": [r for r in dead if r["enabled"]],
        "enabled_sick": [r for r in sick if r["enabled"]],
        # 已停用但探测健康 → 源已恢复，可考虑重启用
        "recovered": [r for r in results if not r["enabled"] and r["verdict"] == "OK"],
    }


def format_report(results: list, summ: dict) -> str:
    order = {"DEAD": 0, "SICK": 1, "OK": 2}
    rows = sorted(results, key=lambda r: (order[r["verdict"]], r["adapter"], r["id"]))
    lines = [f"{'判定':5} {'源id':22} {'adapter':9} {'启用':4} {'channel':10} {'耗时':7} 详情",
             "-" * 100]
    for r in rows:
        lines.append(f"{r['verdict']:5} {r['id']:22} {r['adapter']:9} "
                     f"{'on ' if r['enabled'] else 'OFF':4} {r['channel']:10} "
                     f"{str(r['elapsed']) + 's':7} {r['detail']}")
    lines += ["", (f"== 探测 {summ['total']} | OK {summ['ok']} | "
                   f"DEAD {len(summ['dead'])}(启用中 {len(summ['enabled_dead'])}) | "
                   f"SICK {len(summ['sick'])}(启用中 {len(summ['enabled_sick'])}) ==")]
    if summ["enabled_dead"]:
        lines.append("⚠ 启用中死源: " + ", ".join(r["id"] for r in summ["enabled_dead"]))
    if summ["enabled_sick"]:
        lines.append("· 启用中病源(0条目): " + ", ".join(r["id"] for r in summ["enabled_sick"]))
    if summ["recovered"]:
        lines.append("✓ 已停但恢复(可重启用): " + ", ".join(r["id"] for r in summ["recovered"]))
    return "\n".join(lines)


def build_alert(summ: dict):
    """有"启用中死/病源"或"恢复源"才返回告警串，否则 None（周任务静默）。"""
    if not (summ["enabled_dead"] or summ["enabled_sick"] or summ["recovered"]):
        return None
    parts = [f"源健康探测: {summ['ok']}健康/{len(summ['dead'])}死/{len(summ['sick'])}病"]
    if summ["enabled_dead"]:
        parts.append("启用中死源[" + ",".join(r["id"] for r in summ["enabled_dead"]) + "]")
    if summ["enabled_sick"]:
        parts.append("启用中病源[" + ",".join(r["id"] for r in summ["enabled_sick"]) + "]")
    if summ["recovered"]:
        parts.append("已停但恢复可重启[" + ",".join(r["id"] for r in summ["recovered"]) + "]")
    return "；".join(parts)


def record_results(results: list, *, observed_at=None, db_path=None) -> int:
    """把一轮主动冒烟结果写入账本，返回成功写入条数。"""
    written = 0
    for result in results:
        observation_id = news_health.observe(
            result["id"], "smoke", result["verdict"].lower(),
            enabled=bool(result["enabled"]),
            numeric_value=result.get("elapsed"),
            detail=result.get("detail") or "",
            observed_at=observed_at, db_path=db_path)
        if observation_id is not None:
            written += 1
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="数据源全源健康探测（只记录到18:00日报，不外发）")
    ap.add_argument("--job", help="只探某 job 的源")
    ap.add_argument("--channel", help="只探某 channel 的源")
    ap.add_argument("--skip-disabled", action="store_true", help="不探停用源")
    ap.add_argument("--alert", action="store_true", help="兼容旧参数；只入账不外发")
    args = ap.parse_args(argv)

    sources = sr.load_all()
    if args.job:
        sources = [s for s in sources if s.job == args.job]
    if args.channel:
        sources = [s for s in sources if s.channel == args.channel]
    if args.skip_disabled:
        sources = [s for s in sources if s.enabled]
    if not sources:
        print("无匹配源")
        return 0

    print(f"探测 {len(sources)} 个源（逐源真拉，约数分钟）...\n")
    results = [probe_one(s) for s in sources]
    summ = summarize(results)
    print(format_report(results, summ))
    written = record_results(results)

    if args.alert:
        msg = build_alert(summ)
        if msg:
            print(f"\n[兼容 --alert：只记录到18:00日报，不外发] {msg}")
        else:
            print("\n[兼容 --alert：无状态项；只记录到18:00日报，不外发]")

    ledger_failed = written != len(results)
    return 1 if summ["enabled_dead"] or ledger_failed else 0


if __name__ == "__main__":
    sys.exit(main())
