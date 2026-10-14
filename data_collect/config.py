"""
配置加载模块

从项目根目录的 config.yaml 读取配置。
可通过环境变量 DATA_COLLECT_CONFIG 指定自定义路径。

节点本地覆盖（多节点 Phase A1）：仓库根若存在 config.local.yaml（gitignore，
每节点一份只放差异项），按**顶层键浅合并**覆盖主配置——两边同为 dict 的顶层键
做一层合并（local 子键覆盖、其余保留），标量/列表/新键整体替换。用途：节点差异
（代理地址、dingtalk.enabled、调度强度）不进 git，主配置保持一份。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_LOCAL_CONFIG_PATH = _PROJECT_ROOT / "config.local.yaml"   # 节点差异覆盖（gitignore）

_config_cache: Dict[str, Any] | None = None


def _shallow_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """顶层键浅合并：两边同为 dict 的键做一层合并（override 子键覆盖），
    其余（标量/列表/新键）整体替换。不修改入参。"""
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _load_config() -> Dict[str, Any]:
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config_path = Path(os.environ.get("DATA_COLLECT_CONFIG", str(_DEFAULT_CONFIG_PATH)))
    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}\n"
            f"请复制 config.example.yaml 为 config.yaml 并填入实际配置"
        )

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if _LOCAL_CONFIG_PATH.exists():
        with open(_LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
            local = yaml.safe_load(f) or {}
        data = _shallow_merge(data, local)

    _config_cache = data
    return _config_cache


def get_db_config() -> Dict[str, Any]:
    return dict(_load_config()["database"])


def get_dingtalk_config() -> Dict[str, Any]:
    return dict(_load_config()["dingtalk"])


def get_export_config() -> Dict[str, Any]:
    return dict(_load_config()["export"])


def get_pipeline_config() -> Dict[str, Any]:
    return dict(_load_config().get("pipelines", {}))


def get_tick_config() -> Dict[str, Any]:
    return dict(_load_config().get("tick_storage", {}))


def get_qmt_config() -> Dict[str, Any]:
    return dict(_load_config().get("qmt", {}))


def get_market_data_config() -> Dict[str, Any]:
    """Optional per-task routes and TdxAiData settings; default retains QMT."""
    return dict(_load_config().get("market_data", {}))


def get_opensearch_config() -> Dict[str, Any]:
    return dict(_load_config().get("opensearch", {}))


def get_news_config() -> Dict[str, Any]:
    return dict(_load_config().get("news", {}))


def get_tunnel_config() -> Dict[str, Any]:
    """多节点隧道段（tools/run_node.py 托管 frpc 用；缺省空=不起隧道）。"""
    return dict(_load_config().get("tunnel", {}))


def reload_config() -> None:
    """强制重新加载配置（测试时使用）。"""
    global _config_cache
    _config_cache = None
