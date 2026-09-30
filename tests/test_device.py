"""设备与 adb 调用：状态分组、序列号解析、多设备参数选择。

adb 一律不打真的：把 run_adb 换成返回固定输出的假函数，只验证解析与选择逻辑。
"""

import threading
import time

import pytest

from kuaitou import device

ADB_DEVICES_OUT = """List of devices attached
emulator-5554\tdevice
192.168.1.20:5555\tdevice
V2324A\tunauthorized
172.19.163.3:5555\toffline

"""


def _fake_adb(out):
    def run(args, timeout=8, serial=None):
        return out, "", 0
    return run


def test_device_states_classifies_all_three(monkeypatch):
    """三态判定：在线 / 等待授权 / 离线，不能混为一谈。"""
    monkeypatch.setattr(device, "run_adb", _fake_adb(ADB_DEVICES_OUT))
    states = device.device_states()
    assert states["device"] == ["emulator-5554", "192.168.1.20:5555"]
    assert states["unauthorized"] == ["V2324A"]
    assert states["offline"] == ["172.19.163.3:5555"]


def test_device_states_ignores_blank_and_garbage(monkeypatch):
    monkeypatch.setattr(device, "run_adb", _fake_adb("List of devices attached\n\nsome noise\n"))
    assert device.device_states() == {"device": [], "unauthorized": [], "offline": [], "other": []}


def test_get_devices_only_online(monkeypatch):
    monkeypatch.setattr(device, "run_adb", _fake_adb(ADB_DEVICES_OUT))
    assert device.get_devices() == ["emulator-5554", "192.168.1.20:5555"]


def test_device_info_splits_ip_port():
    info = device.device_info("192.168.1.20:5555")
    assert (info["ip"], info["port"]) == ("192.168.1.20", "5555")


def test_device_info_handles_usb_serial():
    """USB 直连的序列号不是 IP（如 V2324A），不能当成 ip:port 拆。"""
    info = device.device_info("V2324A")
    assert (info["serial"], info["ip"], info["port"]) == ("V2324A", "V2324A", "")


def test_usable_ip_filters_special_ranges():
    assert device._usable_ip("192.168.1.20")
    assert not device._usable_ip("127.0.0.1")
    assert not device._usable_ip("169.254.10.1")     # 网卡未连通时的自动地址
    assert not device._usable_ip("224.0.0.1")
    assert not device._usable_ip("192.168.1.255")
    assert not device._usable_ip("V2324A")


def test_ip_sort_key_puts_usb_last():
    """按 IP 数值排序，USB 序列号排到最后，且不抛异常。"""
    items = [{"ip": "192.168.1.20"}, {"ip": "V2324A"}, {"ip": "10.0.0.5"}]
    assert [e["ip"] for e in sorted(items, key=device._ip_sort_key)] == \
        ["10.0.0.5", "192.168.1.20", "V2324A"]


def test_needs_device_flag():
    assert not device._needs_device(["devices"])
    assert not device._needs_device(["connect", "1.2.3.4:5555"])
    assert device._needs_device(["shell", "getprop"])
    assert device._needs_device([])


def test_is_disconnect_detects_common_messages():
    assert device._is_disconnect("error: device offline")
    assert device._is_disconnect("no devices/emulators found")
    assert not device._is_disconnect("Successfully connected")


def test_serial_args_with_explicit_serial():
    assert device._serial_args("V2324A") == ["-s", "V2324A"]


def test_serial_args_single_device_needs_no_flag(monkeypatch):
    monkeypatch.setattr(device, "get_devices", lambda: ["192.168.1.20:5555"])
    assert device._serial_args() == []


def test_serial_args_prefers_usb_when_multiple(monkeypatch):
    """多台设备时不加 -s 会被 adb 直接拒绝，所以必须挑一台：优先 USB。"""
    monkeypatch.setattr(device, "get_devices",
                        lambda: ["192.168.1.20:5555", "V2324A"])
    assert device._serial_args() == ["-s", "V2324A"]


# ---------- 锁屏解锁 ----------
# 密码读写一律换成内存字典：真跑会往入口脚本的数据流里写，测试不该动到用户的真实数据。
# adb 也全部换成假函数：只验证身份键、解锁流程与缓存，不发一条真命令。

@pytest.fixture(autouse=True)
def _clear_unlock_caches(monkeypatch):
    """身份键 / 锁屏状态 / API 级别 / 顶住的自动锁定都是模块级状态，测试间必须清掉。"""
    monkeypatch.setattr(device, "_VOLUME_SETTLE", 0)   # 音量按键后的等待别真等
    # 数据流写入一律吞掉：音量处理会往投屏日志里记一笔，测试不该污染用户真实的日志文件
    monkeypatch.setattr(device, "storage_write", lambda *a, **k: ("mem", True))
    def clear():
        device._device_key_cache.clear()
        device._lock_cache.clear()
        device._sdk_cache.clear()
        device._screen_off_held.clear()
        device._boosted_volume.clear()
    clear()
    yield
    clear()


