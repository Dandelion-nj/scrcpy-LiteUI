"""设备与投屏。

adb / scrcpy 调用封装、设备连接状态三态判定（device / unauthorized / offline）、
无线连接与自动重连、投屏命令拼装与启动（镜像应用 / 镜像桌面）、
子进程统一登记与收尾。

依赖：storage（路径与配置）。
"""


import base64
import ctypes
import json
import os
import re
import socket
import subprocess
import threading
import time
from ctypes import wintypes

from .storage import (
    ADB_PATH,
    LAUNCH_LOG_STREAM,
    SCRCPY_PATH,
    UNLOCK_PINS_STREAM,
    _read_log_tail,
    load_config,
    save_config,
    storage_delete,
    storage_open_append,
    storage_read,
    storage_write,
)


def get_startupinfo():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si

_adb_slots = threading.BoundedSemaphore(2)  # 限制 adb 并发为 2，避免打挂 adb server / 挤掉无线设备
_NO_DEVICE_ARGS = {"devices", "connect", "pair", "disconnect", "start-server",
                   "kill-server", "version", "help", "mdns"}

def _needs_device(args):
    return not (args and args[0] in _NO_DEVICE_ARGS)

def _is_disconnect(msg):
    msg = (msg or "").lower()
    return any(x in msg for x in (
        "no devices/emulators found", "device offline", "device not found",
        "couldn't read from", "closed", "eof", "broken pipe"))

def _reconnect(serial=None):
    """连接掉线后重连一次：优先重连指定设备，否则用配置里的上次地址。"""
    if serial:
        addr = serial
    else:
        cfg = load_config()
        ip, port = cfg.get("ip", ""), str(cfg.get("port", ""))
        addr = "%s:%s" % (ip, port) if ip and port else ""
    if addr:
        try:
            subprocess.run([ADB_PATH, "connect", addr],
                           capture_output=True, timeout=15,
                           startupinfo=get_startupinfo(), creationflags=0x08000000)
        except Exception:
            pass

def _serial_args(serial=None):
    """返回要附加的设备参数。

    指定 serial（来自 /api/status 的在线设备）时直接用 -s <serial>；
    未指定时若同时连着多台，优先 USB 有线（延迟低、不掉线），否则退回配置里的
    地址 —— 不指定的话 adb/scrcpy 会直接报 'Multiple (2) ADB devices' 失败。
    """
    if serial:
        return ["-s", serial]
    devs = get_devices()
    if len(devs) < 2:
        return []
    usb = [d for d in devs if ":" not in d]
    if usb:
        return ["-s", usb[0]]
    cfg = load_config()
    want = "%s:%s" % (cfg.get("ip", ""), cfg.get("port", ""))
    return ["-s", want if want in devs else devs[0]]

def _popen_adb(args, timeout, binary=False):
    kwargs = {"capture_output": True, "timeout": timeout,
              "startupinfo": get_startupinfo(), "creationflags": 0x08000000}
    if binary:
        kwargs["text"] = False      # 远程按块读 APK，需要原始字节
    else:
        # 必须显式按 UTF-8 解码：打包后的进程没有控制台，locale 编码是 GBK，
        # 用 text=True 会拿 GBK 去解 adb/scrcpy 的 UTF-8 输出，读取线程抛
        # UnicodeDecodeError，stdout 变 None，所有调用方一起失败。
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
    return subprocess.run([ADB_PATH] + args, **kwargs)

def run_adb(args, timeout=8, serial=None):
    # 在拿信号量之前先算好 -s，避免 _serial_args() 里的 get_devices() 再次占用 adb 槽位
    if _needs_device(args):
        args = _serial_args(serial) + args
    with _adb_slots:
        try:
            r = _popen_adb(args, timeout)
            if _needs_device(args) and _is_disconnect(r.stderr + r.stdout):
                _reconnect(serial)
                r = _popen_adb(args, timeout)
            return r.stdout.strip(), r.stderr.strip(), r.returncode
        except subprocess.TimeoutExpired:
            return "", "timeout", -1
        except Exception as e:
            return "", str(e), -1

