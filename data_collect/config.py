"""
配置加载模块

从项目根目录的 config.yaml 读取配置。
可通过环境变量 DATA_COLLECT_CONFIG 指定自定义路径。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"

_config_cache: Dict[str, Any] | None = None


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
        _config_cache = yaml.safe_load(f)

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


def reload_config() -> None:
    """强制重新加载配置（测试时使用）。"""
    global _config_cache
    _config_cache = None
