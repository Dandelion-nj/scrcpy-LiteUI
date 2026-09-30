"""图标匹配：多级容错规则逐条覆盖。

图标素材库（icons/）不入库，CI 上没有这个目录，所以这里一律自己构造 _IconIndex，
不依赖真实素材库，也不碰网络。
"""

from pathlib import Path

import pytest

from kuaitou import apps


@pytest.fixture
def idx():
    """迷你图标库：键覆盖真实素材库里常见的几种命名差异。"""
    index = apps._IconIndex()
    for key in ("com.tencent.mm", "cn.kuwo.player", "com.google.android.chrome",
                "com.example.app.plus", "com.miHoYo.Yuanshen", "com.foo.bar",
                "cn.wps.moffice_eng"):
        index.add(key, "/icons/%s.webp" % key)
    return index


def test_exact_hit(idx):
    assert idx.find("com.tencent.mm") == "/icons/com.tencent.mm.webp"


@pytest.mark.parametrize("pkg", ["", "   ", None])
def test_empty_package(idx, pkg):
    assert idx.find(pkg) is None


def test_case_insensitive(idx):
    # 库键带大写（com.miHoYo.Yuanshen），设备端可能全小写
    assert idx.find("com.mihoyo.yuanshen") == "/icons/com.miHoYo.Yuanshen.webp"


def test_separator_insensitive(idx):
    # 只差分隔符：归一化后与库键一致
    assert idx.find("com-tencent-mm") == "/icons/com.tencent.mm.webp"


def test_library_key_is_prefix_of_device(idx):
    # 设备包名比库键多一段
    assert idx.find("com.tencent.mm.plugin") == "/icons/com.tencent.mm.webp"


def test_library_key_extends_device(idx):
    # 库键比设备包名多一段（设备上只装基础版，库里是 plus 版）
    assert idx.find("com.example.app") == "/icons/com.example.app.plus.webp"


def test_tail_segments_match(idx):
    # 末两段相同：设备 com.kuwo.player 对库 cn.kuwo.player
    assert idx.find("com.kuwo.player") == "/icons/cn.kuwo.player.webp"


def test_tail_segments_prefer_longer_common_suffix():
    """末两段相同时，公共后缀越长越可能是同一个应用。"""
    index = apps._IconIndex()
    index.add("tv.kuwo.player", "/icons/tv.webp")
    index.add("com.kuwo.player", "/icons/com.webp")
    assert index.find("zz.com.kuwo.player") == "/icons/com.webp"


def test_library_key_is_suffix_of_device():
    # 设备包名前面少了几段（库键以设备包名结尾）
    index = apps._IconIndex()
    index.add("com.google.android.chrome", "/icons/chrome.webp")
    assert index.find("android.chrome") == "/icons/chrome.webp"


def test_variant_suffix_stripped(idx):
    # com.kuwo.player.pro 常规链查不到，剥掉 pro 后按末两段重合命中
    assert idx.find("com.kuwo.player.pro") == "/icons/cn.kuwo.player.webp"


def test_variant_suffix_not_stripped_below_two_segments():
    index = apps._IconIndex()
    index.add("com.foo", "/icons/foo.webp")
    # 只剩一段时不再剥：宁可不匹配，也不要指到毫不相干的图标
    assert index.find("com.app.lite") is None


def test_fuzzy_fallback(idx):
    # 换皮包名只差个别字母
    assert idx.find("com.foo.bars") == "/icons/com.foo.bar.webp"


def test_no_match_returns_none(idx):
    assert idx.find("org.unknown.thing") is None


def test_captured_icon_wins_over_library():
    """同一键重复登记保留先到的：运行期抓到的图标优先于内置素材库。"""
    index = apps._IconIndex()
    index.add("com.foo", "/captured/com.foo.webp")
    index.add("com.foo", "/icons/com.foo.webp")
    assert index.find("com.foo") == "/captured/com.foo.webp"


def test_index_grows_after_add():
    """运行期新抓的图标登记后要能被后续请求命中（含模糊表快照失效）。"""
    index = apps._IconIndex()
    index.add("com.aaa.bbb", "/icons/a.webp")
    index.find("com.ccc.ddd")                 # 先跑一次，让模糊表快照建立
    index.add("com.eee.fff", "/icons/b.webp")
    assert index.find("com.eee.fff.g") == "/icons/b.webp"


def test_find_cached_icon_uses_global_index(monkeypatch, idx):
    monkeypatch.setattr(apps, "_icon_index", idx)
    assert apps.find_cached_icon("com.tencent.mm.plugin") == "/icons/com.tencent.mm.webp"


def test_alias_by_exact_name(monkeypatch, idx):
    monkeypatch.setattr(apps, "_icon_index", idx)
    assert apps._alias_icon("微信") == "/icons/com.tencent.mm.webp"


