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

from . import winbar
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

def build_scrcpy_cmd(pkg=None, serial=None, title=None):
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
    ]
    cmd += _window_args(cfg) + _stream_args(cfg) + _video_args(cfg)
    if cfg.get("always_on_top"):
        cmd.append("--always-on-top")
    if audio_mode == "phone":
        cmd.append("--no-audio")

    if pkg:
        if title:
            # 标题栏写目标应用的名字：默认那个 "scrcpy" 看不出投的是哪个应用
            cmd.append("--window-title=" + title)
        cmd.append(f"--new-display={w}x{h}/{dpi}")
        cmd.append("--no-vd-system-decorations")   # 虚拟屏里不画状态栏/导航栏
        cmd.append(f"--start-app={pkg}")
    return cmd

def _window_args(cfg):
    """投屏窗口的初始位置：沿用上次关窗口时记下的坐标，没有就交给 scrcpy 自己挑（auto）。

    以前这里写死 --window-x=600 --window-y=50：用户把窗口挪走，下次投屏又跳回原处；
    小屏或改了显示器布局的电脑上，那个坐标还可能正好落在屏幕外面。
    配置里存的是逻辑坐标（Windows 报给我们的那套），scrcpy 要的是物理像素，这里换算。
    """
    pos = _saved_window_pos(cfg)
    if not pos:
        return []
    scale = _screen_scale()
    return ["--window-x=%d" % int(round(pos[0] * scale)),
            "--window-y=%d" % int(round(pos[1] * scale))]

def _screen_scale():
    """物理像素 ÷ 逻辑像素（Windows 显示缩放比例）。

    本进程不是 DPI 感知的，Windows 会把屏幕尺寸和窗口坐标都按缩放比例虚拟化后再给我们，
    而 scrcpy 摆窗口用的是物理像素。所以存位置用逻辑坐标、用位置时换算成物理像素；
    反过来的话（存物理），200% 缩放下每次记录都要经过一次取整，位置会一趟趟往右下漂。
    不改进程的 DPI 感知：那会把整套界面尺寸和 pywebview 窗口一起卷进来，不值得。
    """
    try:
        gdi32 = ctypes.windll.gdi32
        hdc = gdi32.CreateDCW("DISPLAY", None, None, None)
        try:
            phys = gdi32.GetDeviceCaps(hdc, 118)        # DESKTOPHORZRES：物理宽度
        finally:
            gdi32.DeleteDC(hdc)
        logical = ctypes.windll.user32.GetSystemMetrics(0)
        if phys > 0 and logical > 0:
            return phys / float(logical)
    except Exception:
        pass
    return 1.0

def _window_client_pos(hwnd):
    """窗口客户区左上角的位置（本进程的逻辑坐标）。

    取客户区而不是整窗矩形：SDL 设的就是客户区位置，存客户区才能原样还原，不会每次
    都往左上偏一个边框的宽度。也用不着换算缩放：Windows 报给我们的就是逻辑坐标，
    存它，下次乘回缩放比例，一来一回刚好对上。
    """
    try:
        user32 = ctypes.windll.user32
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        return int(pt.x), int(pt.y)
    except Exception:
        return None

def _saved_window_pos(cfg):
    try:
        x = int(str(cfg.get("window_x", "")).strip())
        y = int(str(cfg.get("window_y", "")).strip())
    except (TypeError, ValueError):
        return None
    return (x, y) if _pos_reachable(x, y) else None

def _pos_reachable(x, y):
    """这个坐标是否还落在可见的虚拟桌面上（多显示器合起来算一块，负坐标也算数）。

    存的是上次投屏时的位置：换过显示器 / 改过分辨率之后，旧坐标可能已经在屏幕之外，
    再沿用就等于「投屏窗口打开了却看不见」。留一点余量，方便用户拖回来。

    现在窗口带原生标题栏，标题栏算在窗口矩形内部，上沿不需要再额外让出高度；只要求
    标题栏那一条（也就是窗口左上角）还落在桌面范围内，用户就能拖回来。
    """
    try:
        user32 = ctypes.windll.user32
        # SM_XVIRTUALSCREEN / YVIRTUALSCREEN / CXVIRTUALSCREEN / CYVIRTUALSCREEN
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        if vw <= 0 or vh <= 0:
            return False
        return ((vx - 40 <= x <= vx + vw - 60)
                and (vy - 10 <= y <= vy + vh - 30))
    except Exception:
        return False

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

