"""
轻量级 DAG 任务编排框架

从 config.yaml 读取 pipeline 定义，按拓扑排序执行任务。
支持：任务依赖、平台过滤、失败跳过下游、单任务执行。

每个任务在独立子进程中执行，完成后自动回收内存和资源。
主进程只负责调度，不执行业务逻辑。
"""

from __future__ import annotations

import datetime
import importlib
import platform
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Any, Dict, List

from data_collect.config import get_pipeline_config
from data_collect.utils.date_utils import add_mark_day, minus_one_market_day
from data_collect.utils.notify import send_dingtalk

import logging

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    name: str
    success: bool
    message: str
    duration: float = 0.0
    skipped: bool = False


def _get_current_platform() -> str:
    """返回 'windows' 或 'linux'。"""
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    return "linux"


def _topological_sort(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对任务列表按 depends_on 进行拓扑排序。"""
    name_to_task = {t["name"]: t for t in tasks}
    in_degree = {t["name"]: 0 for t in tasks}
    dependents = {t["name"]: [] for t in tasks}

    for t in tasks:
        for dep in t.get("depends_on", []):
            if dep not in name_to_task:
                raise ValueError(f"任务 '{t['name']}' 依赖的 '{dep}' 不存在")
            in_degree[t["name"]] += 1
            dependents[dep].append(t["name"])

    name_order = {name: i for i, name in enumerate(name_to_task)}
    queue = [name for name, deg in in_degree.items() if deg == 0]
    sorted_names = []

    while queue:
        queue.sort(key=lambda n: name_order[n])
        current = queue.pop(0)
        sorted_names.append(current)
        for dep_name in dependents[current]:
            in_degree[dep_name] -= 1
            if in_degree[dep_name] == 0:
                queue.append(dep_name)

    if len(sorted_names) != len(tasks):
        raise ValueError("检测到循环依赖，请检查 pipeline 配置")

    return [name_to_task[n] for n in sorted_names]


def print_dag(pipeline_name: str = "daily") -> None:
    """在终端打印 pipeline 的 DAG 可视化图。"""
    try:
        import networkx as nx
        from phart import ASCIIRenderer
    except ImportError:
        logger.warning("缺少 phart/networkx，跳过 DAG 可视化（pip install phart）")
        return

    pipelines = get_pipeline_config()
    pipeline_cfg = pipelines.get(pipeline_name)
    if not pipeline_cfg:
        return

    tasks = pipeline_cfg.get("tasks", [])
    if not tasks:
        return

    G = nx.DiGraph()
    for t in tasks:
        G.add_node(t["name"])
        for dep in t.get("depends_on", []):
            G.add_edge(dep, t["name"])

    renderer = ASCIIRenderer(G)
    print(f"\n{'='*60}")
    print(f"  Pipeline [{pipeline_name}] 任务编排")
    print(f"{'='*60}")
    print(renderer.render())
    print(f"{'='*60}\n")


def _call_job_fn(job_path: str, fn_name: str, **kwargs):
    """在子进程中调用 job 模块的指定函数（被 submit 到 ProcessPoolExecutor）。"""
    module = importlib.import_module(job_path)
    fn = getattr(module, fn_name, None)
    if fn is None:
        raise AttributeError(f"模块 {job_path} 未实现 {fn_name}() 函数")
    return fn(**kwargs)


def _kill_executor_workers(executor: ProcessPoolExecutor) -> None:
    """强杀 ProcessPoolExecutor 的所有 worker（用于超时硬退出，释放 xtquant 资源）。

    注意：依赖 CPython 私有属性 `_processes`（dict[pid, Process]）。
    Python 3.4-3.13 一直存在；若未来移除，回退到 shutdown(wait=False)
    （不会真正强杀，需重新实现）。
    """
    workers = list(getattr(executor, "_processes", {}).values())
    for p in workers:
        try:
            p.terminate()
        except Exception:
            pass
    for p in workers:
        try:
            p.join(timeout=2)
            if p.is_alive():
                p.kill()
                p.join(timeout=2)
        except Exception:
            pass


def execute_in_subprocess(
    job_path: str,
    fn_name: str = "run",
    timeout: float | None = None,
    **kwargs,
):
    """在独立子进程中执行 job 模块的指定函数，超时则硬杀子进程并抛 TimeoutError。

    timeout 单位秒；None 表示不限时（兼容旧调用）。
    """
    executor = ProcessPoolExecutor(max_workers=1)
    timed_out = False
    try:
        future = executor.submit(_call_job_fn, job_path, fn_name, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            timed_out = True
            _kill_executor_workers(executor)
            raise TimeoutError(
                f"任务执行超时（{timeout}s）— xtquant/外部依赖可能挂死，已强杀子进程"
            )
    finally:
        if timed_out:
            # 兜底：超时后再次确保 worker 已死，避免僵尸进程
            _kill_executor_workers(executor)
        executor.shutdown(wait=False)


def _resolve_invocation(
    task_cfg: Dict[str, Any], run_date: str | None, base_kwargs: Dict[str, Any],
) -> tuple[str, Dict[str, Any]]:
    """根据任务配置决定子进程要调的 fn 名和它的 kwargs。

    - fn=run（默认）：传 run_date + base_kwargs（CLI 透传的 limit_stocks 等）
    - fn=run_verify：传 start_date/end_date，由 days_back 推算
    """
    fn_name = task_cfg.get("fn", "run")
    if fn_name == "run_verify":
        days_back = int(task_cfg.get("days_back", 5))
        end_date = minus_one_market_day(datetime.datetime.now())
        start_date = add_mark_day(end_date, -(days_back - 1))
        # 透传 base_kwargs（如 CLI --limit-stocks），run_verify 接收 **kwargs
        return fn_name, {"start_date": start_date, "end_date": end_date, **base_kwargs}
    return fn_name, {"run_date": run_date, **base_kwargs}


def _notify_retry(task_name: str, attempt: int, total: int, exc: Exception) -> None:
    """重试前发钉钉告警；通知失败仅 log，不影响主流程。"""
    is_timeout = isinstance(exc, TimeoutError)
    warn = (
        f"[pipeline] 任务 {task_name} 第 {attempt}/{total} 次"
        f"{'超时' if is_timeout else '失败'}: {exc}，准备重试..."
    )
    print(warn)
    try:
        send_dingtalk(warn)
    except Exception as notify_exc:
        logger.warning(f"钉钉重试告警发送失败: {notify_exc}")


def _run_one_task(
    task_cfg: Dict[str, Any], run_date: str | None, base_kwargs: Dict[str, Any],
) -> TaskResult:
    """在子进程中执行一个任务，自动处理超时和重试，返回 TaskResult。"""
    task_name = task_cfg["name"]
    job_path = task_cfg["job"]
    timeout = task_cfg.get("timeout")
    max_retries = max(0, int(task_cfg.get("retries", 0)))
    attempts_total = 1 + max_retries
    fn_name, call_kwargs = _resolve_invocation(task_cfg, run_date, base_kwargs)

    print(f"[pipeline] 启动任务: {task_name} (fn={fn_name}, timeout={timeout}, retries={max_retries})")
    start_time = time.monotonic()
    last_exc: Exception | None = None
    last_tb = ""

    for attempt in range(1, attempts_total + 1):
        try:
            message = execute_in_subprocess(job_path, fn_name, timeout=timeout, **call_kwargs)
            duration = time.monotonic() - start_time
            print(f"[pipeline] 完成: {task_name} ({duration:.1f}s)")
            return TaskResult(name=task_name, success=True, message=message, duration=duration)
        except Exception as exc:
            last_exc = exc
            last_tb = traceback.format_exc()
            if attempt < attempts_total:
                _notify_retry(task_name, attempt, attempts_total, exc)

    duration = time.monotonic() - start_time
    error_msg = f"失败（{attempts_total}次后放弃）: {last_exc}\n{last_tb}"
    print(f"[pipeline] 失败: {task_name} ({duration:.1f}s)")
    print(error_msg)
    return TaskResult(name=task_name, success=False, message=error_msg, duration=duration)


def run_pipeline(
    pipeline_name: str = "daily",
    run_date: str | None = None,
    only_task: str | None = None,
    show_dag: bool = True,
    **kwargs,
) -> List[TaskResult]:
    """
    执行指定 pipeline。

    每个任务在独立子进程中运行，完成后进程退出、内存释放。
    主进程只负责调度和结果收集。
    """
    pipelines = get_pipeline_config()
    if pipeline_name not in pipelines:
        raise ValueError(f"Pipeline '{pipeline_name}' 不存在，可用: {list(pipelines.keys())}")

    pipeline_cfg = pipelines[pipeline_name]
    tasks = pipeline_cfg.get("tasks", [])
    if not tasks:
        raise ValueError(f"Pipeline '{pipeline_name}' 没有定义任务")

    if show_dag:
        print_dag(pipeline_name)

    current_platform = _get_current_platform()
    sorted_tasks = _topological_sort(tasks)

    if only_task:
        matched = [t for t in sorted_tasks if t["name"] == only_task]
        if not matched:
            available = [t["name"] for t in sorted_tasks]
            raise ValueError(f"任务 '{only_task}' 不存在，可用: {available}")
        sorted_tasks = matched

    results: List[TaskResult] = []
    failed_tasks: set = set()

    for task_cfg in sorted_tasks:
        task_name = task_cfg["name"]
        task_platform = task_cfg.get("platform")
        depends = task_cfg.get("depends_on", [])

        # 平台过滤
        if task_platform and task_platform != current_platform:
            results.append(TaskResult(
                name=task_name, success=True, skipped=True,
                message=f"跳过（平台不匹配：需要 {task_platform}，当前 {current_platform}）",
            ))
            continue

        # 依赖失败检查
        if not only_task:
            failed_deps = [d for d in depends if d in failed_tasks]
            if failed_deps:
                results.append(TaskResult(
                    name=task_name, success=False, skipped=True,
                    message=f"跳过（上游任务失败: {failed_deps}）",
                ))
                failed_tasks.add(task_name)
                continue

        # 在子进程中执行任务（含超时 + 重试）
        result = _run_one_task(task_cfg, run_date, kwargs)
        results.append(result)
        if not result.success:
            failed_tasks.add(task_name)

    _send_pipeline_summary(pipeline_name, run_date, results)
    return results


def _send_pipeline_summary(pipeline_name: str, run_date: str | None, results: List[TaskResult]) -> None:
    """发送 pipeline 执行汇总通知。"""
    lines = [f"流水线 [{pipeline_name}] 执行完毕 (日期: {run_date or '今天'})"]
    for r in results:
        if r.skipped:
            status = "⏭跳过"
        elif r.success:
            status = "✅成功"
        else:
            status = "❌失败"
        duration_str = f" ({r.duration:.1f}s)" if r.duration > 0 else ""
        lines.append(f"  {status} {r.name}{duration_str}")
        if not r.success and not r.skipped and r.message:
            first_line = r.message.split("\n")[0][:200]
            lines.append(f"    ↳ {first_line}")

    total = len(results)
    success_cnt = sum(1 for r in results if r.success and not r.skipped)
    failed_cnt = sum(1 for r in results if not r.success)
    skipped_cnt = sum(1 for r in results if r.skipped)
    lines.append(f"合计: {total}个任务, {success_cnt}成功, {failed_cnt}失败, {skipped_cnt}跳过")

    send_dingtalk("\n".join(lines))