def run_adb_bin(args, timeout=45, serial=None):
    if _needs_device(args):
        args = _serial_args(serial) + args
    with _adb_slots:
        try:
            r = _popen_adb(args, timeout, binary=True)
            if _needs_device(args) and _is_disconnect(r.stderr.decode(errors="ignore")):
                _reconnect(serial)
                r = _popen_adb(args, timeout, binary=True)
            return r.stdout
        except Exception:
            return b""

def get_devices():
    out, _, _ = run_adb(["devices"])
    devices = []
    for line in out.strip().split('\n')[1:]:
        line = line.strip()
        if line and '\t' in line:
            addr, status = line.split('\t')
            if status.strip() == 'device':
                devices.append(addr.strip())
    return devices

def device_states():
    """按 adb devices 的原始状态分组，供界面区分「已连接 / 等待授权 / 离线 / 未连接」。

    之前只认 status=='device'，导致两类误报：手机停在「允许 USB 调试」弹窗时
    被当成"未连接"；adb 里还挂着但已经掉线的设备被当成"已连接"。
    """
    out, _, _ = run_adb(["devices"])
    states = {"device": [], "unauthorized": [], "offline": [], "other": []}
    for line in out.strip().split('\n')[1:]:
        line = line.strip()
        if not line or '\t' not in line:
            continue
        serial, status = line.split('\t', 1)
        serial, status = serial.strip(), status.strip()
        if not serial:
            continue
        states[status if status in states else "other"].append(serial)
    return states

def device_info(serial):
    """把设备序列号拆成 {serial, ip, port}，USB 设备（无端口）也能安全处理。"""
    ip, _, port = serial.rpartition(':')
    if ip and port.isdigit():
        return {"serial": serial, "ip": ip, "port": port}
    return {"serial": serial, "ip": serial, "port": ""}

_model_cache = {}
_model_lock = threading.Lock()

def _device_model(serial):
    """取设备型号用于标签页显示；结果缓存，避免每次轮询状态都跑一次 adb。"""
    with _model_lock:
        if serial in _model_cache:
            return _model_cache[serial]
    out, _, _ = run_adb(["shell", "getprop", "ro.product.model"], timeout=8, serial=serial)
    model = (out.strip().splitlines() or [""])[0].strip() or serial
    with _model_lock:
        _model_cache[serial] = model
    return model

# 「最近连接」：连上一次就记一笔，之后不用再输 IP / 端口。最多留 5 台，最近的排最前。
def _remember_device(addr, name=""):
    if not addr:
        return
    ip, _, port = addr.rpartition(":")
    ip = ip or addr
    if not name and ip in [device_info(d)["ip"] for d in get_devices()]:
        name = _device_model(addr)
        if name == addr:
            name = ""
    cfg = load_config()
    items = [it for it in (cfg.get("recent_devices") or [])
             if isinstance(it, dict) and it.get("addr") and it.get("addr") != addr]
    items.insert(0, {"addr": addr, "ip": ip, "port": port or str(CLASSIC_ADB_PORT),
                     "name": name or "", "ts": int(time.time())})
    save_config({"recent_devices": items[:5]})

CLASSIC_ADB_PORT = 5555           # adb tcpip 模式的固定端口
def _ip_sort_key(e):
    """按 IP 数值排序；USB 序列号这种不是 IP 的值排到最后。

    以前这里直接 int(ip)，USB 直连设备的 ip 是序列号（如 V2324A），一旦插着
    数据线调发现接口就抛 ValueError，整个局域网扫描直接失败。
    """
    ip = e.get("ip") or ""
    if not _usable_ip(ip):
        return (999, 999, 999, 999)
    return tuple(int(x) for x in ip.split("."))

def _usable_ip(ip):
    """排除组播/广播/回环/链路本地地址（169.254 是网卡未连通时的自动地址）。"""
    try:
        a, b, c, d = (int(x) for x in ip.split("."))
    except ValueError:
        return False
    if a in (0, 127) or a >= 224:
        return False
    if a == 169 and b == 254:
        return False
    if d in (0, 255):
        return False
    return True

