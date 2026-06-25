"""
数据采集任务 CLI 入口

用法：
  python run_job.py --mode pipeline [--date YYYYMMDD] [--task NAME]
  python run_job.py --mode backfill --task NAME --start YYYYMMDD --end YYYYMMDD
  python run_job.py --mode once [--date YYYYMMDD] [--limit-stocks N]
  python run_job.py --mode export-only [--date YYYYMMDD] [--limit-stocks N]
  python run_job.py --mode scheduler [--hour H] [--minute M]
  python run_job.py --mode test
"""

from __future__ import annotations

import argparse
import importlib
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _require_blocking_scheduler():
    try:
        blocking_module = importlib.import_module("apscheduler.schedulers.blocking")
        BlockingScheduler = getattr(blocking_module, "BlockingScheduler")
    except Exception as exc:
        raise ImportError("缺少 apscheduler，请先安装：pip install apscheduler") from exc
    return BlockingScheduler


def start_scheduler(
    pipeline_name: str = "daily",
    hour: int | None = None,
    minute: int | None = None,
) -> None:
    """启动 BlockingScheduler，每天执行指定 pipeline。"""
    from data_collect.pipeline import run_pipeline
    from data_collect.config import get_pipeline_config

    pipelines = get_pipeline_config()
    pipeline_cfg = pipelines.get(pipeline_name, {})
    schedule_cfg = pipeline_cfg.get("schedule", {})
    hour = hour if hour is not None else schedule_cfg.get("hour", 15)
    minute = minute if minute is not None else schedule_cfg.get("minute", 30)
    timezone = schedule_cfg.get("timezone", "Asia/Shanghai")

    from data_collect.pipeline import print_dag
    print_dag(pipeline_name)

    BlockingScheduler = _require_blocking_scheduler()
    scheduler = BlockingScheduler(timezone=timezone)
    scheduler.add_job(
        run_pipeline,
        trigger="cron",
        hour=hour,
        minute=minute,
        second=0,
        kwargs={"pipeline_name": pipeline_name},
        id=f"{pipeline_name}_pipeline_job",
        replace_existing=True,
    )
    print(f"定时任务已启动：每天 {hour:02d}:{minute:02d} 执行 {pipeline_name} pipeline")
    scheduler.start()


def run_tests() -> None:
    """运行 pytest 测试。"""
    import subprocess
    import sys
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "tests/", "-v"]))


def _resolve_job_path(task_name: str) -> str:
    """从 pipeline 配置中查找任务对应的 job 模块路径。"""
    from data_collect.config import get_pipeline_config

    for pipeline_cfg in get_pipeline_config().values():
        for task in pipeline_cfg.get("tasks", []):
            if task["name"] == task_name:
                return task["job"]
    raise ValueError(f"任务 '{task_name}' 未在任何 pipeline 中定义")


def _run_job_directly(task_name: str, fn_name: str, **kwargs) -> str:
    """在主进程中直接调用 job 函数（保留 TTY，tqdm 可正常刷新）。"""
    job_path = _resolve_job_path(task_name)
    module = importlib.import_module(job_path)
    fn = getattr(module, fn_name, None)
    if fn is None:
        raise AttributeError(f"模块 {job_path} 未实现 {fn_name}() 函数")
    return fn(**kwargs)


def run_backfill(task_name: str, start_date: str, end_date: str, limit_stocks: int | None = None) -> None:
    """补历史数据：在主进程中直接调用（保留TTY，进度条可正常刷新）。"""
    result = _run_job_directly(
        task_name, "run_backfill",
        start_date=start_date, end_date=end_date, limit_stocks=limit_stocks,
    )
    print(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="数据采集任务入口")
    parser.add_argument(
        "--mode",
        choices=["once", "export-only", "pipeline", "backfill", "verify", "evaluate", "scheduler", "test"],
        default="once",
        help=(
            "运行模式：once=单次采集入库，export-only=只导出，"
            "pipeline=按DAG执行流水线，backfill=补历史数据，"
            "verify=查漏补缺，evaluate=评估数据缺失并输出CSV，"
            "scheduler=启动定时任务，test=运行测试"
        ),
    )
    parser.add_argument(
        "--date", default=None,
        help="指定执行日期，格式YYYYMMDD或YYYY-MM-DD",
    )
    parser.add_argument(
        "--task", default=None,
        help="指定任务名称（pipeline模式下仅执行该任务，backfill模式必填）",
    )
    parser.add_argument(
        "--start", default=None,
        help="补历史起始日期 YYYYMMDD（仅 backfill 模式）",
    )
    parser.add_argument(
        "--end", default=None,
        help="补历史结束日期 YYYYMMDD（仅 backfill 模式）",
    )
    parser.add_argument(
        "--pipeline", default="daily",
        help="指定 pipeline 名称（默认 daily）",
    )
    parser.add_argument(
        "--hour", type=int, default=None,
        help="定时任务小时（0-23），仅 mode=scheduler 有效",
    )
    parser.add_argument(
        "--minute", type=int, default=None,
        help="定时任务分钟（0-59），仅 mode=scheduler 有效",
    )
    parser.add_argument(
        "--limit-stocks", type=int, default=None,
        help="仅处理前N只股票（用于快速验证）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.mode == "test":
        run_tests()
        return

    if args.mode == "scheduler":
        start_scheduler(pipeline_name=args.pipeline, hour=args.hour, minute=args.minute)
        return

    if args.mode == "pipeline":
        from data_collect.pipeline import run_pipeline
        results = run_pipeline(
            pipeline_name=args.pipeline,
            run_date=args.date,
            only_task=args.task,
            limit_stocks=args.limit_stocks,
        )
        for r in results:
            status = "跳过" if r.skipped else ("成功" if r.success else "失败")
            print(f"[{status}] {r.name}: {r.message[:200]}")
        return

    if args.mode == "backfill":
        if not args.task:
            print("错误：backfill 模式必须指定 --task")
            return
        if not args.start or not args.end:
            print("错误：backfill 模式必须指定 --start 和 --end")
            return
        run_backfill(args.task, args.start, args.end, limit_stocks=args.limit_stocks)
        return

    if args.mode == "verify":
        if not args.task:
            print("错误：verify 模式必须指定 --task")
            return
        if not args.start or not args.end:
            print("错误：verify 模式必须指定 --start 和 --end")
            return
        result = _run_job_directly(
            args.task, "run_verify",
            start_date=args.start, end_date=args.end, limit_stocks=args.limit_stocks,
        )
        print(result)
        return

    if args.mode == "evaluate":
        if not args.task:
            print("错误：evaluate 模式必须指定 --task")
            return
        if not args.start or not args.end:
            print("错误：evaluate 模式必须指定 --start 和 --end")
            return
        result = _run_job_directly(
            args.task, "run_evaluate",
            start_date=args.start, end_date=args.end,
        )
        print(result)
        return

    # once / export-only：通过 pipeline 单任务模式执行（同样走子进程隔离）
    if args.mode == "export-only":
        print("提示：export-only 模式建议迁移到 pipeline 模式")

    from data_collect.pipeline import run_pipeline
    results = run_pipeline(
        pipeline_name=args.pipeline,
        run_date=args.date,
        only_task="a_share_minute",
        limit_stocks=args.limit_stocks,
    )
    for r in results:
        print(r.message[:500])


if __name__ == "__main__":
    main()