@pytest.fixture
def pin_store(monkeypatch):
    store = {}

    def fake_read(stream, binary=False):
        return store.get(stream)

    def fake_write(stream, data, binary=False, append=False):
        store[stream] = data
        return "mem:" + stream, True

    def fake_delete(stream):
        store.pop(stream, None)

    monkeypatch.setattr(device, "storage_read", fake_read)
    monkeypatch.setattr(device, "storage_write", fake_write)
    monkeypatch.setattr(device, "storage_delete", fake_delete)
    return store


def _fake_adb(out):
    def run(args, timeout=8, serial=None):
        return out, "", 0
    return run


class _FakeAdb:
    """按命令分派的假 adb：记录收到的命令，锁屏 / 亮屏状态与 secure 设置可以随时改。"""

    def __init__(self, sn="SN-ONE", serial_map=None, sdk=35):
        self.sn = sn
        self.serial_map = serial_map or {}
        self.sdk = sdk              # 35 = Android 15：才有 cmd display power-off
        self.display_cmd_ok = True  # 置 False 模拟老系统里这条命令报错
        self.power_off_works = True  # 置 False 模拟「命令发了但屏幕没关」
        self.volume = 5             # 媒体音量
        self.volume_max = 15
        self.volume_step = 10       # 按一下音量键的步进（真机 150 档位是 10 一格）
        self.set_works = True       # 置 False 模拟 ROM 把 --set 悄悄回滚（真机 vivo 就是这样）
        self.legacy_media = False   # 置 True 模拟老系统上还在的 `media` 命令
        self.volume_set_calls = []  # 每次 --set 收到的值，用来验证「拉满 → 还原」的顺序
        self.locked = False
        self.awake = True           # dumpsys power 里的亮屏状态
        self.screen_state = "ON"    # dumpsys display 里的 mScreenState
        self.settings = {}          # settings get/put/delete 的内存版
        self.settings_fail = False  # 置 True 时写设置失败，验证界面会如实提示
        self.dumpsys_calls = 0
        self.calls = []
        self.on_text = None      # 收到 input text 时回调（用来模拟解锁生效）

    def _volume_cmd(self, args):
        """音量命令：输出照抄真机（含 `will control stream=3` 那句回显），好验证解析不跑偏。"""
        if "--set" in args:
            vol = int(args[args.index("--set") + 1])
            self.volume_set_calls.append(vol)
            if self.set_works:
                self.volume = vol
            return "[V] will set volume to index=%d\n" % vol, "", 0
        return ("[V] will control stream=3 (STREAM_MUSIC)\n"
                "[V] will get volume\n"
                "[V] volume is %d in range [0..%d]\n" % (self.volume, self.volume_max), "", 0)

    def __call__(self, args, timeout=8, serial=None):
        self.calls.append(args)
        if args[1:3] == ["getprop", "ro.serialno"]:
            sn = self.serial_map.get(serial, self.sn)
            return (sn + "\n") if sn else "", "", 0
        if args[1:3] == ["getprop", "ro.build.version.sdk"]:
            return ("%d\n" % self.sdk) if self.sdk else "", "", 0
        if args[1:4] == ["cmd", "display", "power-off"]:
            if not self.display_cmd_ok:
                return "", "cmd: Can't find service: display", 1
            if self.power_off_works:
                self.screen_state = "OFF"
            return "Display power off: 0\n", "", 0
        if args[1:4] == ["cmd", "display", "power-on"]:
            if not self.display_cmd_ok:
                return "", "cmd: Can't find service: display", 1
            self.screen_state = "ON"
            return "Display power on: 0\n", "", 0
        if args[1:3] == ["cmd", "media_session"]:
            return self._volume_cmd(args)
        if args[1:2] == ["media"]:
            if not self.legacy_media:
                # Android 16 起这条命令没了，真机报的就是这句
                return "", "media: inaccessible or not found\n", 127
            return self._volume_cmd(args)
        if args[1:3] == ["dumpsys", "power"]:
            flag = "Awake" if self.awake else "Asleep"
            return "  mWakefulness=%s\n" % flag, "", 0
        if args[1:3] == ["dumpsys", "display"]:
            return "  mScreenState=%s\n" % self.screen_state, "", 0
        if args[1:3] == ["dumpsys", "audio"]:
            # 真机 `dumpsys audio` 里 STREAM_MUSIC 那一段的写法
            return ("  STREAM_MUSIC: Muted: false, Min: 0, Max: %d, streamVolume:%d,"
                    " Current: 2 (speaker): 0\n"
                    % (self.volume_max, self.volume), "", 0)
        if args[1:2] == ["dumpsys"]:
            self.dumpsys_calls += 1
            flag = "true" if self.locked else "false"
            return "  mDreamingLockscreen=%s\n" % flag, "", 0
        if args[1:2] == ["settings"]:
            if args[2] == "get":
                # 真 run_adb 会把 stdout 去掉首尾空白，假 adb 照做
                return self.settings.get(args[4], "null"), "", 0
            if self.settings_fail:
                return "", "write failed", 1
            if args[2] == "put":
                self.settings[args[4]] = args[5]
            elif args[2] == "delete":
                self.settings.pop(args[4], None)
            return "", "", 0
        if args[1:2] == ["wm"]:
            return "Physical size: 1080x2400\n", "", 0
        if args[1:3] == ["input", "keyevent"]:
            # 一次调用可以带多个键码：真机 `input keyevent 24 24 24` 会连按三下
            for code in args[3:]:
                if code not in ("24", "25"):
                    continue
                step = self.volume_step if code == "24" else -self.volume_step
                self.volume = max(0, min(self.volume_max, self.volume + step))
            return "", "", 0
        if args[1:3] == ["input", "text"] and self.on_text:
            self.on_text()
        return "", "", 0


