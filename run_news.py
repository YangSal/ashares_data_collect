"""新闻采集系统独立入口（与行情 run_job.py 分离，便于 Linux 单独部署/服务化）。

用法：
  python run_news.py                                # 启动调度（注册全部新闻 pipeline，默认）
  python run_news.py --pipeline news_daily          # 只调度指定新闻 pipeline（逗号可分隔多个）
  python run_news.py --mode pipeline --pipeline news_digest
                                                    # 手动补发最近完整18:00窗口日报
  python run_news.py --mode pipeline --pipeline news_daily [--date YYYYMMDD] [--task NAME]
                                                    # 立即执行一条新闻 pipeline（调试/补跑）
  python run_news.py --mode backfill --task news_cctv --start 20260601 --end 20260701
                                                    # 补历史（幂等：已有数据日计"重复"跳过）
  python run_news.py --mode verify --task news_flash --start 20260701 --end 20260707
                                                    # 查漏补缺（news 系按自然日语义，窗口入参被忽略）

**只接受新闻系 pipeline/任务**（行情请用 run_job.py，反向同样被拦）。调度/执行逻辑
全部委托既有框架（run_job 的 start_scheduler/run_backfill/_run_job_directly 与
data_collect.pipeline.run_pipeline），本文件只做入口收窄。
"""

from __future__ import annotations

import argparse

from data_collect.utils.news_digest import enable_digest_mode

from run_job import (  # 单一事实源：常量与执行函数均复用 run_job
    NEWS_PIPELINES,
    _news_task_names,
    _run_job_directly,
    run_backfill,
    start_scheduler,
)


def _validate_pipelines(names: list) -> None:
    """pipeline 名必须全部是新闻管线，否则报错退出（职责分离的硬边界）。"""
    bad = [n for n in names if n not in NEWS_PIPELINES]
    if bad:
        raise SystemExit(
            f"错误：{bad} 不是新闻 pipeline（可用: {list(NEWS_PIPELINES)}）；"
            f"行情调度请用 run_job.py")


def _validate_task(task: str | None, mode: str) -> None:
    """任务名必须属于新闻管线（backfill/verify 模式）。"""
    if not task:
        raise SystemExit(f"错误：{mode} 模式必须指定 --task")
    if task not in _news_task_names():
        raise SystemExit(
            f"错误：'{task}' 不是新闻系统任务（可用: {sorted(_news_task_names())}）；"
            f"行情任务请用 run_job.py")


def main() -> None:
    # 必须早于 scheduler/pipeline/backfill/verify 的任何任务调用；环境变量会被
    # ProcessPoolExecutor 子进程继承，使源级 send_dingtalk 同样改为写事件账本。
    enable_digest_mode()
    parser = argparse.ArgumentParser(
        description="新闻采集系统入口（独立于行情 run_job.py）")
    parser.add_argument(
        "--mode", choices=["scheduler", "pipeline", "backfill", "verify"],
        default="scheduler",
        help="scheduler=定时调度（默认）；pipeline=立即执行一条管线；"
             "backfill=补历史；verify=查漏补缺")
    parser.add_argument(
        "--pipeline", default=None,
        help=f"pipeline 名（逗号可分隔多个）；scheduler 模式缺省=全部新闻管线"
             f"（{','.join(NEWS_PIPELINES)}）；pipeline 模式必须恰指定一条")
    parser.add_argument("--task", default=None,
                        help="任务名（backfill/verify 必填；pipeline 模式可选=只执行该任务）")
    parser.add_argument("--date", default=None,
                        help="执行日期 YYYYMMDD 或 YYYY-MM-DD（仅 mode=pipeline）")
    parser.add_argument("--start", default=None, help="起始日期 YYYYMMDD（backfill/verify）")
    parser.add_argument("--end", default=None, help="结束日期 YYYYMMDD（backfill/verify）")
    args = parser.parse_args()

    if args.mode == "scheduler":
        raw = args.pipeline or ",".join(NEWS_PIPELINES)
        names = [n.strip() for n in raw.split(",") if n.strip()]
        _validate_pipelines(names)
        start_scheduler(pipeline_name=",".join(names))
        return

    if args.mode == "pipeline":
        # 立即执行恰一条新闻 pipeline（调试/补跑）
        if not args.pipeline or "," in args.pipeline:
            raise SystemExit("错误：mode=pipeline 需用 --pipeline 指定恰一条新闻 pipeline")
        _validate_pipelines([args.pipeline])
        from data_collect.pipeline import run_pipeline
        results = run_pipeline(pipeline_name=args.pipeline,
                               run_date=args.date, only_task=args.task)
        for r in results:
            status = "跳过" if r.skipped else ("成功" if r.success else "失败")
            print(f"[{status}] {r.name}: {r.message[:200]}")
        return

    # backfill / verify：任务级操作（与 run_job 同款语义，仅收窄到新闻任务）
    _validate_task(args.task, args.mode)
    if not args.start or not args.end:
        raise SystemExit(f"错误：{args.mode} 模式必须指定 --start 和 --end")

    if args.mode == "backfill":
        run_backfill(args.task, args.start, args.end)
    else:  # verify
        result = _run_job_directly(args.task, "run_verify",
                                   start_date=args.start, end_date=args.end)
        print(result)


if __name__ == "__main__":
    main()
