"""pipeline.py 测试（不依赖 xtquant 或数据库）。"""

import pytest

from data_collect.pipeline import (
    _topological_sort,
    _get_current_platform,
    TaskResult,
    execute_in_subprocess,
)


def test_topological_sort_simple():
    tasks = [
        {"name": "a", "job": "mod_a"},
        {"name": "b", "job": "mod_b", "depends_on": ["a"]},
    ]
    result = _topological_sort(tasks)
    names = [t["name"] for t in result]
    assert names.index("a") < names.index("b")


def test_topological_sort_no_deps():
    tasks = [
        {"name": "x", "job": "mod_x"},
        {"name": "y", "job": "mod_y"},
    ]
    result = _topological_sort(tasks)
    assert len(result) == 2


def test_topological_sort_diamond():
    tasks = [
        {"name": "a", "job": "m"},
        {"name": "b", "job": "m", "depends_on": ["a"]},
        {"name": "c", "job": "m", "depends_on": ["a"]},
        {"name": "d", "job": "m", "depends_on": ["b", "c"]},
    ]
    result = _topological_sort(tasks)
    names = [t["name"] for t in result]
    assert names.index("a") < names.index("b")
    assert names.index("a") < names.index("c")
    assert names.index("b") < names.index("d")
    assert names.index("c") < names.index("d")


def test_topological_sort_cycle_detection():
    tasks = [
        {"name": "a", "job": "m", "depends_on": ["b"]},
        {"name": "b", "job": "m", "depends_on": ["a"]},
    ]
    with pytest.raises(ValueError, match="循环依赖"):
        _topological_sort(tasks)


def test_topological_sort_missing_dep():
    tasks = [
        {"name": "a", "job": "m", "depends_on": ["nonexistent"]},
    ]
    with pytest.raises(ValueError, match="不存在"):
        _topological_sort(tasks)


def test_get_current_platform():
    plat = _get_current_platform()
    assert plat in ("windows", "linux")


def test_task_result_dataclass():
    r = TaskResult(name="test", success=True, message="ok", duration=1.5)
    assert r.name == "test"
    assert r.success
    assert not r.skipped


# ===== execute_in_subprocess 超时/成功 =====

def test_execute_in_subprocess_success():
    """正常返回时透传 job 函数的返回值。"""
    result = execute_in_subprocess(
        "tests._sleep_job", "run", timeout=5, seconds=0.1,
    )
    assert result == "slept 0.1s"


def test_execute_in_subprocess_timeout_raises():
    """超时时抛 TimeoutError 并强杀子进程。"""
    with pytest.raises(TimeoutError, match="任务执行超时"):
        execute_in_subprocess(
            "tests._sleep_job", "run", timeout=1, seconds=10,
        )


def test_execute_in_subprocess_no_timeout():
    """timeout=None 时不限时（兼容旧调用）。"""
    result = execute_in_subprocess(
        "tests._sleep_job", "run", timeout=None, seconds=0.05,
    )
    assert result == "slept 0.05s"


def test_execute_in_subprocess_propagates_exception(tmp_path):
    """job 内部抛错时原样向上抛。"""
    marker = tmp_path / "counter.txt"
    with pytest.raises(RuntimeError, match="flaky failure"):
        execute_in_subprocess(
            "tests._sleep_job", "run_flaky",
            timeout=5, fail_times=99, marker_path=str(marker),
        )