class _FakeClock:
    """顶掉 device 里的 time：解锁流程里的等待加起来好几秒，测试不该真等；
    同时让 sleep 推着时间走，缓存 TTL 才会正常过期。"""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += sec

    def strftime(self, fmt):
        return "00:00:00"        # 日志时间戳：固定一个值就行，别让日志写崩


def _sends_input(adb):
    return [a for a in adb.calls if a[1:2] == ["input"]]


def test_device_key_prefers_hardware_serial(monkeypatch):
    """同一台手机 USB 与无线的 adb 序列号不同，硬件序列号相同 → 共用一份密码。"""
    monkeypatch.setattr(device, "run_adb", _FakeAdb(sn="SN123456"))
    assert device.device_key("V2324A") == "SN123456"
    assert device.device_key("172.19.163.3:5555") == "SN123456"


def test_device_key_falls_back_to_transport_serial(monkeypatch):
    monkeypatch.setattr(device, "run_adb", _FakeAdb(sn=""))
    assert device.device_key("V2324A") == "V2324A"


def _two_devices(monkeypatch):
    adb = _FakeAdb(serial_map={"V2324A": "SN-A", "172.19.163.3:5555": "SN-A",
                               "emulator-5554": "SN-B"})
    monkeypatch.setattr(device, "run_adb", adb)
    return adb


def test_pin_is_kept_per_device(pin_store, monkeypatch):
    """多设备：每台手机一份密码互不覆盖；同一台手机换接法仍读到同一份。"""
    adb = _two_devices(monkeypatch)
    assert device.get_unlock_pin("V2324A") == ""            # 没设过 = 不启用
    assert device.set_unlock_pin("V2324A", " 1234 ") is True
    assert device.set_unlock_pin("emulator-5554", "8888") is True
    assert device.get_unlock_pin("V2324A") == "1234"        # 两端空白顺手去掉
    assert device.get_unlock_pin("172.19.163.3:5555") == "1234"
    assert device.get_unlock_pin("emulator-5554") == "8888"
    assert "1234" not in pin_store[device.UNLOCK_PINS_STREAM]   # 不是明文躺在数据流里
    assert adb.calls                                            # 假 adb 至少被问过身份键


def test_pin_clear_only_affects_one_device(pin_store, monkeypatch):
    _two_devices(monkeypatch)
    device.set_unlock_pin("V2324A", "1234")
    device.set_unlock_pin("emulator-5554", "8888")
    assert device.set_unlock_pin("V2324A", "") is True
    assert device.get_unlock_pin("V2324A") == ""
    assert device.get_unlock_pin("emulator-5554") == "8888"


def test_pin_clear_removes_stream_when_no_device_left(pin_store, monkeypatch):
    _two_devices(monkeypatch)
    device.set_unlock_pin("V2324A", "1234")
    device.set_unlock_pin("V2324A", "")
    assert device.get_unlock_pin("V2324A") == ""
    assert device.UNLOCK_PINS_STREAM not in pin_store       # 一台都不剩就别留空壳


def test_pin_survives_non_ascii(pin_store, monkeypatch):
    """字母数字密码也可能是中文输入法打出来的，编解码不能崩。"""
    _two_devices(monkeypatch)
    device.set_unlock_pin("V2324A", "密码abc")
    assert device.get_unlock_pin("V2324A") == "密码abc"


def test_pin_corrupted_data_is_treated_as_unset(pin_store, monkeypatch):
    _two_devices(monkeypatch)
    pin_store[device.UNLOCK_PINS_STREAM] = "不是 json！"
    assert device.get_unlock_pin("V2324A") == ""
    pin_store[device.UNLOCK_PINS_STREAM] = '{"SN-A": 12345}'
    assert device.get_unlock_pin("V2324A") == ""


def test_screen_locked_reads_flag(monkeypatch):
    adb = _FakeAdb()
    monkeypatch.setattr(device, "run_adb", adb)
    adb.locked = True
    assert device.screen_locked("V2324A") is True
    adb.locked = False
    assert device.screen_locked("V2324A", force=True) is False


def test_screen_locked_without_flag_is_not_locked(monkeypatch):
    """ROM 没有这个标志时当作未锁：不确定还去敲密码，可能打进某个聊天窗口。"""
    monkeypatch.setattr(device, "run_adb", _fake_adb("some rom output\n"))
    assert device.screen_locked("V2324A") is False


def test_screen_locked_on_adb_failure_is_not_locked(monkeypatch):
    monkeypatch.setattr(device, "run_adb", lambda *a, **k: ("", "error", 1))
    assert device.screen_locked("V2324A") is False


