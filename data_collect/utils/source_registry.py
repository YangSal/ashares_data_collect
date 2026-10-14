"""数据源注册表加载器（spec docs/superpowers/specs/2026-07-08-source-registry-design.md）。

两层架构的注册表侧：sources.yaml（仓库根，git 版本化）集中登记采集型源，
本模块负责加载/校验/命名解析。**无缓存无监听**——job 每轮跑在新子进程，
进程启动时读一次即最新（热生效的全部实现）。

降级语义（采集连续性优先，同 news_archive 的 spool 哲学）：
- 每次成功加载后刷新 <spool>/.sources_last_good.yaml 副本；
- 主文件坏（语法/校验）→ 沿用 last-good + 钉钉告警；
- 双失败 → raise（宁停不错采）。
"""

from __future__ import annotations

import dataclasses
import logging
import re
import shutil
from pathlib import Path
from typing import Dict, List

import yaml

from data_collect.config import get_news_config
from data_collect.utils import news_archive
from data_collect.utils import notify

logger = logging.getLogger(__name__)

# 项目根：data_collect 包上一级（.../data_collect/utils/source_registry.py → parents[2]）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = _PROJECT_ROOT / "sources.yaml"
_LAST_GOOD_NAME = ".sources_last_good.yaml"

VERSION = 1
ADAPTERS = ("rss", "rsshub", "listpage", "akshare", "api")
CHANNELS = ("flash", "cctv", "policy", "media", "report", "announcement",
            "stock", "us_policy", "us_filing", "us_news", "intl_news")
JOBS = ("news_flash", "news_cctv", "news_policy", "news_regulator",
        "news_us", "news_announcement", "news_stock")
HEALTH_PROFILES = ("continuous", "business", "daily", "event")
PROXIES = {"us": "us_proxy"}   # 命名代理 → config.yaml news 段的键（见 _resolve_proxy）
_DEFAULT_TIMEOUT = 45          # 单源 HTTP 超时缺省（dataclass 与 _to_sources 共用）