# ---------- USB 一键转无线 ----------
# 手机用数据线插上（已允许 USB 调试）时点一下：记下 Wi-Fi IP → adb tcpip 5555
# → adb connect，之后拔掉数据线就能无线投屏。只在用户点击时执行。
def _usb_serial():
    for d in device_states()["device"]:
        if ":" not in d:                  # 序列号不带端口 = USB 直连
            return d
    return None

def _device_wifi_ip(serial):
    """取手机的 Wi-Fi IP：优先 `ip route` 的 src，回退 wlan0 的 inet 地址。"""
    out, _, _ = run_adb(["shell", "ip", "route"], timeout=8, serial=serial)
    m = re.search(r'\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})', out or "")
    if m and _usable_ip(m.group(1)):
        return m.group(1)
    out, _, _ = run_adb(["shell", "ip", "-f", "inet", "addr", "show", "wlan0"],
                        timeout=8, serial=serial)
    m = re.search(r'\binet\s+(\d{1,3}(?:\.\d{1,3}){3})', out or "")
    if m and _usable_ip(m.group(1)):
        return m.group(1)
    return ""

def usb_to_wifi(port=CLASSIC_ADB_PORT):
    """USB 转无线：返回 {ok, addr, error}。IP 要在切 tcpip 之前取（USB 那时一定在线）。"""
    usb = _usb_serial()
    if not usb:
        return {"ok": False, "error": "没有检测到 USB 连接的手机：请先用数据线连上电脑并允许 USB 调试"}
    ip = _device_wifi_ip(usb)
    if not ip:
        return {"ok": False, "error": "取不到手机的 Wi-Fi IP：请确认手机已连上 Wi-Fi 再试"}
    out, err, _ = run_adb(["tcpip", str(port)], timeout=20, serial=usb)
    msg = ((out or "") + (err or "")).lower()
    if "restarting in tcp mode" not in msg and "already in tcp mode" not in msg:
        return {"ok": False, "error": ((out or "") + (err or "")).strip() or "切换无线调试失败"}
    addr = "%s:%s" % (ip, port)
    time.sleep(1.5)                        # 等 adbd 在 5555 上重新监听
    out, err, _ = run_adb(["connect", addr], timeout=15)
    text = ((out or "") + (err or "")).lower()
    if "connected" not in text or any(x in text for x in ("cannot", "failed", "refused")):
        return {"ok": False, "addr": addr,
                "error": ((out or "") + (err or "")).strip() or "adb connect 失败"}
    _skip_reconnect.discard(addr)
    for _ in range(10):                    # 等设备在 adb 里就绪（首次会弹「允许调试」）
        if ip in [device_info(d)["ip"] for d in get_devices()]:
            _remember_device(addr)
            save_config({"ip": ip, "port": str(port)})
            return {"ok": True, "addr": addr}
        time.sleep(1)
    return {"ok": True, "addr": addr,
            "note": "已发起无线连接，若手机弹出「允许调试」请点允许"}

# ---------- 掉线自动重连 ----------
# 只重连「曾经连上、后来掉线」的无线地址（手机熄屏、路由器休眠都会掉）。每台地址
# 独立退避：6s → 12s → 24s → 60s（之后固定 60s），连续 10 次失败就放弃这一台，
# 等它重新出现在设备列表里再从头开始，避免无限刷 adb。
_reconnect_state = {}        # addr -> {"tries": n, "next": ts}
_reconnect_lock = threading.Lock()
_skip_reconnect = set()      # 用户主动「断开」过的地址：不自动重连，直到重新手动连上
_RECONNECT_BACKOFF = (6, 12, 24, 60)
_RECONNECT_MAX_TRIES = 10

def _try_reconnect(addr):
    try:
        run_adb(["connect", addr], timeout=15)
    except Exception:
        pass