_VOLUME_STREAM = "3"        # STREAM_MUSIC：投屏抓的就是这一路
_VOLUME_STEP_KEYS = 30      # 一轮最多按这么多次，按不动就收手
_VOLUME_SETTLE = 0.4        # 按完等一下再读：音频服务记下新值有一点点延迟
# 投屏起来后复查音量的时间点（秒，相对投屏启动）。真机实测：镜像一开，音频通路切换，
# ROM 会把媒体音量按「输出设备的记忆值」拉回去（70 → 40 都见过），所以不能只拉一次。
_BOOST_RECHECKS = (3.0, 5.0, 7.0)

def _parse_volume(out):
    """从 `volume is 7 in range [0..15]` 里抠出 (当前值, 上限)。

    必须认这句而不是"把输出里的数字都抓出来"：`cmd media_session` 会先回显
    `[V] will control stream=3 (STREAM_MUSIC)`，里面的 3 会被误当成音量。
    """
    m = re.search(r"volume is (\d+)\s+in range \[(\d+)\.\.(\d+)\]", out or "")
    if m:
        return int(m.group(1)), int(m.group(3))
    m = re.match(r"\s*(\d+)\s*$", out or "")
    return (int(m.group(1)), -1) if m else (-1, -1)

def _parse_dumpsys_volume(out):
    """从 `dumpsys audio` 里抠 STREAM_MUSIC 的 (当前值, 上限)。

    兜底用：`cmd media_session volume --get` 在某些 ROM 上会漏报或报旧值，
    dumpsys 里那份是音频服务自己的状态（`STREAM_MUSIC: ... Max: 150, streamVolume:75, ...`）。
    """
    for m in re.finditer(r"STREAM_MUSIC:(.{0,400}?)(?=STREAM_[A-Z]|$)", out or "", re.S):
        blk = m.group(1)
        v = re.search(r"streamVolume:(\d+)", blk)
        if not v:
            continue
        top = re.search(r"Max:\s*(\d+)", blk)
        return int(v.group(1)), (int(top.group(1)) if top else -1)
    return -1, -1

def _media_volume(serial=None):
    """读媒体音量 → (当前值, 上限)；读不到给 (-1, -1)。

    Android 16 起 `media` 这条 shell 命令已经没了（真机报 `media: inaccessible or
    not found`），得改用 `cmd media_session`。老实现既只认前者、又按 split()[-1]
    取尾字段（拿到的是 "[0..150]"），两个原因叠在一起，等于从来没读到过音量。
    这里再拿 `dumpsys audio` 兜一层：读数被 ROM 漏报时，后面按音量键的逻辑不会
    误以为「按不动了」而提前收手。
    """
    fallback = (-1, -1)         # 读到了值但没读到上限：先留着，后面能读到更全的就用更全的
    for args in (["shell", "cmd", "media_session", "volume", "--stream", _VOLUME_STREAM, "--get"],
                 ["shell", "media", "volume", "--stream", _VOLUME_STREAM, "--get"]):
        out, _, code = run_adb(args, timeout=6, serial=serial)
        if code == 0:
            cur, top = _parse_volume(out)
            if cur >= 0 and top > 0:
                return cur, top
            if cur >= 0 and fallback[0] < 0:
                fallback = (cur, top)
    out, _, code = run_adb(["shell", "dumpsys", "audio"], timeout=15, serial=serial)
    if code == 0:
        cur, top = _parse_dumpsys_volume(out)
        if cur >= 0 and top > 0:
            return cur, top
    return fallback

def get_media_volume(serial=None):
    return _media_volume(serial)[0]

def _log_volume(msg):
    """音量处理结果写进投屏日志：窗口程序没有控制台，用户报「没调到最大」时只能靠它复盘。"""
    storage_write(LAUNCH_LOG_STREAM,
                  "\n[volume %s] %s\n" % (time.strftime("%H:%M:%S"), msg), append=True)

