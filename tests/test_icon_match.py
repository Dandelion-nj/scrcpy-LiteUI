"""图标匹配：多级容错规则逐条覆盖。

图标素材库（icons/）不入库，CI 上没有这个目录，所以这里一律自己构造 _IconIndex，
不依赖真实素材库，也不碰网络。
"""

import time
import zipfile

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
# 从手机取图标：设备一连上就自动跑一遍（推 dex → 用 app_process 在手机上跑一遍 →
# 把图标包拉回来入库）；包里的条目就是「包名.png」，包名直接当索引键。收回来后要排在
# 预置素材库前面（用户原话：传回后优先用手机传回的图片）。全程不发真 adb 命令。

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


class _FakeSyncAdb:
    """假 adb：只认 推 dex / 跑 app_process / 拉图标包 三步，每步都做成可调状态。"""

    def __init__(self, names=("com.tencent.mm.png",), dump_code=0, dump_out=None,
                 pull_code=0):
        self.names = list(names)
        self.dump_code = dump_code
        self.dump_out = "[icondump] 完成：导出 1 个，跳过 0 个" if dump_out is None else dump_out
        self.pull_code = pull_code
        self.calls = []

    def __call__(self, args, timeout=8, serial=None):
        self.calls.append(args)
        if args[:1] == ["push"]:
            return "1 file pushed.\n", "", 0
        if args[:1] == ["pull"]:
            if self.pull_code == 0:
                with zipfile.ZipFile(args[2], "w") as zf:
                    for n in self.names:
                        zf.writestr(n, b"BAD-DATA" if "bad" in n else b"\x89PNG\r\n\x1a\n")
            return "", ("" if self.pull_code == 0 else "error: closed"), self.pull_code
        if len(args) > 1 and args[0] == "shell" and args[1].startswith("CLASSPATH="):
            return self.dump_out, "", self.dump_code
        return "", "", 0


def _patch_synced(monkeypatch):
    """把「这台设备取过图标了」的持久记录换成内存版。

    否则测试会读写真入口脚本的数据流，还会为拿设备键真去跑 adb getprop。
    """
    state = set()
    monkeypatch.setattr(apps, "_synced_keys", lambda: set(state))

    def save(keys):
        state.clear()
        state.update(keys)

    monkeypatch.setattr(apps, "_save_synced_keys", save)
    monkeypatch.setattr(apps, "_sync_key", lambda serial: serial or "")
    monkeypatch.setattr(apps, "_icon_synced", set())
    return state


def _patch_sync(monkeypatch, written, rebuilt, **kw):
    adb = _FakeSyncAdb(**kw)
    monkeypatch.setattr(apps, "run_adb", adb)
    monkeypatch.setattr(apps, "get_devices", lambda: ["SN-1"])
    # 取图 dex 只查「在不在」，随便指个真实存在的文件即可
    monkeypatch.setattr(apps, "_ICON_DEX_FILE", apps.__file__)
    monkeypatch.setattr(apps, "_icon_bytes_to_webp",
                        lambda raw, size=256: None if raw.startswith(b"BAD") else b"w")
    monkeypatch.setattr(apps, "_write_icon",
                        lambda pkg, data: written.append(pkg) or ("/x/%s.webp" % pkg))
    monkeypatch.setattr(apps, "_icon_index_build", lambda: rebuilt.append(1))
    _patch_synced(monkeypatch)
    return adb


def test_sync_icons_ingests_and_reports(monkeypatch):
    """包名当索引键入库；认不出包名的条目、图片不合格的都算跳过。"""
    written, rebuilt = [], []
    adb = _patch_sync(monkeypatch, written, rebuilt,
                      names=["com.tencent.mm.png", "bad.com.foo.png", "说明.txt"])
    r = apps.sync_icons_from_device("SN-1")
    assert r["ok"] is True and r["imported"] == 1 and r["skipped"] == 2
    assert written == ["com.tencent.mm"]
    assert rebuilt == [1]
    assert adb.calls[0][:1] == ["push"]          # 第一步就是把 dex 推上去


