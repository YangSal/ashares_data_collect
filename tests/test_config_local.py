"""config.local.yaml 浅合并单测（多节点 Phase A1）。"""
import pytest

from data_collect import config as cfg


@pytest.fixture(autouse=True)
def _clean_cache():
    """测试前后清缓存：避免污染其他测试用例读到本测试的合成配置。"""
    cfg._config_cache = None
    yield
    cfg._config_cache = None


def test_shallow_merge_semantics():
    base = {"database": {"host": "a", "port": 1}, "news": {"x": 1},
            "flag": True, "lst": [1, 2]}
    over = {"database": {"host": "b"}, "flag": False, "lst": [9], "extra": {"k": 1}}
    m = cfg._shallow_merge(base, over)
    assert m["database"] == {"host": "b", "port": 1}   # dict 一层深合并
    assert m["news"] == {"x": 1}                        # 未覆盖键保留
    assert m["flag"] is False and m["lst"] == [9]       # 标量/列表整体替换
    assert m["extra"] == {"k": 1}                       # 新键加入
    assert base["database"]["host"] == "a"              # 不修改入参


def test_local_overlay_applied(tmp_path, monkeypatch):
    main = tmp_path / "config.yaml"
    main.write_text("database:\n  host: main\n  port: 5432\n"
                    "dingtalk:\n  enabled: true\n", encoding="utf-8")
    local = tmp_path / "config.local.yaml"
    local.write_text("database:\n  host: local\n"
                     "dingtalk:\n  enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("DATA_COLLECT_CONFIG", str(main))
    monkeypatch.setattr(cfg, "_LOCAL_CONFIG_PATH", local)
    assert cfg.get_db_config() == {"host": "local", "port": 5432}
    assert cfg.get_dingtalk_config()["enabled"] is False


def test_no_local_file_zero_effect(tmp_path, monkeypatch):
    main = tmp_path / "config.yaml"
    main.write_text("database:\n  host: main\n", encoding="utf-8")
    monkeypatch.setenv("DATA_COLLECT_CONFIG", str(main))
    monkeypatch.setattr(cfg, "_LOCAL_CONFIG_PATH", tmp_path / "absent.yaml")
    assert cfg.get_db_config() == {"host": "main"}


def test_empty_local_file_zero_effect(tmp_path, monkeypatch):
    main = tmp_path / "config.yaml"
    main.write_text("database:\n  host: main\n", encoding="utf-8")
    local = tmp_path / "config.local.yaml"
    local.write_text("", encoding="utf-8")
    monkeypatch.setenv("DATA_COLLECT_CONFIG", str(main))
    monkeypatch.setattr(cfg, "_LOCAL_CONFIG_PATH", local)
    assert cfg.get_db_config() == {"host": "main"}