def _reconnect_loop():
    last_seen = set()
    while True:
        time.sleep(6)
        try:
            if not load_config().get("reconnect_enabled", True):
                with _reconnect_lock:
                    _reconnect_state.clear()
                last_seen = set()
                continue
            online = set(get_devices())
            now = time.time()
            gone = {a for a in last_seen - online if ":" in a and a not in _skip_reconnect}
            with _reconnect_lock:
                for a in online:
                    _reconnect_state.pop(a, None)      # 已连上：清零重连计数
                for a in gone:
                    st = _reconnect_state.setdefault(a, {"tries": 0, "next": 0.0})
                    if st["tries"] >= _RECONNECT_MAX_TRIES or now < st["next"]:
                        continue
                    st["tries"] += 1
                    st["next"] = now + _RECONNECT_BACKOFF[
                        min(st["tries"] - 1, len(_RECONNECT_BACKOFF) - 1)]
                    threading.Thread(target=_try_reconnect, args=(a,), daemon=True).start()
            last_seen = online
        except Exception:
            continue

threading.Thread(target=_reconnect_loop, name="reconnect", daemon=True).start()
def _pc_dpi():
    """取电脑当前 DPI（含系统显示缩放，如 125% → 120）。取不到时回退 96（100%）。"""
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()      # Win10 1607+
        if dpi:
            return int(dpi)
    except Exception:
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 90)   # LOGPIXELSY
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        return int(dpi) if dpi else 96
    except Exception:
        return 96

def build_scrcpy_cmd(pkg=None, serial=None):
    cfg = load_config()
    w = cfg.get("res_w", "1080")
    h = cfg.get("res_h", "2400")
    scale = float(cfg.get("scale", "1.0"))
    # 1 倍缩放 = 电脑 DPI（新虚拟显示器的密度与电脑一致，观感最接近原生）
    dpi = max(72, int(round(_pc_dpi() * scale)))
    audio_mode = cfg.get("audio_mode", "both")

    cmd = [SCRCPY_PATH] + _serial_args(serial) + [
        "--flex-display",
        "--stay-awake",
        "--window-x=600",
        "--window-y=50",
    ]
    cmd += _stream_args(cfg) + _video_args(cfg)
    if audio_mode == "phone":
        cmd.append("--no-audio")

    if pkg:
        cmd.append(f"--new-display={w}x{h}/{dpi}")
        cmd.append("--no-vd-system-decorations")   # 虚拟屏里不画状态栏/导航栏
        cmd.append(f"--start-app={pkg}")
    return cmd

def _stream_args(cfg):
    """码率 / 帧率：镜像应用和镜像桌面共用，避免两边设置不一致。

    这两项对"虚拟屏镜像应用"和"镜像桌面"都生效 —— 原来只在 build_scrcpy_cmd 里
    拼，镜像桌面走的是另一条命令，导致设置页改了帧率 / 码率对桌面投屏没反应。
    """
    return ["--video-bit-rate=%sM" % cfg.get("bitrate", "8"),
            "--max-fps=%s" % cfg.get("fps", "60")]

def _video_args(cfg):
    """画面公共参数：实测帧率输出（诊断报告用）、视频编码、最大尺寸。"""
    args = ["--print-fps"]                    # 每秒把实测帧率写进投屏日志，供诊断报告提取
    codec = str(cfg.get("video_codec") or "auto").lower()
    if codec in ("h264", "h265"):
        args.append("--video-codec=" + codec)
    try:
        max_size = int(str(cfg.get("max_size") or "0").strip() or 0)
    except ValueError:
        max_size = 0
    if max_size > 0:
        args.append("--max-size=%d" % max_size)
    return args

def get_media_volume(serial=None):
    out, _, _ = run_adb(["shell", "media", "volume", "--stream", "3"], timeout=5, serial=serial)
    try:
        return int(out.strip().split()[-1])
    except Exception:
        return -1

def set_media_volume(vol, serial=None):
    run_adb(["shell", "media", "volume", "--stream", "3", "--set", str(vol)],
            timeout=5, serial=serial)

