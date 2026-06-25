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


_CRON_KEYS = ("year", "month", "day", "day_of_week", "hour", "minute", "second", "week")


def _build_cron_kwargs(schedule_cfg: dict, hour_override=None, minute_override=None) -> dict:
    """从 schedule 配置抽取 APScheduler cron 字段。"""
    cron = {k: v for k, v in schedule_cfg.items() if k in _CRON_KEYS}
    if hour_override is not None:
        cron["hour"] = hour_override
    if minute_override is not None:
        cron["minute"] = minute_override
    cron.setdefault("second", 0)
    return cron


def start_scheduler(
    pipeline_name: str | None = None,
    hour: int | None = None,
    minute: int | None = None,
) -> None:
    """启动 BlockingScheduler，注册所有有 schedule 段的 pipeline。

    pipeline_name=None：注册全部 pipeline；指定时仅注册该 pipeline。
    hour/minute 仅在指定单 pipeline 时生效（覆盖配置）。
    """
    from data_collect.pipeline import run_pipeline, print_dag
    from data_collect.config import get_pipeline_config

    pipelines = get_pipeline_config()

    if pipeline_name:
        if pipeline_name not in pipelines:
            print(f"错误：pipeline '{pipeline_name}' 不存在，可用: {list(pipelines.keys())}")
            return
        targets = {pipeline_name: pipelines[pipeline_name]}
    else:
        targets = {n: c for n, c in pipelines.items() if c.get("schedule")}

    if not targets:
        print("错误：未找到任何带 schedule 段的 pipeline")
        return

    timezone = next(iter(targets.values())).get("schedule", {}).get("timezone", "Asia/Shanghai")

    BlockingScheduler = _require_blocking_scheduler()
    scheduler = BlockingScheduler(timezone=timezone)

    for name, cfg in targets.items():
        sch = cfg.get("schedule", {})
        if pipeline_name and (hour is not None or minute is not None):
            cron_kwargs = _build_cron_kwargs(sch, hour, minute)
        else:
            cron_kwargs = _build_cron_kwargs(sch)

        scheduler.add_job(
            run_pipeline,
            trigger="cron",
            # show_dag=False：DAG 已在启动时展示，cron 触发时无需再印
            kwargs={"pipeline_name": name, "show_dag": False},
            id=f"{name}_pipeline_job",
            replace_existing=True,
            **cron_kwargs,
        )
        cron_str = " ".join(f"{k}={v}" for k, v in cron_kwargs.items())
        print(f"  [OK] 已注册: {name}  [{cron_str}]")
        print_dag(name)

    print(f"\n定时任务启动完毕，共 {len(targets)} 个 pipeline，时区 {timezone}")
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
        "--pipeline", default=None,
        help="指定 pipeline 名称（pipeline/once 模式默认 daily；scheduler 模式默认全部注册）",
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
            pipeline_name=args.pipeline or "daily",
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
        pipeline_name=args.pipeline or "daily",
        run_date=args.date,
        only_task="a_share_minute",
        limit_stocks=args.limit_stocks,
    )
    for r in results:
        print(r.message[:500])


if __name__ == "__main__":
    main()
