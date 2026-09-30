"""设备与 adb 调用：状态分组、序列号解析、多设备参数选择。

adb 一律不打真的：把 run_adb 换成返回固定输出的假函数，只验证解析与选择逻辑。
"""

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