def test_screen_locked_falls_back_to_second_flag(monkeypatch):
    monkeypatch.setattr(device, "run_adb", _fake_adb("  mShowingLockscreen=true\n"))
    assert device.screen_locked("V2324A") is True


def test_screen_locked_is_cached(monkeypatch):
    """dumpsys window 输出很大而界面每 3 秒轮询一次状态，不能每次都真跑一遍。"""
    adb = _FakeAdb()
    monkeypatch.setattr(device, "run_adb", adb)
    clock = _FakeClock()
    monkeypatch.setattr(device, "time", clock)
    device.screen_locked("V2324A")
    device.screen_locked("V2324A")
    assert adb.dumpsys_calls == 1
    clock.now += device._LOCK_CACHE_TTL + 1
    device.screen_locked("V2324A")
    assert adb.dumpsys_calls == 2


def test_unlock_now_without_pin_does_not_touch_screen(pin_store, monkeypatch):
    """没填密码就不碰屏幕——这是"未填写则不启用"的硬要求。"""
    adb = _FakeAdb()
    monkeypatch.setattr(device, "run_adb", adb)
    r = device.unlock_now("V2324A")
    assert r["ok"] is False and r["msg"] and "设置" in r["msg"]
    assert _sends_input(adb) == []


def test_unlock_now_when_already_unlocked(pin_store, monkeypatch):
    """没锁屏时只负责点亮屏幕，不该往手机上乱输字符。"""
    adb = _FakeAdb()
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    device.set_unlock_pin("V2324A", "1234")
    adb.locked = False
    r = device.unlock_now("V2324A")
    assert r["ok"] is True and "已点亮" in r["msg"]
    assert _sends_input(adb) == [["shell", "input", "keyevent", "224"]]


def test_unlock_now_wakes_screen_before_swiping(pin_store, monkeypatch):
    """真机反馈：熄屏时上滑 / 输密码都落不到锁屏界面上，必须先点亮屏幕。"""
    adb = _FakeAdb()
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    device.set_unlock_pin("V2324A", "1234")
    adb.locked = True                        # 熄屏 + 锁屏：先 wake 才能解锁
    adb.on_text = lambda: setattr(adb, "locked", False)

    r = device.unlock_now("V2324A")
    assert r["ok"] is True and "已解锁" in r["msg"]
    wake = adb.calls.index(["shell", "input", "keyevent", "224"])
    swipe = adb.calls.index(["shell", "input", "swipe", "540", "1920", "540", "600", "200"])
    text = adb.calls.index(["shell", "input", "text", "1234"])
    assert wake < swipe < text


def test_screen_off_uses_display_command_on_android15(monkeypatch):
    """Android 15+：直接关显示电源。设备不进睡眠，锁屏那套逻辑压根不启动。"""
    adb = _FakeAdb(sdk=35)
    adb.settings[device._LOCK_TIMEOUT_KEY] = "5000"
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())

    r = device.screen_off("V2324A")
    assert r["ok"] is True and "不会锁定" in r["msg"]
    assert ["shell", "cmd", "display", "power-off", "0"] in adb.calls
    assert ["shell", "input", "keyevent", "26"] not in adb.calls     # 不按电源键
    assert adb.settings[device._LOCK_TIMEOUT_KEY] == "5000"          # 也没动用户的设置


def test_screen_off_falls_back_on_old_android(monkeypatch):
    """Android 14 及以下没有那条命令：退回「顶住自动锁定 + 电源键」，并记住原值。"""
    adb = _FakeAdb(sdk=34)
    adb.settings[device._LOCK_TIMEOUT_KEY] = "5000"
    adb.awake = False                       # 按完电源键后确实熄屏了
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())

    r = device.screen_off("V2324A")
    assert r["ok"] is True
    assert ["shell", "input", "keyevent", "26"] in adb.calls
    assert ["shell", "cmd", "display", "power-off", "0"] not in adb.calls
    # 顶住的设置不能马上还原（提前还原等于没顶），要记到点亮屏幕时再还
    assert adb.settings[device._LOCK_TIMEOUT_KEY] == str(device._SCREEN_OFF_KEEP_MS)
    assert device._screen_off_held["V2324A"] == "5000"


def test_screen_off_falls_back_when_display_command_missing(monkeypatch):
    """Android 15 但这条命令报错（个别 ROM 裁掉了）：也要能退回电源键那条路。"""
    adb = _FakeAdb(sdk=35)
    adb.display_cmd_ok = False
    adb.awake = False
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    r = device.screen_off("V2324A")
    assert r["ok"] is True
    assert ["shell", "input", "keyevent", "26"] in adb.calls


def test_screen_off_reports_when_screen_stays_on(monkeypatch):
    """手机没熄屏时必须如实说，而不是假装成功。"""
    adb = _FakeAdb(sdk=35)
    adb.power_off_works = False             # 命令发了，屏还亮着
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    r = device.screen_off("V2324A")
    assert r["ok"] is False and "亮着" in r["msg"]