# ============ 锁屏解锁 ============
# 手机锁着的时候投出来的就是锁屏画面，还得在电脑上手滑一次才看得到内容。密码由用户事先
# 在设置里按手机填好，存在 EXE 数据流里而不是 config.json——配置文件是会被随手发出去的
# 东西，不该带密码。主页按设备显示当前锁屏状态并提供「解锁」按钮：点了才在后台静默
# 上滑 + 输密码，投屏 / 启动应用本身不再自动动手机。
# 只支持数字 PIN / 字母数字密码：图案锁没法用 input 模拟，指纹/人脸更不行，那两种情况下
# 用户仍会看到锁屏，与没有此功能时一致。
_UNLOCK_XOR = b"kuaitou"        # 只求密码别明文躺在数据流里，不是加密，也不假装是
_UNLOCK_SETTLE = 0.8            # 上滑动画 / 输入法弹出的等待时间
_UNLOCK_HINTS = ("mDreamingLockscreen", "mShowingLockscreen")
_DEFAULT_SCREEN = (1080, 2400)  # 取不到 wm size 时的兜底分辨率，够 swipe 用
_LOCK_CACHE_TTL = 5.0           # 锁屏状态缓存秒数：界面 3 秒一轮询，dumpsys window 输出很大
_UNLOCK_VERIFY_ROUNDS = 5       # 解锁后复查锁屏标志的轮数
_UNLOCK_VERIFY_WAIT = 0.8       # 每轮复查的间隔：vivo 等 ROM 的标志翻转会晚一拍

def _pin_scramble(raw):
    return bytes(b ^ _UNLOCK_XOR[i % len(_UNLOCK_XOR)] for i, b in enumerate(raw))

def _pins_map():
    """读回 {设备键: 密码}。没设置过或数据损坏都返回空表。"""
    raw = storage_read(UNLOCK_PINS_STREAM)
    if not raw:
        return {}
    try:
        saved = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(saved, dict):
        return {}
    pins = {}
    for key, blob in saved.items():
        if not isinstance(blob, str):
            continue
        try:
            pins[key] = _pin_scramble(base64.b64decode(blob.strip())).decode("utf-8", "replace")
        except Exception:
            continue
    return pins

def _save_pins_map(pins):
    """整表回写；一台设备都没有密码时把数据流删掉，不留空壳。"""
    if not pins:
        storage_delete(UNLOCK_PINS_STREAM)
        return True
    data = {k: base64.b64encode(_pin_scramble(v.encode("utf-8"))).decode("ascii")
            for k, v in pins.items()}
    return bool(storage_write(UNLOCK_PINS_STREAM, json.dumps(data, ensure_ascii=False))[0])

_device_key_lock = threading.Lock()
_device_key_cache = {}          # adb 序列号 -> 身份键，免去每次轮询都跑一次 adb

def device_key(serial=None):
    """设备的稳定身份键：优先手机硬件序列号 ro.serialno。

    同一台手机 USB 与无线的 adb 序列号不同（V2324A / 172.19.163.3:5555），用硬件序列号
    做键，两种接法才能共用同一份锁屏密码。取不到时退回 adb 传输序列号。
    """
    cache_key = serial or ""
    with _device_key_lock:
        if cache_key in _device_key_cache:
            return _device_key_cache[cache_key]
    out, _, code = run_adb(["shell", "getprop", "ro.serialno"], timeout=8, serial=serial)
    sn = (out.strip().splitlines() or [""])[0].strip()
    key = sn if (code == 0 and sn) else cache_key
    with _device_key_lock:
        _device_key_cache[cache_key] = key
    return key

def get_unlock_pin(serial=None):
    """取这台设备已保存的解锁密码；没设过或数据损坏都返回空串（= 不启用解锁）。"""
    key = device_key(serial)
    return _pins_map().get(key, "") if key else ""

def set_unlock_pin(serial, pin):
    """保存某台设备的解锁密码；传空串只关掉这台设备，其它设备的密码保留。"""
    key = device_key(serial)
    if not key:
        return False
    pins = _pins_map()
    pin = (pin or "").strip()
    if pin:
        pins[key] = pin
    else:
        pins.pop(key, None)
    return _save_pins_map(pins)

_lock_cache_lock = threading.Lock()
_lock_cache = {}                # adb 序列号 -> (时间戳, 是否锁屏)