def test_sync_icons_runs_the_dumped_dex(monkeypatch):
    """手机上跑的是 app_process + dex：用 CLASSPATH 指到推上去的 dex，跑我们的主类。"""
    written, rebuilt = [], []
    adb = _patch_sync(monkeypatch, written, rebuilt)
    assert apps.sync_icons_from_device("SN-1")["ok"] is True
    assert ["push", apps.__file__, apps._ICON_DEX_REMOTE] in adb.calls
    runs = [a for a in adb.calls
            if len(a) > 1 and a[0] == "shell" and a[1].startswith("CLASSPATH=")]
    assert runs == [["shell", "CLASSPATH=" + apps._ICON_DEX_REMOTE, "app_process",
                     "/system/bin", apps._ICON_DUMP_CLASS, apps._ICON_ZIP_REMOTE]]


def test_sync_icons_cleans_up_the_device(monkeypatch):
    """dex 与图标包用完就删：手机上不留任何东西（这也是换掉装 App 那条路的意义）。"""
    written, rebuilt = [], []
    adb = _patch_sync(monkeypatch, written, rebuilt)
    apps.sync_icons_from_device("SN-1")
    assert ["shell", "rm", "-f", apps._ICON_DEX_REMOTE, apps._ICON_ZIP_REMOTE] in adb.calls


def test_sync_icons_reports_dump_failure(monkeypatch):
    """app_process 非零退出（比如被 ROM 杀掉）时如实报错，别当成功。"""
    written, rebuilt = [], []
    _patch_sync(monkeypatch, written, rebuilt, dump_code=137, dump_out="[icondump] 失败：…")
    r = apps.sync_icons_from_device("SN-1")
    assert r["ok"] is False and "导出图标失败" in r["msg"]
    assert written == []


def test_sync_icons_reports_missing_dex(monkeypatch):
    """打包漏了 dex 时要直说，别让用户对着「拉取失败」瞎猜。"""
    written, rebuilt = [], []
    adb = _patch_sync(monkeypatch, written, rebuilt)
    monkeypatch.setattr(apps, "_ICON_DEX_FILE", "no-such-icondump.dex")
    r = apps.sync_icons_from_device("SN-1")
    assert r["ok"] is False and "取图程序不在" in r["msg"]
    assert adb.calls == []


def test_sync_icons_reports_push_failure(monkeypatch):
    written, rebuilt = [], []
    adb = _patch_sync(monkeypatch, written, rebuilt)
    monkeypatch.setattr(apps, "run_adb",
                        lambda args, timeout=8, serial=None:
                        ("", "adb: error: failed to copy", 1)
                        if args[:1] == ["push"] else adb(args, timeout, serial))
    r = apps.sync_icons_from_device("SN-1")
    assert r["ok"] is False and "推送取图程序失败" in r["msg"]
    assert written == []


def test_sync_icons_reports_pull_failure(monkeypatch):
    written, rebuilt = [], []
    _patch_sync(monkeypatch, written, rebuilt, pull_code=1)
    r = apps.sync_icons_from_device("SN-1")
    assert r["ok"] is False and "拉取图标失败" in r["msg"]
    assert written == []


def test_sync_icons_without_device(monkeypatch):
    written, rebuilt = [], []
    _patch_sync(monkeypatch, written, rebuilt)
    monkeypatch.setattr(apps, "get_devices", lambda: [])
    r = apps.sync_icons_from_device(None)
    assert r["ok"] is False and "没有已连接的设备" in r["msg"]


def test_sync_icons_marks_device_in_ads(monkeypatch):
    """取成功就把这台设备记进数据流：下次连上不必重取。"""
    written, rebuilt = [], []
    _patch_sync(monkeypatch, written, rebuilt)
    assert apps.sync_icons_from_device("SN-1")["ok"] is True
    assert apps._synced_keys() == {"SN-1"}


def test_sync_icons_failure_keeps_device_unmarked(monkeypatch):
    """这一轮没成（比如导出超时）就别登记，下次连上还得再试。"""
    written, rebuilt = [], []
    _patch_sync(monkeypatch, written, rebuilt, pull_code=1)
    assert apps.sync_icons_from_device("SN-1")["ok"] is False
    assert apps._synced_keys() == set()


def test_sync_key_carries_the_icon_format(monkeypatch):
    """记录键带上取图格式版本：dex 换了渲染方式后老记录自动作废，设备下次连上重取一份。"""
    monkeypatch.setattr(apps, "device_key", lambda serial: "V2324A")
    assert apps._sync_key("172.19.163.3:5555") == apps._ICON_SYNC_FORMAT + ":V2324A"


def test_sync_key_empty_without_device(monkeypatch):
    monkeypatch.setattr(apps, "device_key", lambda serial: "")
    assert apps._sync_key("") == ""