def test_wake_screen_restores_lock_timeout(monkeypatch):
    """点亮屏幕时要把顶住的「熄屏后自动锁定」还给用户。"""
    adb = _FakeAdb(sdk=34)
    adb.settings[device._LOCK_TIMEOUT_KEY] = "5000"
    adb.awake = False
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    device.screen_off("V2324A")
    assert adb.settings[device._LOCK_TIMEOUT_KEY] == str(device._SCREEN_OFF_KEEP_MS)

    device.wake_screen("V2324A")
    assert adb.settings[device._LOCK_TIMEOUT_KEY] == "5000"
    assert device._screen_off_held == {}
    assert ["shell", "input", "keyevent", "224"] in adb.calls


def test_release_lock_timeout_deletes_when_it_was_unset(monkeypatch):
    """原来就没有这条设置的话，还原等于删掉，不能留下我们写的那条。"""
    adb = _FakeAdb(sdk=34)
    adb.awake = False
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    device.screen_off("V2324A")
    device.release_lock_timeout("V2324A")
    assert device._LOCK_TIMEOUT_KEY not in adb.settings


def test_screen_off_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("adb 没了")

    monkeypatch.setattr(device, "run_adb", boom)
    r = device.screen_off("V2324A")
    assert r["ok"] is False and r["msg"]


def test_restore_lock_timeout_puts_back_or_deletes(monkeypatch):
    adb = _FakeAdb()
    adb.settings[device._LOCK_TIMEOUT_KEY] = str(device._SCREEN_OFF_KEEP_MS)
    monkeypatch.setattr(device, "run_adb", adb)
    device._restore_lock_timeout("V2324A", "5000")
    assert adb.settings[device._LOCK_TIMEOUT_KEY] == "5000"
    device._restore_lock_timeout("V2324A", "null")      # 原来没设过 → 删掉这条设置
    assert device._LOCK_TIMEOUT_KEY not in adb.settings


def test_unlock_now_swipes_and_types_pin(pin_store, monkeypatch):
    """锁着时：上滑 → 输密码 → 回车，一次都不能少。"""
    adb = _FakeAdb()
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    device.set_unlock_pin("V2324A", "1234")
    adb.locked = True
    adb.on_text = lambda: setattr(adb, "locked", False)

    r = device.unlock_now("V2324A")
    assert r["ok"] is True and "已解锁" in r["msg"]
    assert ["shell", "input", "swipe", "540", "1920", "540", "600", "200"] in adb.calls
    assert ["shell", "input", "text", "1234"] in adb.calls
    assert ["shell", "input", "keyevent", "66"] in adb.calls


def test_unlock_now_tolerates_rom_flag_delay(pin_store, monkeypatch):
    """真机反馈：其实已经解开、锁屏标志却晚一拍翻转，不能报成解锁失败。"""
    state = {"locked": True, "sent": False, "reads": 0}

    def adb(args, timeout=8, serial=None):
        if args[1:3] == ["getprop", "ro.serialno"]:
            return "SN-ONE\n", "", 0
        if args[1:2] == ["dumpsys"]:
            state["reads"] += 1
            if state["sent"] and state["reads"] >= 3:
                state["locked"] = False          # 输完密码后第 3 次读才报「已解锁」
            return "  mDreamingLockscreen=%s\n" % ("true" if state["locked"] else "false"), "", 0
        if args[1:3] == ["input", "text"]:
            state["sent"] = True
        if args[1:2] == ["wm"]:
            return "Physical size: 1080x2400\n", "", 0
        return "", "", 0

    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    device.set_unlock_pin("V2324A", "1234")

    r = device.unlock_now("V2324A")
    assert r["ok"] is True and "已解锁" in r["msg"]


def test_unlock_now_reports_when_still_locked(pin_store, monkeypatch):
    """解锁没成功必须在界面上说出来，而不是假装一切正常。"""
    adb = _FakeAdb()
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    device.set_unlock_pin("V2324A", "1234")
    adb.locked = True                        # 输完密码依然锁着

    r = device.unlock_now("V2324A")
    assert r["ok"] is False and "仍报告锁屏" in r["msg"]


def test_unlock_now_never_raises(pin_store, monkeypatch):
    """adb 炸了也只是没解锁，不能把异常抛到 HTTP 线程上。"""

    def boom(*a, **k):
        raise OSError("adb 没了")

    monkeypatch.setattr(device, "run_adb", boom)
    monkeypatch.setattr(device, "time", _FakeClock())
    r = device.unlock_now("V2324A")
    assert r["ok"] is False and r["msg"]


# ---------- 「仅电脑播放」时拉满手机音量 ----------
# scrcpy 抓的是手机输出的那路声音，手机音量压着电脑这边就没声 / 很小；
# 所以起投屏之前拉满，关掉投屏窗口再还给用户。

class _FakeProc:
    """假投屏进程：finish() 之后 wait() 才返回，用来观察「关窗后还原」。"""

    def __init__(self):
        self.pid = 4242
        self._done = threading.Event()

    def finish(self):
        self._done.set()

    def wait(self, timeout=None):
        self._done.wait(timeout or 5)
        return 0

    def poll(self):
        return 0 if self._done.is_set() else None