# 命名 header 集（源自 news_us；SEC 要求声明式 UA 含联系方式，勿改浏览器 UA）
HEADER_SETS: Dict[str, dict] = {
    "browser": {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36")},
    "sec_declared": {"User-Agent": "data_collect research youngman.cn@gmail.com"},
}

_ID_RE = re.compile(r"^[a-z0-9_]+$")
_TOP_KEYS = {"version", "defaults", "sources"}
_DEFAULT_KEYS = {"enabled", "timeout"}
_SOURCE_KEYS = {"id", "adapter", "channel", "job", "url", "route", "proxy",
                "headers", "timeout", "enabled", "note", "health_profile",
                "stale_hours"}


@dataclasses.dataclass(frozen=True)
class Source:
    """注册表单源（proxy/headers 已解析为实际值，job 代码不再碰 config）。"""
    id: str
    adapter: str
    channel: str
    job: str
    url: str = ""
    route: str = ""
    proxy_url: str = ""                                   # 空=直连
    proxy_name: str = ""      # 声明的命名代理（原始值；与解析结果并存，供 proxy_missing 判定）
    headers: dict = dataclasses.field(default_factory=dict)
    timeout: int = _DEFAULT_TIMEOUT   # HTTP 超时，已接线（三期）：feed 类经 source_adapters 生效
    enabled: bool = True
    note: str = ""
    health_profile: str = ""
    stale_hours: float | None = None


def _validate(data) -> List[str]:
    """schema 逐规则校验，返回错误列表（空=通过）。fail-fast 契约：
    配置手误静默忽略=静默停采，未知键一律报错。"""
    if not isinstance(data, dict):
        return [f"顶层应为 mapping，实际 {type(data).__name__}"]
    errors: List[str] = []
    unknown_top = set(data) - _TOP_KEYS
    if unknown_top:
        errors.append(f"未知顶层键: {sorted(unknown_top)}")
    if data.get("version") != VERSION:
        errors.append(f"version 应为 {VERSION}，实际 {data.get('version')!r}")
    unknown_def = set(data.get("defaults") or {}) - _DEFAULT_KEYS
    if unknown_def:
        errors.append(f"defaults 未知键: {sorted(unknown_def)}")

    seen_ids: set = set()
    for i, src in enumerate(data.get("sources") or []):
        where = f"sources[{i}]"
        if not isinstance(src, dict):
            errors.append(f"{where} 应为 mapping")
            continue
        unknown = set(src) - _SOURCE_KEYS
        if unknown:
            errors.append(f"{where} 未知键: {sorted(unknown)}")
        sid = src.get("id")
        if not (isinstance(sid, str) and _ID_RE.match(sid)):
            errors.append(f"{where} id 非法（需 [a-z0-9_]+）: {sid!r}")
        elif sid in seen_ids:
            errors.append(f"{where} id 重复: {sid}")
        else:
            seen_ids.add(sid)
        if src.get("adapter") not in ADAPTERS:
            errors.append(f"{where} adapter 非法: {src.get('adapter')!r}")
        if src.get("channel") not in CHANNELS:
            errors.append(f"{where} channel 非法: {src.get('channel')!r}")
        if src.get("job") not in JOBS:
            errors.append(f"{where} job 非法: {src.get('job')!r}")
        if src.get("adapter") == "rss" and not src.get("url"):
            errors.append(f"{where}({sid}) adapter=rss 必须有 url")
        if src.get("adapter") == "rsshub" and not src.get("route"):
            errors.append(f"{where}({sid}) adapter=rsshub 必须有 route")
        if "proxy" in src and src["proxy"] not in PROXIES:
            errors.append(f"{where}({sid}) proxy 非法: {src['proxy']!r}"
                          f"（可用: {tuple(PROXIES)}）")
        if "headers" in src and src["headers"] not in HEADER_SETS:
            errors.append(f"{where}({sid}) headers 非法: {src['headers']!r}"
                          f"（可用: {tuple(HEADER_SETS)}）")
        profile = src.get("health_profile")
        if profile is not None and profile not in HEALTH_PROFILES:
            errors.append(f"{where}({sid}) health_profile 非法: {profile!r}"
                          f"（可用: {HEALTH_PROFILES}）")
        if "stale_hours" in src:
            value = src.get("stale_hours")
            try:
                hours = float(value)
            except (TypeError, ValueError):
                hours = 0.0
            if isinstance(value, bool) or hours <= 0:
                errors.append(f"{where}({sid}) stale_hours 应为正数: {value!r}")
    return errors


def validate_registry(path=None) -> List[str]:
    """校验注册表文件，返回错误列表（CLI validate 用；不解析 proxy 不读 config）。"""
    path = Path(path or REGISTRY_PATH)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 读失败/语法错统一报为校验错误
        return [f"YAML 读取/解析失败: {exc!r}"]
    return _validate(data)


def _resolve_proxy(name) -> str:
    """命名代理 → 实际 URL（按 PROXIES 映射到 config news 段的键；空/未配置=直连）。

    name 已经 schema 校验 ∈ PROXIES（load_all 先 validate）；按名查键，加第二个代理
    （如 eu）只需在 PROXIES 加一行，不会静默落到 us_proxy（评审：半成品命名索引）。
    `news.proxy_enabled: false`（应急总开关，Phase A4）→ 全部命名代理解析为空，
    即全部代理源在本节点让位（配合 proxy_missing 跳过语义）。
    """
    if not name:
        return ""
    news_cfg = get_news_config()
    if not news_cfg.get("proxy_enabled", True):
        return ""
    return str(news_cfg.get(PROXIES[name]) or "").strip()


def _to_sources(data) -> List[Source]:
    defaults = data.get("defaults") or {}
    d_enabled = bool(defaults.get("enabled", True))
    d_timeout = int(defaults.get("timeout", _DEFAULT_TIMEOUT))
    return [Source(
        id=src["id"], adapter=src["adapter"],
        channel=src["channel"], job=src["job"],
        url=str(src.get("url") or ""), route=str(src.get("route") or ""),
        proxy_url=_resolve_proxy(src.get("proxy")),
        proxy_name=str(src.get("proxy") or ""),
        headers=dict(HEADER_SETS.get(src.get("headers"), {})),
        timeout=int(src.get("timeout", d_timeout)),
        enabled=bool(src.get("enabled", d_enabled)),
        note=str(src.get("note") or ""),
        health_profile=str(src.get("health_profile") or ""),
        stale_hours=(float(src["stale_hours"])
                     if src.get("stale_hours") is not None else None),
    ) for src in data.get("sources") or []]


def _spool_dir() -> Path:
    """last-good 副本目录：直接复用 news_archive 的 spool 根（同址，单一实现）。"""
    return news_archive._spool_root()


def _load_validated(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors = _validate(data)
    if errors:
        raise ValueError(f"sources.yaml 校验失败: {errors[:5]}")
    return data


def load_all() -> List[Source]:
    """加载全量源（含 disabled）；主文件坏 → last-good 降级+告警；双失败 raise。"""
    last_good = _spool_dir() / _LAST_GOOD_NAME
    try:
        data = _load_validated(REGISTRY_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"sources.yaml 加载失败，尝试 last-good 副本: {exc!r}")
        try:
            data = _load_validated(last_good)
        except Exception as exc2:  # noqa: BLE001
            raise RuntimeError(
                f"数据源注册表主文件与 last-good 副本均不可用（宁停不错采）: "
                f"主={exc!r} / 副={exc2!r}") from exc
        notify.guarded_send(
            f"数据源注册表 sources.yaml 损坏（{exc!r}），已降级沿用 last-good 副本"
            f"继续采集，请尽快修复并提交")
        return _to_sources(data)
    try:   # 成功后刷新 last-good（尽力而为，失败不影响本轮）
        last_good.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REGISTRY_PATH, last_good)
    except OSError:
        logger.warning(f"last-good 副本写入失败（不影响本轮）: {last_good}", exc_info=True)
    return _to_sources(data)


def load_sources(job: str, *, include_disabled: bool = False) -> List[Source]:
    """按 job 过滤的启用源列表（各采集 job 的唯一入口）。"""
    if job not in JOBS:
        raise ValueError(f"未知 job: {job!r}（可用: {JOBS}）")
    return [s for s in load_all()
            if s.job == job and (include_disabled or s.enabled)]


def get_source(source_id: str) -> Source:
    """按 id 取单源（CLI test 用）；不存在 KeyError。"""
    for s in load_all():
        if s.id == source_id:
            return s
    raise KeyError(f"注册表无此源: {source_id!r}")


def proxy_missing(source: Source) -> bool:
    """声明了命名代理但本节点解析不到（键未配置 / proxy_enabled=false）→ True。

    多节点让位语义（Phase A4）：无代理节点遇代理源不硬试直连（必失败徒增告警），
    跳过让给有代理能力的节点；重叠采集由 create-only 幂等兜底。调用方（job 采集
    循环）据此跳过并计 skipped，不入失败集不告警。
    """
    return bool(source.proxy_name) and not source.proxy_url


def is_enabled(source_id: str) -> bool:
    """单源 enabled 查询（单源 job 的 kill-switch）。

    **未登记视为启用**：注册表未登记的源不受注册表管辖，返回 True 保持原行为——
    避免"忘登记/注册表读失败"静默停采（宁多采不漏采，与 load_all 降级同哲学）。
    复用 get_source 的按 id 查找（KeyError 即未登记 → True），不重复扫描逻辑。
    """
    try:
        return get_source(source_id).enabled
    except KeyError:
        return True