def _volume_after_key(key, serial, prev):
    """按一下音量键并读回新值；读到没变或读不到就再等等重读一次。

    读数滞后会让人误判成「按不动了」，从而在离目标还很远时就收手——这正是
    「选了仅电脑播放，音量却没调上去」的常见由来，所以这里宁可多读一次。
    """
    run_adb(["shell", "input", "keyevent", key], timeout=6, serial=serial)
    cur = -1
    for _ in range(2):
        time.sleep(_VOLUME_SETTLE)
        cur = _media_volume(serial)[0]
        if cur >= 0 and cur != prev:
            return cur
    return cur

def _press_volume_to(vol, cur, serial=None):
    """把媒体音量挪到 vol，返回是否真的到位。

    一下一下按太慢：真机一次按键往返约半秒，0→150 要按十几下。改成先按一下量出步进
    （各家 ROM 是 10 还是 15 不一样），再一口气把剩下的按完，最后读回核对；核不上就
    按量出的步进再补一轮，不轻言放弃。
    """
    if cur < 0:
        return False
    for _ in range(3):
        if cur == vol:
            return True
        key = "24" if vol > cur else "25"     # 24 = 音量加，25 = 音量减；每轮按当前值重算方向
        before = cur
        cur = _volume_after_key(key, serial, before)
        step = cur - before
        if cur < 0 or step == 0 or (step > 0) != (key == "24"):
            return False            # 到头了，或者被 ROM / 前台的播放器顶回来了
        step = abs(step)
        # 向下取整：宁可最后差一格，也别按过头。原值不是步进整数倍时（真机 150 档、步进 10，
        # 用户原值可能是 36 这种）目标本身就按不到，向上取整会直接冲过去（36 → 0 见过）。
        need = min(abs(vol - cur) // step, _VOLUME_STEP_KEYS)
        if need:
            run_adb(["shell", "input", "keyevent"] + [key] * need,
                    timeout=10 + need, serial=serial)
        time.sleep(_VOLUME_SETTLE)
        cur = _media_volume(serial)[0]
    return cur == vol

def set_media_volume(vol, serial=None):
    """把媒体音量设成 vol，返回是否真的设成了。

    真机实测（Android 16 / vivo）：`cmd media_session volume --set` 会在日志里留下
    `setStreamVolume(index:...)` 却立刻被 ROM 回滚，只有音量键是真的生效。所以先试
    命令，读回来核对；没变就退回音量键，一步步按过去。
    """
    try:
        run_adb(["shell", "cmd", "media_session", "volume", "--stream", _VOLUME_STREAM,
                 "--set", str(vol)], timeout=6, serial=serial)
        time.sleep(0.2)
        cur = _media_volume(serial)[0]
        if cur == vol:
            return True
        ok = _press_volume_to(vol, cur, serial)
        if not ok:
            # 读数被 ROM 拖着不更新时，上面会误判成没到位。以最终读回值再确认一次，
            # 免得明明拉满了还去还原。
            ok = _media_volume(serial)[0] == vol
        return ok
    except Exception:
        return False

# 「仅电脑播放」时把手机音量拉满的登记表：序列号 -> 拉满之前用户自己的音量。
# 和 _screen_off_held 一个道理，改了用户的东西就得记着还回去。
_boosted_volume = {}
_boosted_volume_lock = threading.Lock()
_volume_ops = {}                # 序列号 -> 锁：拉满 / 投屏后复查 / 还原 三件事不许互相插队
_volume_ops_lock = threading.Lock()

def _volume_op_lock(key):
    with _volume_ops_lock:
        lk = _volume_ops.get(key)
        if lk is None:
            lk = _volume_ops[key] = threading.Lock()
        return lk

def boost_media_volume(serial=None):
    """把手机媒体音量拉到最大，返回原值；不需要改（已最大 / 读不到 / 改不动）时返回 None。

    真机反馈：选了「仅电脑播放」后电脑这边没声或很小。scrcpy 抓的就是手机输出的
    那路声音，手机音量压着，抓到的信号自然也是压着的，所以要拉满；关窗口时还原。
    """
    key = serial or ""
    try:
        with _volume_op_lock(key):
            cur, top = _media_volume(serial)
            if cur < 0 or top <= 0:
                _log_volume("读不到媒体音量，跳过拉满（serial=%s）" % (serial or "-"))
                return None
            if cur >= top:
                _log_volume("媒体音量本来就在最大（%d/%d），不用动" % (cur, top))
                return None
            with _boosted_volume_lock:
                if key in _boosted_volume:
                    return _boosted_volume[key]   # 这台已经拉满过，别把拉满后的值当成原值记第二次
            if not set_media_volume(top, serial):
                got = _media_volume(serial)[0]
                _log_volume("拉满失败：%d → %d，目标是 %d（serial=%s）"
                            % (cur, got, top, serial or "-"))
                return None                     # 没真拉上去就别登记，免得还原时去动用户的音量
            with _boosted_volume_lock:
                _boosted_volume[key] = cur
            _log_volume("已拉满：%d → %d（关窗口后还原成 %d；serial=%s）"
                        % (cur, top, cur, serial or "-"))
            return cur
    except Exception as e:
        _log_volume("拉满出错：%r" % (e,))
        return None

def recheck_boost_volume(serial=None):
    """投屏起来之后按 _BOOST_RECHECKS 排的时间点复查几遍：有的 ROM 会在音频通路切换时把音量拉回去。

    真机实测（vivo / Android 16）：镜像一开，音频通路切到新输出，ROM 会按「输出设备的记忆值」
    把媒体音量拉回去（70 → 40 都见过），所以只在拉起前拉一次不够，要盯着补几遍。每遍都先确认
    「这台还在拉满名单里」才动手，免得窗口已经关了还去改用户自己的音量。跑在后台线程里，
    调用方不用等它。
    """
    def run():
        key = serial or ""
        lock = _volume_op_lock(key)
        started = time.time()
        for at in _BOOST_RECHECKS:
            time.sleep(max(0.0, at - (time.time() - started)))
            with _boosted_volume_lock:
                if key not in _boosted_volume:
                    return                  # 已经还原过（窗口关了），别再往回拉
            if not lock.acquire(blocking=False):
                continue                    # 正有拉满 / 还原在跑，这一轮别插队
            try:
                with _boosted_volume_lock:
                    if key not in _boosted_volume:
                        return
                cur, top = _media_volume(serial)
                if 0 <= cur < top:
                    if set_media_volume(top, serial):
                        _log_volume("投屏后复查：音量被拉回到 %d，已重新拉满到 %d（serial=%s）"
                                    % (cur, top, serial or "-"))
                    else:
                        _log_volume("投屏后复查：音量是 %d（未满 %d），再拉也没拉动（serial=%s）"
                                    % (cur, top, serial or "-"))
            except Exception:
                pass
            finally:
                lock.release()

    threading.Thread(target=run, daemon=True).start()

def restore_media_volume(serial=None):
    """把拉满的音量还给用户：投屏窗口关闭时调。

    认的是启动时那台设备，所以 key 用 serial or ""——与「传 None 表示全部还原」区分开。
    整段拿设备锁：不能和投屏后的复查交叉，否则复查会把还原好的音量又拉满。
    """
    key = serial or ""
    with _volume_op_lock(key):
        with _boosted_volume_lock:
            orig = _boosted_volume.pop(key, None)
        if orig is not None:
            set_media_volume(orig, serial)
            # 记下实际读回值：音量档位多半只能按步进走，原值又未必是步进的整数倍，可能差一格
            _log_volume("已还原成 %d（实际 %d；serial=%s）" % (orig, _media_volume(serial)[0], serial or "-"))

def restore_all_media_volume():
    """全部还原：退出应用时调（关窗走的 os._exit，等着投屏线程收尾来不及）。"""
    with _boosted_volume_lock:
        targets = list(_boosted_volume.items())
        _boosted_volume.clear()
    for key, orig in targets:
        with _volume_op_lock(key):
            set_media_volume(orig, key or None)

# ============ 锁屏解锁 / 熄屏 ============
# 手机锁着的时候投出来的就是锁屏画面，还得在电脑上手滑一次才看得到内容。密码由用户事先
# 在设置里按手机填好，存在 EXE 数据流里而不是 config.json——配置文件是会被随手发出去的
# 东西，不该带密码。顶栏（跟随当前设备）显示锁屏状态并提供「解锁」按钮：点了才在后台
# 静默点亮屏幕 + 上滑 + 输密码，投屏 / 启动应用本身不再自动动手机。
# 只支持数字 PIN / 字母数字密码：图案锁没法用 input 模拟，指纹/人脸更不行，那两种情况下
# 用户仍会看到锁屏，与没有此功能时一致。
# 另有「关闭物理屏幕」：按电源键熄屏，但临时顶住系统的「熄屏后自动锁定」，这样手机只是
# 黑屏、不会顺手锁上（投屏时把手机屏关掉省电，同时不用每次重新解锁）。
_UNLOCK_XOR = b"kuaitou"        # 只求密码别明文躺在数据流里，不是加密，也不假装是
_UNLOCK_SETTLE = 0.8            # 上滑动画 / 输入法弹出的等待时间
_UNLOCK_WAKE_SETTLE = 0.6       # 点亮屏幕后等锁屏界面画出来，再去上滑
_UNLOCK_HINTS = ("mDreamingLockscreen", "mShowingLockscreen")
_DEFAULT_SCREEN = (1080, 2400)  # 取不到 wm size 时的兜底分辨率，够 swipe 用
_LOCK_CACHE_TTL = 5.0           # 锁屏状态缓存秒数：界面 3 秒一轮询，dumpsys window 输出很大
_UNLOCK_VERIFY_ROUNDS = 5       # 解锁后复查锁屏标志的轮数
_UNLOCK_VERIFY_WAIT = 0.8       # 每轮复查的间隔：vivo 等 ROM 的标志翻转会晚一拍
_SDK_ANDROID_15 = 35            # Android 15 = API 35：从这版起才有 cmd display power-off
_LOCK_TIMEOUT_KEY = "lock_screen_lock_after_timeout"   # 熄屏后多久自动锁定（毫秒）
_SCREEN_OFF_KEEP_MS = 86400000  # 老系统的退路：熄屏前把上面那条临时顶成一天
_SCREEN_OFF_SETTLE = 0.9        # 发完熄屏指令后等系统状态更新
_SCREEN_OFF_ROUNDS = 3          # 复查熄屏是否生效的轮数（vivo 等 ROM 状态更新晚一拍）
_SCREEN_OFF_WAIT = 0.6          # 每轮复查的间隔

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

def wake_screen(serial=None):
    """点亮手机屏幕，并把之前临时顶住的「熄屏后自动锁定」还回去。

    熄屏状态下锁屏标志还在，但上滑和输入都没有落点，必须先叫醒屏幕再动手。
    """
    release_lock_timeout(serial)
    # Android 15+ 关屏时是直接关的显示电源，得用对应的 power-on 叫回来；
    # 老系统没这条命令（会报错，忽略即可），补一发 KEYCODE_WAKEUP 就够。
    run_adb(["shell", "cmd", "display", "power-on", "0"], timeout=8, serial=serial)
    run_adb(["shell", "input", "keyevent", "224"], timeout=8, serial=serial)

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

    顶栏「解锁」按钮走这里。没设密码就不碰屏幕，只回一条说明；先点亮屏幕再复查锁屏状态
    ——熄屏时上滑 / 输密码都落不到锁屏界面上，那是真机反馈过的失败原因之一。
    解锁后复查锁屏标志：vivo 这类 ROM 的标志翻转会晚一拍，所以多给几轮复查，别把
    「其实已经解开、只是标志还没跟上」误报成解锁失败。任何一步出错都只记结果，不抛异常。
    """
    try:
        pin = get_unlock_pin(serial)
        if not pin:
            return {"ok": False,
                    "msg": "这台设备还没设置锁屏密码：请到「设置 → 设备与连接 → 锁屏解锁」里填写"}
        wake_screen(serial)
        time.sleep(_UNLOCK_WAKE_SETTLE)
        if not screen_locked(serial, force=True):
            return {"ok": True, "msg": "屏幕已点亮，当前没有锁屏"}
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

def _restore_lock_timeout(serial, original):
    """把「熄屏后自动锁定」恢复成用户原来的值；原来没有这条设置就删掉。"""
    value = (original or "").strip()
    if value and value.lower() != "null":
        run_adb(["shell", "settings", "put", "secure", _LOCK_TIMEOUT_KEY, value],
                timeout=8, serial=serial)
    else:
        run_adb(["shell", "settings", "delete", "secure", _LOCK_TIMEOUT_KEY],
                timeout=8, serial=serial)

_sdk_cache_lock = threading.Lock()
_sdk_cache = {}                 # 序列号 -> API 级别

def _sdk_level(serial=None):
    """设备 API 级别（35 = Android 15）。取不到返回 0，一律按老系统处理。"""
    key = serial or ""
    with _sdk_cache_lock:
        if key in _sdk_cache:
            return _sdk_cache[key]
    out, _, _ = run_adb(["shell", "getprop", "ro.build.version.sdk"], timeout=8, serial=serial)
    try:
        level = int((out or "").strip().splitlines()[0])
    except Exception:
        level = 0
    with _sdk_cache_lock:
        _sdk_cache[key] = level
    return level

def _display_power(action, serial=None):
    """Android 15+ 的 `cmd display power-off|power-on <id>`，成功返回 True。

    这是「关掉显示但不锁定」的正路：显示电源被直接关掉，设备并没有进入睡眠，
    锁屏那套定时器压根不会启动（scrcpy 关屏不锁屏走的也是这条）。
    老系统没有这个命令，会报错，据此回退到电源键那条路。
    """
    out, err, code = run_adb(["shell", "cmd", "display", action, "0"], timeout=8, serial=serial)
    text = (out + err).lower()
    return code == 0 and not any(bad in text for bad in
                                ("error", "exception", "unknown", "not found"))

def _display_screen_state(serial=None):
    """dumpsys display 里的屏幕状态：True=亮着、False=已熄、None=读不到就不判断。

    用 cmd display 关屏时设备并没有睡（mWakefulness 仍是 Awake），所以不能用
    dumpsys power 判断，得看 dumpsys display 的 mScreenState。
    """
    out, _, code = run_adb(["shell", "dumpsys", "display"], timeout=10, serial=serial)
    if code != 0 or not out:
        return None
    m = re.search(r"mScreenState=(\w+)", out)
    return (m.group(1).upper() == "ON") if m else None

def _screen_awake(serial=None):
    """手机是否亮着屏（dumpsys power 的 mWakefulness）。取不到标志返回 None，不做判断。"""
    out, _, code = run_adb(["shell", "dumpsys", "power"], timeout=10, serial=serial)
    if code != 0 or not out:
        return None
    m = re.search(r"mWakefulness=(\w+)", out)
    return m.group(1) == "Awake" if m else None

_screen_off_held = {}           # 序列号 -> 顶住之前用户自己的「熄屏后自动锁定」值
_screen_off_held_lock = threading.Lock()

def _hold_lock_timeout(serial):
    """老系统的退路用：把「熄屏后自动锁定」临时顶成一天，成功返回原值，失败返回 None。

    顶完不能马上还原——有些 ROM 会持续读这条设置，提前还原等于没顶（这正是上一版
    「关了屏还是被锁」的原因）。所以记在 _screen_off_held 里，等下次点亮屏幕或
    退出应用时再还回去。
    """
    with _screen_off_held_lock:
        if serial in _screen_off_held:
            return _screen_off_held[serial]
    orig, _, code = run_adb(["shell", "settings", "get", "secure", _LOCK_TIMEOUT_KEY],
                            timeout=8, serial=serial)
    if code != 0:
        return None
    _, _, put_code = run_adb(["shell", "settings", "put", "secure", _LOCK_TIMEOUT_KEY,
                              str(_SCREEN_OFF_KEEP_MS)], timeout=8, serial=serial)
    if put_code != 0:
        return None
    saved = (orig or "").strip()
    with _screen_off_held_lock:
        _screen_off_held[serial] = saved
    return saved

def release_lock_timeout(serial=None):
    """把顶住的「熄屏后自动锁定」还回原值：点亮屏幕时、退出应用时都要调。"""
    with _screen_off_held_lock:
        if serial is None:
            targets = list(_screen_off_held.items())
            _screen_off_held.clear()
        elif serial in _screen_off_held:
            targets = [(serial, _screen_off_held.pop(serial))]
        else:
            targets = []
    for dev, orig in targets:
        _restore_lock_timeout(dev, orig)

def _screen_off_by_power_key(serial=None):
    """老系统的退路：顶住「自动锁定」→ 按电源键 → 复查是否真的熄屏。

    这条路成不成取决于 ROM 认不认那条设置（vivo 就未必认），所以失败要照实说。
    """
    held = _hold_lock_timeout(serial) is not None
    run_adb(["shell", "input", "keyevent", "26"], timeout=8, serial=serial)   # 26 = 电源键
    time.sleep(_SCREEN_OFF_SETTLE)
    awake = None
    for _ in range(_SCREEN_OFF_ROUNDS):
        awake = _screen_awake(serial)
        if awake is not True:
            break
        time.sleep(_SCREEN_OFF_WAIT)
    if awake is True:
        return {"ok": False,
                "msg": "已按电源键，但手机仍报告屏幕亮着：请看一眼手机是否停在需要操作的界面"}
    if held:
        return {"ok": True,
                "msg": "已关闭手机物理屏幕（这台手机走的是「顶住自动锁定」的办法，不会立刻锁屏）"}
    return {"ok": True,
            "msg": "已关闭手机物理屏幕；没能顶住系统的「自动锁定」，如果被锁上请点「解锁」"}

def screen_off(serial=None):
    """关闭手机物理屏幕，但不要让手机顺手锁上，返回 {ok, msg} 供界面提示。

    优选 Android 15+ 的 `cmd display power-off`：只关显示、设备不进睡眠，锁屏的定时器
    不会启动，所以手机不会被锁上。老系统没有这条命令，退回「顶住『熄屏后自动锁定』+
    电源键」，那条路不保证不锁，提示里会说清楚走的是哪条。
    """
    try:
        if _sdk_level(serial) >= _SDK_ANDROID_15 and _display_power("power-off", serial):
            time.sleep(_SCREEN_OFF_SETTLE)
            if _display_screen_state(serial) is True:
                return {"ok": False,
                        "msg": "已发送熄屏指令，但手机仍报告屏幕亮着：请看一眼手机是否停在需要操作的界面"}
            return {"ok": True, "msg": "已关闭手机物理屏幕（只关显示，不会锁定手机）"}
        return _screen_off_by_power_key(serial)
    except Exception as e:
        return {"ok": False, "msg": "关闭屏幕出错：%s" % e}

# 同一台设备上的同一个应用只保留一个投屏窗口：手机端没有应用多开，重复启动要么报错
# 要么把已有的那个顶掉。所以再次点同一个应用时不再拉起 scrcpy，直接把已有窗口唤到前台。
# 只记"应用镜像"，镜像桌面不在此列。
_mirror_procs = {}          # {(序列号, 包名): Popen}
_mirror_lock = threading.Lock()

def launch_app(pkg, serial=None, title=None, icon_path=None):
    key = (serial or "", pkg)
    with _mirror_lock:
        old = _mirror_procs.get(key)
        if old is not None and old.poll() is None:
            focus_proc_window(old)      # 已在投屏：唤到前台就够了
            return None, True

    cfg = load_config()
    audio_mode = cfg.get("audio_mode", "both")

    if audio_mode == "pc":
        # 抓的就是手机输出那路声音：手机音量压着，电脑这边就没声 / 很小，所以先拉满
        boost_media_volume(serial)

    cmd = build_scrcpy_cmd(pkg=pkg, serial=serial, title=title)
    proc = _spawn(cmd, tag="镜像应用 %s @ %s" % (pkg, serial or "-"))
    recheck_boost_volume(serial)        # 投屏起来后回头看音量有没有被 ROM 拉回去
    target = _saved_window_pos(cfg)     # 这次要摆的位置（没有就交给 scrcpy 自己挑）
    with _mirror_lock:
        _mirror_procs[key] = proc

    def wait_and_kill():
        nudge_scrcpy_window(proc)
        # 原生标题栏上的图标与「置顶」按钮由 winbar 叠加
        winbar.attach(proc, icon_path=icon_path,
                      always_on_top=cfg.get("always_on_top"))
        _track_window_pos(proc, target)     # 摆正 + 盯着位置，退出时记下来，下次还开在这儿
        proc.wait()                     # 上面超时回来时兜底，保证等到进程真的结束
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
        restore_media_volume(serial)
    threading.Thread(target=wait_and_kill, daemon=True).start()
    return proc, False

def launch_desktop(serial=None):
    cfg = load_config()
    audio_mode = cfg.get("audio_mode", "both")

    cmd = ([SCRCPY_PATH] + _serial_args(serial)
           + ["--stay-awake", "--window-title=镜像桌面"]
           + _window_args(cfg) + _stream_args(cfg) + _video_args(cfg))
    if cfg.get("always_on_top"):
        cmd.append("--always-on-top")
    if audio_mode == "phone":
        cmd.append("--no-audio")
    elif audio_mode == "pc":
        boost_media_volume(serial)      # 同上：拉满手机音量，关窗口时还原

    proc = _spawn(cmd, tag="镜像桌面 @ %s" % (serial or "-"))
    recheck_boost_volume(serial)        # 投屏起来后回头看音量有没有被 ROM 拉回去
    target = _saved_window_pos(cfg)     # 这次要摆的位置（没有就交给 scrcpy 自己挑）
    def wait_restore():
        winbar.attach(proc, always_on_top=cfg.get("always_on_top"))
        _track_window_pos(proc, target)     # 摆正 + 盯着位置，退出时记下来，下次还开在这儿
        proc.wait()                     # 上面超时回来时兜底，保证等到进程真的结束
        _close_child_log(proc.pid)
        restore_media_volume(serial)
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

def _wait_window(proc, timeout=15):
    """等 scrcpy 的主窗口出现；进程先退出（启动失败）或超时都返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        hwnd = _find_window_by_pid(proc.pid)
        if hwnd:
            return hwnd
        time.sleep(0.3)
    return None

_SWP_NOSIZE, _SWP_NOZORDER, _SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010

def _move_window_client_to(hwnd, target):
    """把窗口摆到指定位置，让客户区左上角正好落在 target（逻辑坐标）上。

    scrcpy 已经按记下的位置开过窗了，这一步只是把那一两像素的偏差抹平：scrcpy 交给 SDL
    的坐标要经过一次边框换算，屏幕缩放不是 100% 时会差个一两像素；每次都按窗口实际位置
    回记的话，窗口会一趟趟往同一侧爬。SetWindowPos 定的是整窗位置，所以先量出客户区相对
    整窗的偏移再减掉。
    """
    try:
        user32 = ctypes.windll.user32
        rect, pt = wintypes.RECT(), wintypes.POINT(0, 0)
        if not (user32.GetWindowRect(hwnd, ctypes.byref(rect))
                and user32.ClientToScreen(hwnd, ctypes.byref(pt))):
            return
        dx, dy = int(pt.x) - rect.left, int(pt.y) - rect.top
        user32.SetWindowPos(hwnd, 0, target[0] - dx, target[1] - dy, 0, 0,
                            _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
    except Exception:
        pass

def _track_window_pos(proc, target=None, timeout=25):
    """投屏期间记下窗口位置，进程退出时把最后的坐标写进配置，下次沿用它开窗。

    等窗口出现了才开始记；最小化 / 最大化时的矩形不代表用户挑的位置，跳过不记。
    target 是这次要摆的位置，窗口一出来就校正过去（用户还没动过，不会跟他抢）。
    """
    hwnd = _wait_window(proc, timeout=timeout)
    if not hwnd:
        return
    if target:
        _move_window_client_to(hwnd, target)
    user32 = ctypes.windll.user32
    last = None
    while proc.poll() is None:
        try:
            if not user32.IsIconic(hwnd) and not user32.IsZoomed(hwnd):
                last = _window_client_pos(hwnd) or last
        except Exception:
            pass
        time.sleep(1.0)
    if last:
        save_config({"window_x": str(last[0]), "window_y": str(last[1])})

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
        hwnd = _wait_window(proc, timeout=timeout)
        if not hwnd:
            return False        # scrcpy 已退出（启动失败）或超时没等到窗口

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
    # 老系统关屏时顶住过「熄屏后自动锁定」，退出前必须还给用户，别把人家的设置留在我们这
    release_lock_timeout(None)
    # 拉满的手机音量同理：关窗会直接 os._exit，等投屏线程收尾来不及，这里先还
    restore_all_media_volume()
    winbar.shutdown()
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
