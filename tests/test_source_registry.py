"""source_registry 单测：schema 校验 / job 过滤 / 命名解析 / last-good 降级。"""

import pytest

from data_collect.utils import source_registry as sr

_VALID = """\
version: 1
defaults:
  enabled: true
sources:
  - id: fed
    adapter: rss
    channel: us_policy
    job: news_us
    url: https://example.com/feed.xml
    proxy: us
    headers: browser
  - id: caixin
    adapter: rsshub
    channel: media
    job: news_regulator
    route: /caixin/latest
    health_profile: continuous
    stale_hours: 12
  - id: csrc
    adapter: listpage
    channel: policy
    job: news_regulator
    enabled: false
    note: 测试停用
"""


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "sources.yaml"
    path.write_text(_VALID, encoding="utf-8")
    monkeypatch.setattr(sr, "REGISTRY_PATH", path)
    monkeypatch.setattr(sr, "_spool_dir", lambda: tmp_path / "spool")
    monkeypatch.setattr(sr, "get_news_config",
                        lambda: {"us_proxy": "http://127.0.0.1:7890"})
    return path


# ---------- 加载与过滤 ----------

def test_load_sources_filters_job_and_enabled(registry):
    us = sr.load_sources("news_us")
    assert [s.id for s in us] == ["fed"]
    assert us[0].proxy_url == "http://127.0.0.1:7890"     # 命名代理已解析
    assert us[0].headers["User-Agent"].startswith("Mozilla")  # 命名 header 集已解析
    reg = sr.load_sources("news_regulator")
    assert [s.id for s in reg] == ["caixin"]              # disabled 默认排除
    all_reg = sr.load_sources("news_regulator", include_disabled=True)
    assert [s.id for s in all_reg] == ["caixin", "csrc"]


def test_unknown_job_raises(registry):
    with pytest.raises(ValueError, match="未知 job"):
        sr.load_sources("nope")


def test_get_source(registry):
    assert sr.get_source("caixin").route == "/caixin/latest"
    with pytest.raises(KeyError):
        sr.get_source("nope")


def test_health_metadata_loads(registry):
    caixin = sr.get_source("caixin")
    assert caixin.health_profile == "continuous"
    assert caixin.stale_hours == 12.0
    assert sr.get_source("fed").health_profile == ""
    assert sr.get_source("fed").stale_hours is None


def test_is_enabled(registry):
    assert sr.is_enabled("fed") is True            # 启用源
    assert sr.is_enabled("csrc") is False           # enabled: false
    assert sr.is_enabled("nonexistent") is True     # 未登记=不管辖=保持启用（向后兼容）


# ---------- 代理让位（Phase A4） ----------

def test_proxy_missing_three_branches(registry, monkeypatch):
    fed = sr.get_source("fed")
    assert fed.proxy_name == "us"
    assert sr.proxy_missing(fed) is False               # 声明+解析到 → 不缺
    assert sr.proxy_missing(sr.get_source("caixin")) is False   # 未声明 → 不缺
    monkeypatch.setattr(sr, "get_news_config", lambda: {})      # 本节点无 us_proxy
    fed2 = sr.get_source("fed")
    assert fed2.proxy_url == "" and sr.proxy_missing(fed2) is True   # 声明+解析不到 → 让位


def test_proxy_enabled_false_blanks_all(registry, monkeypatch):
    """news.proxy_enabled: false 应急总开关 → 命名代理全部解析为空（让位语义生效）。"""
    monkeypatch.setattr(sr, "get_news_config",
                        lambda: {"us_proxy": "http://127.0.0.1:7890",
                                 "proxy_enabled": False})
    fed = sr.get_source("fed")
    assert fed.proxy_url == "" and sr.proxy_missing(fed) is True


# ---------- schema 校验（fail-fast 逐规则） ----------

def _errors_of(tmp_path, text):
    p = tmp_path / "bad.yaml"
    p.write_text(text, encoding="utf-8")
    return sr.validate_registry(p)


def test_validate_ok(registry):
    assert sr.validate_registry(registry) == []


@pytest.mark.parametrize("mutation, fragment", [
    ("version: 1", "version: 2"),                        # 版本不符
    ("adapter: rss", "adapter: magic"),                  # 未知 adapter
    ("channel: us_policy", "channel: gossip"),           # 未知 channel
    ("job: news_us", "job: news_nope"),                  # 未知 job
    ("proxy: us", "proxy: mars"),                        # 未知代理名
    ("headers: browser", "headers: alien"),              # 未知 header 集
    ("    url: https://example.com/feed.xml\n", ""),     # rss 缺 url
    ("    route: /caixin/latest\n", ""),                 # rsshub 缺 route
    ("id: caixin", "id: fed"),                           # id 重复
    ("id: csrc", "id: CSRC-1"),                          # id 含非法字符
    ("note: 测试停用", "banana: 1"),                     # 未知源级键
    ("health_profile: continuous", "health_profile: hourlyish"),
    ("stale_hours: 12", "stale_hours: 0"),
    ("stale_hours: 12", "stale_hours: nope"),
])
def test_validate_rejects(tmp_path, mutation, fragment):
    assert _errors_of(tmp_path, _VALID.replace(mutation, fragment)) != []


def test_validate_unreadable(tmp_path):
    assert sr.validate_registry(tmp_path / "absent.yaml") != []


# ---------- last-good 降级 ----------

def test_last_good_written_on_success(registry, tmp_path):
    sr.load_all()
    assert (tmp_path / "spool" / ".sources_last_good.yaml").exists()


def test_corrupt_falls_back_to_last_good(registry, tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(sr.notify, "guarded_send",
                        lambda msg, **kw: alerts.append(msg) or True)
    sr.load_all()                                        # 先产出 last-good
    registry.write_text("version: [broken", encoding="utf-8")
    sources = sr.load_all()                              # 降级沿用 last-good
    assert [s.id for s in sources] == ["fed", "caixin", "csrc"]
    assert len(alerts) == 1 and "last-good" in alerts[0]


def test_both_broken_raises(registry, tmp_path):
    registry.write_text("version: [broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="均不可用"):
        sr.load_all()                                    # 无 last-good（未成功加载过）