def test_synced_keys_drops_legacy_entries(monkeypatch):
    """上一版留下的、不带格式前缀的老键不再算数，读的时候顺手丢掉。"""
    monkeypatch.setattr(apps, "_synced_cache", None)
    monkeypatch.setattr(apps, "storage_read",
                        lambda stream, binary=False: '["V2324A", "%s:V2324A"]'
                        % apps._ICON_SYNC_FORMAT)
    assert apps._synced_keys() == {apps._ICON_SYNC_FORMAT + ":V2324A"}


def test_auto_sync_runs_once_per_device(monkeypatch):
    """状态接口每 3 秒轮询一次，不能每轮都去装一遍、拉一遍。"""
    runs = []
    monkeypatch.setattr(apps, "sync_icons_from_device",
                        lambda serial=None: runs.append(serial) or {"ok": True})
    _patch_synced(monkeypatch)
    for _ in range(5):
        apps.auto_sync_icons("SN-1")
    deadline = time.time() + 5
    while len(runs) < 1 and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    assert runs == ["SN-1"]


def test_auto_sync_skips_device_already_taken(monkeypatch):
    """这台之前取过（记录在数据流里，重开程序也认）：连上就直接用，不再重取。"""
    runs = []
    monkeypatch.setattr(apps, "sync_icons_from_device",
                        lambda serial=None: runs.append(serial) or {"ok": True})
    state = _patch_synced(monkeypatch)
    state.add("SN-1")
    apps.auto_sync_icons("SN-1")
    time.sleep(0.15)
    assert runs == []


def test_auto_sync_skips_after_disconnect(monkeypatch):
    """掉线再连上不重取：图标已经在数据流里，不必因为掉了一次线再来一遍。"""
    runs = []
    monkeypatch.setattr(apps, "sync_icons_from_device",
                        lambda serial=None: runs.append(serial) or {"ok": True})
    state = _patch_synced(monkeypatch)
    state.add("SN-1")
    apps.auto_sync_icons("SN-1")
    time.sleep(0.15)
    apps.forget_icon_sync("SN-1")         # 掉线
    apps.auto_sync_icons("SN-1")          # 又连上
    time.sleep(0.15)
    assert runs == []


def test_resync_icons_forgets_then_takes_again(monkeypatch):
    """首页点「刷新」＝用户明确要求重来：抹掉记录再取一遍。"""
    runs = []
    monkeypatch.setattr(apps, "sync_icons_from_device",
                        lambda serial=None: runs.append(serial) or {"ok": True})
    state = _patch_synced(monkeypatch)
    state.add("SN-1")
    apps.resync_icons("SN-1")
    deadline = time.time() + 5
    while not runs and time.time() < deadline:
        time.sleep(0.01)
    assert runs == ["SN-1"]
    assert "SN-1" not in state           # 记录先抹掉，取成功后再由 sync 自己登记


def test_resync_icons_without_device(monkeypatch):
    monkeypatch.setattr(apps, "get_devices", lambda: [])
    monkeypatch.setattr(apps, "sync_icons_from_device",
                        lambda serial=None: {"ok": True})
    _patch_synced(monkeypatch)
    apps.resync_icons(None)              # 没设备：安静收工，不炸
    time.sleep(0.05)


def test_auto_sync_failure_is_retried_next_time(monkeypatch):
    """这一轮没成（比如手机没解锁）就别记成「取过了」，下次连上再试。"""
    monkeypatch.setattr(apps, "sync_icons_from_device",
                        lambda serial=None: {"ok": False, "msg": "手机导出超时"})
    monkeypatch.setattr(apps, "_log_scan_failure", lambda msg: None)
    _patch_synced(monkeypatch)
    apps.auto_sync_icons("SN-1")
    deadline = time.time() + 5
    while "SN-1" in apps._icon_synced and time.time() < deadline:
        time.sleep(0.01)
    assert "SN-1" not in apps._icon_synced


def test_successful_sync_bumps_icon_rev(monkeypatch):
    """取回新图标要让图标库版本 +1，界面据此重新取图。"""
    written, rebuilt = [], []
    _patch_sync(monkeypatch, written, rebuilt)
    before = apps.icon_rev()
    assert apps.sync_icons_from_device("SN-1")["ok"] is True
    assert apps.icon_rev() == before + 1


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