def test_media_volume_parses_range_output(monkeypatch):
    """`volume is 7 in range [0..15]`：老实现按 split()[-1] 取到的是 "[0..15]"，
    解析一直失败 → 音量从来没被读到过。"""
    adb = _FakeAdb()
    adb.volume, adb.volume_max = 7, 15
    monkeypatch.setattr(device, "run_adb", adb)
    assert device.get_media_volume("V2324A") == 7
    assert device._media_volume("V2324A") == (7, 15)


def test_media_volume_output_echo_is_not_the_volume(monkeypatch):
    """`cmd media_session` 会先回显 `will control stream=3`：那个 3 不是音量，
    所以不能"把输出里的数字都抓出来"。"""
    adb = _FakeAdb()
    adb.volume = 12
    monkeypatch.setattr(device, "run_adb", adb)
    assert device._media_volume("V2324A") == (12, 15)


def test_media_volume_reads_without_legacy_media_command(monkeypatch):
    """真机（Android 16）没有 `media` 这条命令，必须靠 `cmd media_session` 读。"""
    adb = _FakeAdb()
    monkeypatch.setattr(device, "run_adb", adb)
    assert device.get_media_volume("V2324A") == 5
    assert ["shell", "cmd", "media_session", "volume", "--stream", "3", "--get"] in adb.calls


def test_media_volume_falls_back_to_legacy_command(monkeypatch):
    """更老的系统上 `cmd media_session` 未必有，那就走 `media`。"""
    adb = _FakeAdb()
    adb.legacy_media = True
    adb.volume = 9
    monkeypatch.setattr(device, "run_adb",
                        lambda args, timeout=8, serial=None:
                        ("", "", 1) if args[1:3] == ["cmd", "media_session"]
                        else adb(args, timeout, serial))
    assert device.get_media_volume("V2324A") == 9


def test_set_media_volume_falls_back_to_keys_when_rom_reverts(monkeypatch):
    """真机实测：`--set` 返回成功却被 ROM 立刻回滚，只有音量键真的管用。"""
    adb = _FakeAdb()
    adb.volume = 4
    adb.set_works = False
    monkeypatch.setattr(device, "run_adb", adb)
    assert device.set_media_volume(15, "V2324A") is True
    assert adb.volume == 15
    ups = [a for a in adb.calls if a[1:4] == ["input", "keyevent", "24"]]
    assert len(ups) == 2            # 4 → 14 → 15


def test_set_media_volume_gives_up_when_volume_is_pinned(monkeypatch):
    """音量键也推不动（被前台的播放器顶回来）时如实返回失败，别谎报。"""
    adb = _FakeAdb()
    adb.volume = 4
    adb.set_works = False
    adb.volume_step = 0
    monkeypatch.setattr(device, "run_adb", adb)
    assert device.set_media_volume(15, "V2324A") is False
    assert adb.volume == 4


def test_set_media_volume_does_not_overshoot_unreachable_target(monkeypatch):
    """原值不是步进的整数倍时（真机 150 档 / 步进 10，用户原值 36 这种），音量键根本按不到
    那个数：宁可最后差一格，也不能按过头——曾经把 36 一路按到 0。"""
    adb = _FakeAdb()
    adb.volume_max = 150
    adb.volume_step = 10
    adb.volume = 150
    adb.set_works = False
    monkeypatch.setattr(device, "run_adb", adb)
    device.set_media_volume(36, "V2324A")
    assert 30 <= adb.volume <= 40


def test_media_volume_parses_bare_number(monkeypatch):
    """个别系统只回一个数字：当当前值用，上限未知。"""
    monkeypatch.setattr(device, "run_adb", _fake_adb("5\n"))
    assert device._media_volume("V2324A") == (5, -1)


def test_media_volume_unreadable(monkeypatch):
    monkeypatch.setattr(device, "run_adb", _fake_adb(""))
    assert device._media_volume("V2324A") == (-1, -1)


def test_boost_media_volume_sets_max_then_restores(monkeypatch):
    adb = _FakeAdb()
    adb.volume = 4
    monkeypatch.setattr(device, "run_adb", adb)
    assert device.boost_media_volume("V2324A") == 4
    assert adb.volume == adb.volume_max
    device.restore_media_volume("V2324A")
    assert adb.volume == 4
    assert adb.volume_set_calls == [15, 4]
    assert device._boosted_volume == {}


def test_boost_media_volume_remembers_original_only_once(monkeypatch):
    """同一台手机连着开两个窗口时，别把「已经拉满的 15」当成原值记进登记表。"""
    adb = _FakeAdb()
    adb.volume = 4
    monkeypatch.setattr(device, "run_adb", adb)
    device.boost_media_volume("V2324A")
    device.boost_media_volume("V2324A")
    assert device._boosted_volume == {"V2324A": 4}
    device.restore_media_volume("V2324A")
    assert adb.volume == 4


def test_boost_media_volume_registers_nothing_when_it_cannot_change(monkeypatch):
    """没真拉上去就别登记：否则关窗口时反而会去动一个我们没改过的音量。"""
    adb = _FakeAdb()
    adb.volume = 4
    adb.set_works = False
    adb.volume_step = 0
    monkeypatch.setattr(device, "run_adb", adb)
    assert device.boost_media_volume("V2324A") is None
    assert device._boosted_volume == {}