def screen_locked(serial=None, force=False):
    """按 dumpsys window 里的锁屏标志判断屏幕是否锁着。取不到标志时一律当作未锁。

    ROM 差异（标志名不存在）和命令失败都归到这条路径：宁可不解锁，也不能在不确定
    的状态下往手机上乱敲密码——那可能把密码打进某个聊天窗口里。
    结果按设备缓存几秒：dumpsys window 输出很大，而界面每 3 秒就轮询一次状态。
    force=True 跳过缓存，用于解锁后立刻复查。
    """
    cache_key = serial or ""
    now = time.time()
    with _lock_cache_lock:
        hit = _lock_cache.get(cache_key)
        if hit and not force and now - hit[0] < _LOCK_CACHE_TTL:
            return hit[1]
    out, _, code = run_adb(["shell", "dumpsys", "window"], timeout=10, serial=serial)
    locked = False
    if code == 0 and out:
        for hint in _UNLOCK_HINTS:
            m = re.search(hint + r"=(\w+)", out)
            if m:
                locked = m.group(1) == "true"
                break
    with _lock_cache_lock:
        _lock_cache[cache_key] = (now, locked)
    return locked

def _screen_size(serial=None):
    out, _, _ = run_adb(["shell", "wm", "size"], timeout=8, serial=serial)
    m = re.search(r"(\d+)x(\d+)", out or "")
    return (int(m.group(1)), int(m.group(2))) if m else _DEFAULT_SCREEN

def _swipe_and_type_pin(pin, serial=None):
    """上滑叫出密码输入框，把密码打进去再回车。"""
    w, h = _screen_size(serial)
    x = w // 2
    run_adb(["shell", "input", "swipe", str(x), str(int(h * 0.8)),
             str(x), str(int(h * 0.25)), "200"], timeout=8, serial=serial)
    time.sleep(_UNLOCK_SETTLE)
    run_adb(["shell", "input", "text", pin], timeout=8, serial=serial)
    time.sleep(0.3)
    run_adb(["shell", "input", "keyevent", "66"], timeout=8, serial=serial)   # 66 = 回车
    time.sleep(_UNLOCK_SETTLE)

def unlock_now(serial=None):
    """在后台静默解锁这台设备的屏幕，返回 {ok, msg} 供界面提示。

    主页「解锁」按钮走这里。没设密码就不碰屏幕，只回一条说明；屏幕本来就开着只回状态。
    解锁后复查锁屏标志：vivo 这类 ROM 的标志翻转会晚一拍，所以多给几轮复查，别把
    「其实已经解开、只是标志还没跟上」误报成解锁失败。任何一步出错都只记结果，不抛异常。
    """
    try:
        pin = get_unlock_pin(serial)
        if not pin:
            return {"ok": False,
                    "msg": "这台设备还没设置锁屏密码：请到「设置 → 设备与连接 → 锁屏解锁」里填写"}
        if not screen_locked(serial):
            return {"ok": True, "msg": "屏幕已经是解锁状态"}
        _swipe_and_type_pin(pin, serial)
        for _ in range(_UNLOCK_VERIFY_ROUNDS):
            if not screen_locked(serial, force=True):
                return {"ok": True, "msg": "已解锁手机屏幕"}
            time.sleep(_UNLOCK_VERIFY_WAIT)
        return {"ok": False,
                "msg": "已发送解锁指令，但手机仍报告锁屏：请核对密码是否与手机一致"
                       "（图案锁 / 指纹 / 人脸无法自动解锁）"}
    except Exception as e:
        return {"ok": False, "msg": "解锁出错：%s" % e}

# 同一台设备上的同一个应用只保留一个投屏窗口：手机端没有应用多开，重复启动要么报错
# 要么把已有的那个顶掉。所以再次点同一个应用时不再拉起 scrcpy，直接把已有窗口唤到前台。
# 只记"应用镜像"，镜像桌面不在此列。
_mirror_procs = {}          # {(序列号, 包名): Popen}
_mirror_lock = threading.Lock()