def test_alias_by_normalized_name(monkeypatch, idx):
    """名字的写法差异（空格 / 大小写 / 标点）也能命中。"""
    monkeypatch.setattr(apps, "_icon_index", idx)
    assert apps._alias_icon("wps   office") == "/icons/cn.wps.moffice_eng.webp"


def test_alias_by_fuzzy_ascii_name(monkeypatch, idx):
    """英文名只差个别字母时兜底命中。"""
    monkeypatch.setattr(apps, "_icon_index", idx)
    assert apps._alias_icon("Chromee") == "/icons/com.google.android.chrome.webp"


def test_alias_no_fuzzy_for_short_or_chinese_names(monkeypatch, idx):
    """短名与中文名不做模糊匹配：差一个字往往就是另一个应用。"""
    monkeypatch.setattr(apps, "_icon_index", idx)
    assert apps._alias_icon("Chrm") is None
    assert apps._alias_icon("微信读") is None


@pytest.mark.parametrize("pkg", ["", None])
def test_alias_empty_name(monkeypatch, idx, pkg):
    monkeypatch.setattr(apps, "_icon_index", idx)
    assert apps._alias_icon(pkg) is None


# ---------- 名字版本变体兜底 ----------

@pytest.mark.parametrize("name", ["微信HD", "微信 hd", "微信极速版", "微信 国际版"])
def test_alias_strips_name_variant_suffix(monkeypatch, idx, name):
    """名字带版本后缀（HD / 极速版 / 国际版）时，剥掉后缀再查别名表。"""
    monkeypatch.setattr(apps, "_icon_index", idx)
    assert apps._alias_icon(name) == "/icons/com.tencent.mm.webp"


def test_name_variants_keeps_original_first():
    assert apps._name_variants("微信hd") == ["微信hd", "微信"]


def test_name_variants_never_strips_below_two_chars():
    """剩下的主名不足两个字就不再剥，避免把 "HD" 剥成空串乱匹配。"""
    assert apps._name_variants("hd") == ["hd"]
    assert apps._name_variants("tv") == ["tv"]


def test_name_variants_leaves_plain_name_untouched():
    assert apps._name_variants("telegram") == ["telegram"]


def test_alias_overseas_name(monkeypatch):
    """海外应用按英文名兜底：库里只有包名，名字全对不上也不该漏。"""
    index = apps._IconIndex()
    index.add("org.telegram.messenger", "/icons/telegram.webp")
    index.add("com.openai.chatgpt", "/icons/chatgpt.webp")
    monkeypatch.setattr(apps, "_icon_index", index)
    assert apps._alias_icon("Telegram") == "/icons/telegram.webp"
    assert apps._alias_icon("ChatGPT") == "/icons/chatgpt.webp"


# ---------- 从手机导入图标 ----------
# 手机上取图工具导出的图标收回电脑：文件名优先当包名，其次当应用名；
# 收回来后要排在预置素材库前面（用户原话：传回后优先用手机传回的图片）。