def test_boost_media_volume_skips_when_already_max(monkeypatch):
    adb = _FakeAdb()
    adb.volume = adb.volume_max
    monkeypatch.setattr(device, "run_adb", adb)
    assert device.boost_media_volume("V2324A") is None
    assert device._boosted_volume == {}
    device.restore_media_volume("V2324A")        # 没拉满过，还原是空操作
    assert adb.volume_set_calls == []


def test_boost_media_volume_gives_up_when_unreadable(monkeypatch):
    """读不到音量就什么都不做：宁可没声音，也别乱改用户的设置。"""
    monkeypatch.setattr(device, "run_adb", _fake_adb(""))
    assert device.boost_media_volume("V2324A") is None
    assert device._boosted_volume == {}


def test_boost_media_volume_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("adb 没了")

    monkeypatch.setattr(device, "run_adb", boom)
    assert device.boost_media_volume("V2324A") is None


def test_restore_all_media_volume_covers_every_device(monkeypatch):
    """退出应用时要一次还清——关窗走的是 os._exit，等投屏线程收尾来不及。"""
    adb = _FakeAdb()
    monkeypatch.setattr(device, "run_adb", adb)
    adb.volume = 3
    device.boost_media_volume("SN-A")
    adb.volume = 6
    device.boost_media_volume("SN-B")
    device.restore_all_media_volume()
    assert device._boosted_volume == {}
    assert adb.volume_set_calls == [15, 15, 3, 6]


def _wait_until(cond, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_recheck_boost_volume_pulls_the_volume_back_up(monkeypatch):
    """真机实测：镜像一开，ROM 按「输出设备的记忆值」把音量拉回去（70 → 40 都见过），
    所以投屏起来后要复查几遍，把它重新拉满。"""
    adb = _FakeAdb()
    adb.volume = 4
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())     # 复查要等好几秒，测试不该真等
    assert device.boost_media_volume("V2324A") == 4

    adb.volume = 6                       # 镜像起来后 ROM 把音量拉回去了
    device.recheck_boost_volume("V2324A")
    assert _wait_until(lambda: adb.volume == adb.volume_max)
    assert device._boosted_volume == {"V2324A": 4}        # 原值还是启动时那个 4


def test_recheck_boost_volume_leaves_volume_alone_without_boost(monkeypatch):
    """没拉满过（选的是手机出声）就什么都不做，不能去动用户自己的音量。"""
    adb = _FakeAdb()
    adb.volume = 6
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    device.recheck_boost_volume("V2324A")
    time.sleep(0.1)                      # 假时钟让那几轮复查瞬间跑完
    assert adb.volume == 6
    assert adb.volume_set_calls == []


def test_recheck_boost_volume_does_not_fight_restore(monkeypatch):
    """关窗口后复查线程不能把还原好的音量又拉满——真机上这个交叉会把音量留在最高。"""
    adb = _FakeAdb()
    adb.volume = 4
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "time", _FakeClock())
    device.boost_media_volume("V2324A")
    device.recheck_boost_volume("V2324A")
    device.restore_media_volume("V2324A")
    assert adb.volume == 4
    time.sleep(0.1)                      # 复查线程跑完剩下的轮次
    assert adb.volume == 4
    assert device._boosted_volume == {}


def test_launch_desktop_boosts_before_launch_then_restores(monkeypatch):
    """选「仅电脑播放」：起 scrcpy 之前就拉满，窗口关掉后还原。"""
    adb = _FakeAdb()
    adb.volume = 5
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "load_config", lambda: {"audio_mode": "pc"})
    monkeypatch.setattr(device, "_close_child_log", lambda pid: None)
    monkeypatch.setattr(device, "_BOOST_RECHECKS", ())   # 投屏后的复查另有专门用例，这里别留后台线程
    monkeypatch.setattr(device, "_wait_window", lambda proc, timeout=15: None)   # 别真去遍历窗口
    proc = _FakeProc()
    seen = {}

    def fake_spawn(cmd, tag="scrcpy"):
        seen["volume_at_launch"] = adb.volume
        seen["cmd"] = cmd
        return proc

    monkeypatch.setattr(device, "_spawn", fake_spawn)
    device.launch_desktop("V2324A")
    assert seen["volume_at_launch"] == adb.volume_max
    assert "--no-audio" not in seen["cmd"]

    proc.finish()                                # 窗口关掉 → 后台线程还原音量
    deadline = time.time() + 5
    while device._boosted_volume and time.time() < deadline:
        time.sleep(0.01)
    assert device._boosted_volume == {}
    assert adb.volume == 5


