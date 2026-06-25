"""
轻量级 DAG 任务编排框架

从 config.yaml 读取 pipeline 定义，按拓扑排序执行任务。
支持：任务依赖、平台过滤、失败跳过下游、单任务执行。

每个任务在独立子进程中执行，完成后自动回收内存和资源。
主进程只负责调度，不执行业务逻辑。
"""

from __future__ import annotations

import importlib
import platform
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List

from data_collect.config import get_pipeline_config
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


def execute_in_subprocess(job_path: str, fn_name: str = "run", **kwargs):
    """在独立子进程中执行 job 模块的指定函数，完成后自动回收资源。"""
    with ProcessPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_call_job_fn, job_path, fn_name, **kwargs)
        return future.result()


def run_pipeline(
    pipeline_name: str = "daily",
    run_date: str | None = None,
    only_task: str | None = None,
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
        job_path = task_cfg["job"]
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

        # 在子进程中执行任务
        print(f"[pipeline] 启动任务: {task_name}")
        start_time = time.time()
        try:
            message = execute_in_subprocess(job_path, "run", run_date=run_date, **kwargs)
            duration = time.time() - start_time
            print(f"[pipeline] 完成: {task_name} ({duration:.1f}s)")
            results.append(TaskResult(
                name=task_name, success=True, message=message, duration=duration,
            ))
        except Exception as exc:
            duration = time.time() - start_time
            error_msg = f"失败: {exc}\n{traceback.format_exc()}"
            print(f"[pipeline] 失败: {task_name} ({duration:.1f}s)")
            results.append(TaskResult(
                name=task_name, success=False, message=error_msg, duration=duration,
            ))
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

    total = len(results)
    success = sum(1 for r in results if r.success and not r.skipped)
    failed = sum(1 for r in results if not r.success)
    skipped = sum(1 for r in results if r.skipped)
    lines.append(f"合计: {total}个任务, {success}成功, {failed}失败, {skipped}跳过")

    send_dingtalk("\n".join(lines))