def launch_app(pkg, serial=None):
    key = (serial or "", pkg)
    with _mirror_lock:
        old = _mirror_procs.get(key)
        if old is not None and old.poll() is None:
            focus_proc_window(old)      # 已在投屏：唤到前台就够了
            return None, True

    cfg = load_config()
    audio_mode = cfg.get("audio_mode", "both")
    old_vol = -1

    if audio_mode == "pc":
        old_vol = get_media_volume(serial)
        if old_vol >= 0:
            set_media_volume(0, serial)

    cmd = build_scrcpy_cmd(pkg=pkg, serial=serial)
    proc = _spawn(cmd, tag="镜像应用 %s @ %s" % (pkg, serial or "-"))
    with _mirror_lock:
        _mirror_procs[key] = proc

    def wait_and_kill():
        nudge_scrcpy_window(proc)
        proc.wait()
        with _mirror_lock:
            if _mirror_procs.get(key) is proc:
                _mirror_procs.pop(key, None)
        _close_child_log(proc.pid)
        time.sleep(1.5)
        try:
            subprocess.run([ADB_PATH] + _serial_args(serial) + ["shell", "am", "force-stop", pkg],
                          capture_output=True, timeout=5,
                          encoding="utf-8", errors="replace",
                          startupinfo=get_startupinfo(), creationflags=0x08000000)
        except Exception:
            pass
        if old_vol >= 0:
            time.sleep(0.5)
            set_media_volume(old_vol, serial)
    threading.Thread(target=wait_and_kill, daemon=True).start()
    return proc, False

def launch_desktop(serial=None):
    cfg = load_config()
    audio_mode = cfg.get("audio_mode", "both")

    cmd = ([SCRCPY_PATH] + _serial_args(serial)
           + ["--stay-awake", "--window-x=600", "--window-y=50"]
           + _stream_args(cfg) + _video_args(cfg))
    old_vol = -1
    if audio_mode == "phone":
        cmd.append("--no-audio")
    elif audio_mode == "pc":
        old_vol = get_media_volume(serial)
        if old_vol >= 0:
            set_media_volume(0, serial)

    proc = _spawn(cmd, tag="镜像桌面 @ %s" % (serial or "-"))
    def wait_restore():
        proc.wait()
        _close_child_log(proc.pid)
        if old_vol >= 0:
            time.sleep(1)
            set_media_volume(old_vol, serial)
    threading.Thread(target=wait_restore, daemon=True).start()
    return proc

def _find_window_by_pid(pid):
    """按 PID 找该进程的第一个可见顶层窗口（scrcpy 只开一个主窗口）。"""
    try:
        user32 = ctypes.windll.user32
        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        found = []

        def callback(hwnd, _lparam):
            win_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            if win_pid.value == pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(enum_proc_type(callback), 0)
        return found[0] if found else None
    except Exception:
        return None

def focus_proc_window(proc):
    """把投屏窗口从最小化 / 别的窗口后面唤到前台。返回是否找到并唤起。"""
    hwnd = _find_window_by_pid(proc.pid)
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.AllowSetForegroundWindow(0xFFFFFFFF)   # ASFW_ANY：否则只能抢到任务栏闪烁
        user32.ShowWindow(hwnd, 9)                    # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False

def nudge_scrcpy_window(proc, timeout=15):
    """应用投屏成功启动后，将 scrcpy 窗口宽高各增大 1 像素，强制窗口/渲染重新布局。

    按 scrcpy 进程 PID 查找其可见窗口；进程提前退出（启动失败）或超时未出现
    窗口则放弃。返回 True 表示已调整。
    """
    try:
        user32 = ctypes.windll.user32

        deadline = time.time() + timeout
        hwnd = None
        while time.time() < deadline:
            if proc.poll() is not None:
                return False  # scrcpy 已退出，视为启动失败
            hwnd = _find_window_by_pid(proc.pid)
            if hwnd:
                break
            time.sleep(0.3)
        if not hwnd:
            return False

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(
            hwnd, 0, rect.left, rect.top,
            width + 1, height + 1,
            SWP_NOZORDER | SWP_NOACTIVATE
        )
        return True
    except Exception:
        return False