def test_launch_desktop_leaves_volume_alone_in_phone_mode(monkeypatch):
    adb = _FakeAdb()
    adb.volume = 5
    monkeypatch.setattr(device, "run_adb", adb)
    monkeypatch.setattr(device, "load_config", lambda: {"audio_mode": "phone"})
    monkeypatch.setattr(device, "_close_child_log", lambda pid: None)
    monkeypatch.setattr(device, "_BOOST_RECHECKS", ())
    monkeypatch.setattr(device, "_wait_window", lambda proc, timeout=15: None)
    proc = _FakeProc()
    seen = {}

    def fake_spawn(cmd, tag="scrcpy"):
        seen["cmd"] = cmd
        return proc

    monkeypatch.setattr(device, "_spawn", fake_spawn)
    device.launch_desktop("V2324A")
    assert "--no-audio" in seen["cmd"]
    assert adb.volume_set_calls == []


# ---------- 投屏窗口：位置记忆 / 置顶 / 标题 ----------
# 窗口位置存的是逻辑坐标（Windows 报给我们的那套），给 scrcpy 时要换算成物理像素；
# 换过显示器后旧坐标可能已经在屏幕外，那时宁可让 scrcpy 自己挑位置。

def _cmd(monkeypatch, cfg=None, **kwargs):
    """按给定配置拼一条投屏命令（不碰真的 scrcpy / adb）。"""
    monkeypatch.setattr(device, "load_config", lambda: dict(cfg or {}))
    monkeypatch.setattr(device, "_pc_dpi", lambda: 96)
    return device.build_scrcpy_cmd(**kwargs)


def test_window_args_use_saved_position(monkeypatch):
    monkeypatch.setattr(device, "_screen_scale", lambda: 2.0)
    monkeypatch.setattr(device, "_pos_reachable", lambda x, y: True)
    # 配置里存的是逻辑坐标，scrcpy 要物理像素：200% 缩放下要乘 2
    assert device._window_args({"window_x": "427", "window_y": "289"}) == \
        ["--window-x=854", "--window-y=578"]


def test_window_args_skip_when_nothing_saved(monkeypatch):
    monkeypatch.setattr(device, "_screen_scale", lambda: 1.0)
    assert device._window_args({}) == []
    assert device._window_args({"window_x": "", "window_y": ""}) == []
    assert device._window_args({"window_x": "abc", "window_y": "1"}) == []


def test_window_args_skip_when_off_screen(monkeypatch):
    """换过显示器 / 改过分辨率后旧坐标已经在屏幕外：不能再沿用，否则窗口开在看不见的地方。"""
    monkeypatch.setattr(device, "_screen_scale", lambda: 1.0)
    monkeypatch.setattr(device, "_pos_reachable", lambda x, y: False)
    assert device._window_args({"window_x": "5000", "window_y": "3000"}) == []


def test_build_cmd_puts_title_on_app_mirroring(monkeypatch):
    """镜像应用：标题栏写应用名，别只显示 scrcpy。"""
    cmd = _cmd(monkeypatch, pkg="com.tencent.mm", title="微信")
    assert "--window-title=微信" in cmd
    assert "--start-app=com.tencent.mm" in cmd


def test_build_cmd_has_no_title_for_desktop(monkeypatch):
    cmd = _cmd(monkeypatch)
    assert not any(a.startswith("--window-title") for a in cmd)


def test_build_cmd_always_on_top_switch(monkeypatch):
    assert "--always-on-top" in _cmd(monkeypatch, {"always_on_top": True})
    assert "--always-on-top" not in _cmd(monkeypatch, {"always_on_top": False})
    assert "--always-on-top" not in _cmd(monkeypatch)        # 没配过 → 默认不置顶


def test_build_cmd_keeps_native_titlebar(monkeypatch):
    """必须保留原生标题栏：拖边缩放 / 双击最大化 / 系统菜单都靠它。

    winbar 现在是在原生标题栏上叠加按钮，不能再传 --window-borderless —— 那会换成
    WS_POPUP 窗口，系统不给缩放边框，拖边完全不改窗口尺寸。
    """
    assert "--window-borderless" not in _cmd(monkeypatch, pkg="com.tencent.mm")


def test_launch_desktop_keeps_native_titlebar(monkeypatch):
    """镜像桌面同样保留原生标题栏（靠 --window-title 给标题），并挂上叠加按钮。"""
    monkeypatch.setattr(device, "load_config", lambda: {})
    monkeypatch.setattr(device, "_close_child_log", lambda pid: None)
    monkeypatch.setattr(device, "_BOOST_RECHECKS", ())
    monkeypatch.setattr(device, "_wait_window", lambda proc, timeout=15: None)
    attached = {}
    monkeypatch.setattr(device.winbar, "attach",
                        lambda proc, **kw: attached.update(kw) or attached)
    proc = _FakeProc()
    seen = {}

    def fake_spawn(cmd, tag="scrcpy"):
        seen["cmd"] = cmd
        return proc

    monkeypatch.setattr(device, "_spawn", fake_spawn)
    device.launch_desktop("V2324A")
    assert "--window-borderless" not in seen["cmd"]
    assert "--window-title=镜像桌面" in seen["cmd"]
    proc.finish()
    deadline = time.time() + 5
    while not attached and time.time() < deadline:
        time.sleep(0.01)
    assert "title" not in attached                  # 新的 attach 不再自绘整条栏
    assert attached.get("always_on_top") is None    # 没配过 → 默认不置顶