def _img(p):
    """铺一个假图片文件：导入流程只看文件名与字节，内容无所谓。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" if "bad" not in p.name else b"BAD-DATA")
    return p


def test_looks_like_pkg():
    assert apps._looks_like_pkg("com.tencent.mm")
    assert apps._looks_like_pkg("cn.kuwo.player")
    assert not apps._looks_like_pkg("微信")
    assert not apps._looks_like_pkg("wechat")


def test_ingest_icon_file_prefers_package_name(tmp_path):
    f = _img(tmp_path / "com.tencent.mm.png")
    assert apps._ingest_icon_file(str(f), {}) == "com.tencent.mm"


def test_ingest_icon_file_falls_back_to_app_name(tmp_path, monkeypatch):
    monkeypatch.setattr(apps, "_apps_cache",
                        {"SN": [{"name": "微信", "package": "com.tencent.mm"}]})
    table = apps._pkg_by_app_name()
    assert apps._ingest_icon_file(str(_img(tmp_path / "微信.png")), table) == "com.tencent.mm"
    assert apps._ingest_icon_file(str(_img(tmp_path / "认不出来.png")), table) is None


class _FakeImportAdb:
    """假 adb：ls 给一份名单，pull 就地铺出同名文件。"""

    def __init__(self, names, ls_out=None, pull_code=0):
        self.names = names
        self.ls_out = ls_out if ls_out is not None else "\n".join(names)
        self.pull_code = pull_code
        self.calls = []

    def __call__(self, args, timeout=8, serial=None):
        self.calls.append(args)
        if args[:3] == ["shell", "ls", "-1"]:
            return self.ls_out, "", 0
        if args[0] == "pull":
            for n in self.names:
                _img(Path(args[2]) / "icons" / n)
            return "", ("" if self.pull_code == 0 else "error: closed"), self.pull_code
        return "", "", 0


def _patch_import(monkeypatch, names, written, rebuilt, **kw):
    adb = _FakeImportAdb(names, **kw)
    monkeypatch.setattr(apps, "run_adb", adb)
    monkeypatch.setattr(apps, "load_config", lambda: {"icon_import_dir": "/sdcard/ic"})
    monkeypatch.setattr(apps, "get_devices", lambda: ["SN-1"])
    monkeypatch.setattr(apps, "_apps_cache",
                        {"SN-1": [{"name": "微信", "package": "com.tencent.mm"}]})
    monkeypatch.setattr(apps, "_icon_bytes_to_webp",
                        lambda raw, size=256: None if raw.startswith(b"BAD") else b"w")
    monkeypatch.setattr(apps, "_write_icon",
                        lambda pkg, data: written.append(pkg) or ("/x/%s.webp" % pkg))
    monkeypatch.setattr(apps, "_icon_index_build", lambda: rebuilt.append(1))
    return adb


def test_import_icons_ingests_and_reports(monkeypatch):
    """包名文件名 + 应用名文件名都能入库；认不出的、图片不合格的算跳过。"""
    written, rebuilt = [], []
    _patch_import(monkeypatch,
                  ["com.tencent.mm.png", "微信.png", "bad.com.foo.png", "说明.txt"],
                  written, rebuilt)
    r = apps.import_icons_from_device("SN-1")
    assert r["ok"] is True and r["imported"] == 2 and r["skipped"] == 1
    assert written == ["com.tencent.mm", "com.tencent.mm"]
    assert rebuilt == [1]                     # 入库后重建索引，导入的图标才会排到前面
    assert "已导入 2 个图标" in r["msg"]


def test_import_icons_rejects_dir_without_images(monkeypatch):
    written, rebuilt = [], []
    _patch_import(monkeypatch, ["a.txt"], written, rebuilt)
    r = apps.import_icons_from_device("SN-1")
    assert r["ok"] is False and "没有图片" in r["msg"]
    assert written == [] and rebuilt == []


def test_import_icons_reports_missing_dir(monkeypatch):
    written, rebuilt = [], []
    _patch_import(monkeypatch, [], written, rebuilt,
                  ls_out="ls: /sdcard/ic: No such file or directory")
    r = apps.import_icons_from_device("SN-1")
    assert r["ok"] is False and "找不到" in r["msg"]


def test_import_icons_reports_pull_failure(monkeypatch):
    written, rebuilt = [], []
    _patch_import(monkeypatch, ["com.a.b.png"], written, rebuilt, pull_code=1)
    r = apps.import_icons_from_device("SN-1")
    assert r["ok"] is False and "拉取图标失败" in r["msg"]
    assert written == []


def test_import_icons_without_device(monkeypatch):
    written, rebuilt = [], []
    _patch_import(monkeypatch, [], written, rebuilt)
    monkeypatch.setattr(apps, "get_devices", lambda: [])
    r = apps.import_icons_from_device(None)
    assert r["ok"] is False and "没有已连接的设备" in r["msg"]


def test_imported_icon_wins_over_bundled(tmp_path, monkeypatch):
    """索引重建后，手机传回的图标必须排在预置素材库前面。"""
    from_phone = _img(tmp_path / "phone" / "icon_com.foo.bar.webp")
    bundled = tmp_path / "bundled"
    _img(bundled / "com.foo.bar.webp")
    monkeypatch.setattr(apps, "_icon_ads_keys", lambda: ["com.foo.bar"])
    monkeypatch.setattr(apps, "ads_path", lambda stream: str(from_phone))
    monkeypatch.setattr(apps, "ICON_SEARCH_DIRS", [str(bundled)])
    monkeypatch.setattr(apps, "_icon_index", apps._icon_index)   # 会换掉全局索引，跑完还原
    apps._icon_index_build()
    assert apps.find_cached_icon("com.foo.bar") == str(from_phone)


def test_launch_icon_tool_needs_package(monkeypatch):
    monkeypatch.setattr(apps, "load_config", lambda: {"icon_tool_package": ""})
    r = apps.launch_icon_tool(serial="SN-1")
    assert r["ok"] is False and "包名" in r["msg"]


def test_launch_icon_tool_reports_missing_app(monkeypatch):
    monkeypatch.setattr(apps, "load_config", lambda: {"icon_tool_package": "com.x.y"})
    monkeypatch.setattr(apps, "run_adb",
                        lambda args, timeout=8, serial=None:
                        ("", "** No activities found to run, monkey aborted.", 1))
    r = apps.launch_icon_tool(serial="SN-1")
    assert r["ok"] is False and "没能打开" in r["msg"]
