"""存储层与纯解析函数。

不读写磁盘 / 数据流：路径解析与配置合并都是纯逻辑，直接调函数断言。
"""

import json
import os
import sys

from kuaitou import storage
from kuaitou.apps import _parse_apps_output

# ---------- 路径解析 ----------

def test_resolve_paths_source_mode(monkeypatch):
    """源码态：资源、数据都在项目根目录；ADS 宿主是入口脚本。"""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    res, data, exe = storage._resolve_paths()
    root = storage.RES_DIR
    assert res == root and data == root
    assert exe.endswith("launcher_server.py")


def test_resolve_paths_frozen_mode(monkeypatch):
    """打包态：资源在解包临时目录，数据与宿主在 exe 目录。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "C:/pkg_res", raising=False)
    monkeypatch.setattr(sys, "executable", "C:/exe_dir/快投.exe", raising=False)
    res, data, exe = storage._resolve_paths()
    assert res == "C:/pkg_res"
    assert data == os.path.dirname("C:/exe_dir/快投.exe")
    assert exe == os.path.abspath("C:/exe_dir/快投.exe")


def test_default_config_shape():
    """内置默认配置的结构保持不变（漏字段 = 界面直接拿不到值）。"""
    cfg = storage.DEFAULT_CONFIG
    for key in ("ip", "port", "res_w", "res_h", "bitrate", "scale", "fps",
                "audio_mode", "minimize_to_tray", "autostart", "reconnect_enabled",
                "recent_devices", "quick_launch"):
        assert key in cfg
    assert cfg["port"] == "5555"
    assert cfg["audio_mode"] == "both"
    assert cfg["ip"] == ""          # 内置默认不带具体设备地址


def test_shipped_config_matches_code_defaults():
    """随包内置的 config.json 与代码里的 DEFAULT_CONFIG 不许再打架。

    两份都定义过的键必须一致：否则内置文件会静默盖掉代码里的默认值，
    排查问题时看到的"默认值"与实际生效的不是同一个。
    """
    with open(os.path.join(storage.RES_DIR, "config.json"), encoding="utf-8") as f:
        shipped = json.load(f)
    for key, value in shipped.items():
        assert key in storage.DEFAULT_CONFIG, "内置配置里有代码中不存在的键: %s" % key
        assert storage.DEFAULT_CONFIG[key] == value, "内置配置与代码默认值不一致: %s" % key


# ---------- 配置合并 ----------

def test_merge_user_config_overrides_defaults(monkeypatch):
    """用户配置覆盖内置默认值；没写的键保留默认。"""
    monkeypatch.setattr(storage, "storage_read", lambda *a, **k: '{"port": "9999"}')
    merged = storage.load_config()
    assert merged["port"] == "9999"
    assert merged["fps"] == "60"
    assert merged["audio_mode"] == "both"


def test_merge_corrupt_config_falls_back(monkeypatch):
    """用户配置损坏时回落到内置默认值，不抛异常。"""
    monkeypatch.setattr(storage, "storage_read", lambda *a, **k: "{not json")
    merged = storage.load_config()
    assert merged["port"] == "5555"


def test_merge_no_user_config_uses_builtin(monkeypatch, tmp_path):
    """没有用户配置时读随包内置 config.json（不存在则用代码默认值）。"""
    monkeypatch.setattr(storage, "storage_read", lambda *a, **k: None)
    monkeypatch.setattr(storage, "BUILTIN_CONFIG_FILE",
                        str(tmp_path / "missing.json"))
    merged = storage.load_config()
    assert merged["port"] == "5555"
    assert merged["audio_mode"] == "both"


def test_merge_legacy_quick_launch_list(monkeypatch):
    """旧版快捷启动是列表（全局共用），合并后要统一成 {设备: [...]}。"""
    monkeypatch.setattr(storage, "storage_read",
                        lambda *a, **k: '{"quick_launch": ["a.b", "c.d"]}')
    merged = storage.load_config()
    assert merged["quick_launch"] == {"*": ["a.b", "c.d"]}


# ---------- scrcpy --list-apps 输出解析 ----------

def test_parse_apps_output_normal():
    out = (" - 应用名    com.foo.bar\n"
           " * 微信    com.tencent.mm\n"
           " - 没有两个空格的脏行\n")
    apps = _parse_apps_output(out)
    assert apps == [
        {"name": "应用名", "package": "com.foo.bar"},
        {"name": "微信", "package": "com.tencent.mm"},
    ]


def test_parse_apps_output_ignores_duplicates_and_invalid():
    out = (" - 微信    com.tencent.mm\n"
           " - 微信2   com.tencent.mm\n"
           " - bad     not.a.valid-pkg!\n")
    apps = _parse_apps_output(out)
    assert apps == [{"name": "微信", "package": "com.tencent.mm"}]


def test_parse_apps_output_sorted_by_name():
    out = (" - B    com.b\n"
           " - a    com.a\n")
    assert [a["name"] for a in _parse_apps_output(out)] == ["a", "B"]
