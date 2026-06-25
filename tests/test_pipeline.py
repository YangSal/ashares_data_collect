"""pipeline.py 测试（不依赖 xtquant 或数据库）。"""

import pytest

from data_collect.pipeline import _topological_sort, _get_current_platform, TaskResult


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