# 本进程拉起的子进程（投屏的 scrcpy 等）：关闭应用时要一起结束，
# 否则会出现"界面关了，投屏窗口和 adb 进程还在后台跑"。
_child_procs = set()
_child_logs = {}
_child_lock = threading.Lock()

def _spawn(cmd, tag="scrcpy"):
    """拉起 scrcpy，并把它的标准输出/错误重定向到日志文件：
    打包后没有控制台，投屏在别人的电脑上起不来时只能靠这份日志定位原因。"""
    log = storage_open_append(LAUNCH_LOG_STREAM)
    if log:
        try:
            log.write("\n===== %s | %s =====\n%s\n"
                      % (time.strftime("%Y-%m-%d %H:%M:%S"), tag,
                         subprocess.list2cmdline(cmd)))
            log.flush()
        except Exception:
            pass
    p = subprocess.Popen(cmd, cwd=os.path.dirname(SCRCPY_PATH),
                         startupinfo=get_startupinfo(), creationflags=0x08000000,
                         stdout=(log or subprocess.DEVNULL),
                         stderr=(subprocess.STDOUT if log else subprocess.DEVNULL))
    with _child_lock:
        _child_procs.add(p)
        if log:
            _child_logs[p.pid] = log
    return p

def _close_child_log(pid):
    with _child_lock:
        f = _child_logs.pop(pid, None)
    if f:
        try:
            f.close()
        except Exception:
            pass

def _kill_children():
    with _child_lock:
        procs = list(_child_procs)
        _child_procs.clear()
    with _mirror_lock:
        _mirror_procs.clear()       # 进程随下面一起结束，登记表一并清空
    for p in procs:
        if p.poll() is None:
            try:
                # /t：连同它拉起的 adb 等子进程一起结束
                subprocess.run(["taskkill", "/f", "/t", "/pid", str(p.pid)],
                               capture_output=True, timeout=5,
                               startupinfo=get_startupinfo(), creationflags=0x08000000)
            except Exception:
                pass
        _close_child_log(p.pid)

def _adb_server_running():
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", 5037)) == 0
    finally:
        s.close()

_adb_server_ours = False   # 由入口在首次调用 adb 之前经 note_adb_server_before_start() 置位

def cleanup_scrcpy():
    try:
        subprocess.run(["taskkill", "/f", "/im", "scrcpy.exe"],
                      capture_output=True, startupinfo=get_startupinfo(), creationflags=0x08000000)
    except Exception:
        pass

def shutdown_all():
    """关闭应用时统一收尾：投屏进程、本进程拉起的子进程，以及本次由我们启动的 adb 服务。
    托盘图标由上层（system）负责收尾——托盘是界面层的东西，不该由本模块反向依赖。"""
    cleanup_scrcpy()
    _kill_children()
    if _adb_server_ours:
        # 只关我们自己拉起来的 adb 服务，避免误杀 Android Studio 等正在用的 adb
        try:
            subprocess.run([ADB_PATH, "kill-server"], capture_output=True, timeout=5,
                           startupinfo=get_startupinfo(), creationflags=0x08000000)
        except Exception:
            pass

def _launch_result(proc, seconds=3.0):
    """投屏进程若几秒内就退出，说明它没起来。把日志尾部返回给界面，
    让用户直接看到 scrcpy 的真实报错，而不是"点了没反应"。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _read_log_tail(LAUNCH_LOG_STREAM).strip()
            return {"ok": False,
                    "error": tail or ("scrcpy 启动后立即退出（返回码 %s）" % proc.returncode)}
        time.sleep(0.1)
    return {"ok": True}


# ---------- 供入口（launcher_server.py）调用的接口 ----------
# adb 服务的归属只在进程启动时确定一次，用函数而不是 from ... import 取值，
# 否则拿到的会是被赋值之前的那份快照。
def note_adb_server_before_start():
    """首次调用 adb 之前调用：记下 adb 服务是不是本次由我们拉起来的，
    退出时只关我们自己拉起来的那个，不误杀 Android Studio 等正在用的 adb。"""
    global _adb_server_ours
    _adb_server_ours = not _adb_server_running()

def adb_server_ours():
    return _adb_server_ours
