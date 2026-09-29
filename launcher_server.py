import concurrent.futures
import ctypes
import heapq
import http.server
import io
import json
import locale
import os
import re
import secrets
import selectors
import socket
import string
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from collections import deque
from ctypes import wintypes
from difflib import SequenceMatcher
import webview

# 版本号：显示在设置页底部和诊断报告里。exe 被拷到多台电脑排查问题时，
# 靠它一眼就能确认两边跑的是不是同一个版本。
APP_VERSION = "1.2.0"

# 路径解析：PyInstaller 打包后，随包资源解包到只读临时目录（sys._MEIPASS）；
# 用户数据不再落成散落文件，而是写进 exe 自身的 NTFS 数据流（见下方存储层）。
# 未打包（源码直跑）时宿主文件就是本脚本。
if getattr(sys, "frozen", False):
    RES_DIR = sys._MEIPASS
    DATA_DIR = os.path.dirname(sys.executable)
else:
    RES_DIR = DATA_DIR = os.path.dirname(os.path.abspath(__file__))

ADB_PATH = os.path.join(RES_DIR, "scrcpy", "adb.exe")
SCRCPY_PATH = os.path.join(RES_DIR, "scrcpy", "scrcpy.exe")
BUILTIN_CONFIG_FILE = os.path.join(RES_DIR, "config.json")     # 只读：内置默认配置
ICON_DIR = os.path.join(DATA_DIR, "icons")                     # 兜底：ADS 不可用时的图标目录
_RES_ICON_DIR = os.path.join(RES_DIR, "icons")                 # 只读：内置图标库
# 图标查找顺序：可写目录优先，其次内置图标库（未打包时两者相同，避免重复查找）
ICON_SEARCH_DIRS = ([ICON_DIR] if os.path.normcase(ICON_DIR) == os.path.normcase(_RES_ICON_DIR)
                    else [ICON_DIR, _RES_ICON_DIR])
ICON_EXTS = (".png", ".webp", ".jpg", ".jpeg")

# ---------- 存储层：EXE 自身的 NTFS 备用数据流（ADS）----------
# 便携目标：应用产生的一切文件（配置 / 应用缓存 / 投屏日志 / 抓取的图标 / 错误日志）
# 都写进「快投.exe」自己的数据流里，磁盘上不再散落任何小文件，拷走一个 exe 就带走全部状态。
# 所在卷不是 NTFS（FAT32 / 网络盘 / 无写权限）时 ADS 会失败，此时自动退回 exe 同目录的
# 普通文件，功能不受影响；当前实际存储位置会在诊断报告里说明。
# 实测注意：ADS 路径不支持 os.replace（WinError 87），因此一律直接覆盖写。
if getattr(sys, "frozen", False):
    EXE_PATH = os.path.abspath(sys.executable)
else:
    EXE_PATH = os.path.abspath(__file__)

CONFIG_STREAM = "config.json"                 # 用户配置
APPS_CACHE_STREAM = "apps_cache.json"         # 应用列表缓存
LAUNCH_LOG_STREAM = "scrcpy_launch.log"       # scrcpy 启动/输出日志
SCAN_LOG_STREAM = "apps_scan.log"             # 应用扫描异常日志
ERROR_LOG_STREAM = "快投_错误日志.txt"          # 启动致命错误日志
ICON_INDEX_STREAM = "icon_index.json"         # 已缓存图标的包名索引
ICON_STREAM_PREFIX = "icon_"                  # 图标流：icon_<包名>.webp

_ads_ok = None                                # None=尚未探测；True/False=探测结果

def ads_path(stream):
    return "%s:%s" % (EXE_PATH, stream)

def _ads_usable():
    """宿主文件所在卷是否支持 ADS。不用 fsutil（需要管理员），直接一次读写试错。"""
    global _ads_ok
    if _ads_ok is None:
        probe = ads_path(".ads_probe")
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            _ads_ok = True
            try:
                os.remove(probe)
            except OSError:
                pass
        except Exception:
            _ads_ok = False
    return _ads_ok

def storage_open(path, binary=False, append=False):
    if binary:
        return open(path, "ab" if append else "wb")
    return open(path, "a" if append else "w", encoding="utf-8", errors="replace")

def _fallback_path(stream):
    p = os.path.join(DATA_DIR, stream)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return p

# 单个日志流上限：日志藏在 exe 的数据流里，用户看不见也没法手动清理，
# 长期用会一直涨。排查问题只需要最近一次投屏的记录，所以超限就整个丢弃重来。
MAX_LOG_BYTES = 1024 * 1024

def _rotate_log(path):
    try:
        if os.path.getsize(path) > MAX_LOG_BYTES:
            os.remove(path)
    except OSError:
        pass

def storage_write(stream, data, binary=False, append=False):
    """写入：优先 EXE 数据流，不支持时退回 exe 同目录普通文件。返回 (实际路径, 是否ADS)。"""
    if _ads_usable():
        p = ads_path(stream)
        try:
            with storage_open(p, binary, append) as f:
                f.write(data)
            return p, True
        except Exception:
            pass
    try:
        p = _fallback_path(stream)
        with storage_open(p, binary, append) as f:
            f.write(data)
        return p, False
    except Exception:
        return "", False

def storage_read(stream, binary=False):
    """读取：先试 EXE 数据流，再试 exe 同目录普通文件（兼容旧版本遗留的文件）。"""
    cands = [ads_path(stream)] if _ads_usable() else []
    cands.append(os.path.join(DATA_DIR, stream))
    for p in cands:
        try:
            if binary:
                with open(p, "rb") as f:
                    return f.read()
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            continue
    return None

def storage_open_append(stream, binary=False):
    """以追加方式打开一个长期持有的句柄（供子进程 stdout 重定向）。失败返回 None。"""
    if _ads_usable():
        path = ads_path(stream)
        _rotate_log(path)
        try:
            return storage_open(path, binary, True)
        except Exception:
            pass
    path = _fallback_path(stream)
    _rotate_log(path)
    try:
        return storage_open(path, binary, True)
    except Exception:
        return None

def storage_location():
    """诊断用：当前实际存储位置一览。"""
    kind = ("EXE 数据流（NTFS ADS）" if _ads_usable()
            else "exe 同目录普通文件（所在卷不支持 ADS 或不可写）")
    lines = ["宿主文件: %s" % EXE_PATH,
             "数据流可用: %s" % ("是" if _ads_usable() else "否"),
             "实际存储方式: %s" % kind]
    for stream in (CONFIG_STREAM, APPS_CACHE_STREAM, LAUNCH_LOG_STREAM,
                   ICON_INDEX_STREAM, SCAN_LOG_STREAM, ERROR_LOG_STREAM):
        p = ads_path(stream)
        try:
            n = os.path.getsize(p)
            lines.append("  [ADS] %-22s %d 字节" % (stream, n))
        except Exception:
            fp = os.path.join(DATA_DIR, stream)
            if os.path.exists(fp):
                lines.append("  [文件] %-21s %d 字节  %s"
                             % (stream, os.path.getsize(fp), fp))
            else:
                lines.append("  [无]   %s" % stream)
    return "\n".join(lines)

DEFAULT_CONFIG = {
    "ip": "172.19.163.3",
    "port": "42849",
    "res_w": "1080",
    "res_h": "2400",
    "res_presets": ["720x1280", "1080x2400", "1440x3200"],
    "bitrate": "8",
    "scale": "1.0",
    "fps": "60",
    "audio_mode": "both",
    "video_codec": "auto",      # auto=交给 scrcpy；也可固定 h264 / h265
    "max_size": "0",            # 画面最大边长（像素），0 = 不限（原始分辨率）
    "reconnect_enabled": True,  # 无线掉线后后台自动重连（退避 + 次数上限）
    "autostart": False,         # 开机自启：静默启动，只驻托盘
    "recent_devices": [],       # 最近连接过的设备 [{addr, ip, port, name, ts}]，最多 5 台
    "quick_launch": {},         # {设备序列号: [包名...]}；"*" 为无专属列表时的默认值
    "minimize_to_tray": False   # 开启后点关闭不退出，而是收进系统托盘继续待命
}

def load_config():
    merged = DEFAULT_CONFIG.copy()
    # 先读用户配置（EXE 数据流，或 ADS 不可用时的 exe 同目录文件）；
    # 缺失或不合法时回落到随包内置的默认配置
    raw = storage_read(CONFIG_STREAM)
    if raw:
        try:
            merged.update(json.loads(raw))
        except Exception:
            pass
    else:
        try:
            if os.path.exists(BUILTIN_CONFIG_FILE):
                with open(BUILTIN_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    merged.update(json.load(f))
        except Exception:
            pass
    # 旧版快捷启动是一个列表（全局共用），统一成 {设备: [...]} 结构，用 "*" 兜底
    ql = merged.get("quick_launch")
    if isinstance(ql, list):
        merged["quick_launch"] = {"*": ql} if ql else {}
    elif not isinstance(ql, dict):
        merged["quick_launch"] = {}
    return merged

def save_config(cfg):
    try:
        current = load_config()
        current.update(cfg)
        storage_write(CONFIG_STREAM,
                      json.dumps(current, ensure_ascii=False, indent=2))
    except Exception:
        pass

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
    kwargs = dict(capture_output=True, timeout=timeout,
                  startupinfo=get_startupinfo(), creationflags=0x08000000)
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

# 无线调试的设备发现：不再扫描 30000-49999 随机端口段（太慢，且扫描时 connect
# 会顺带连上多台设备导致后续 scrcpy 报 Multiple devices），改为只找 IP、只探 5555。
_IPV4_RE = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}')
CLASSIC_ADB_PORT = 5555           # adb tcpip 模式的固定端口
_PROBE_TIMEOUT = 0.35
_PROBE_WORKERS = 256              # 并发过高手机防火墙会丢弃 SYN，反而一个都扫不到

def _probe_port(ip, port, timeout=None):
    s = socket.socket()
    s.settimeout(timeout or _PROBE_TIMEOUT)
    try:
        return s.connect_ex((ip, port)) == 0
    except Exception:
        return False
    finally:
        s.close()

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

def _arp_table():
    """读 Windows ARP 邻居表，返回 (本机各网卡地址, 邻居主机地址)。

    只按 IPv4 字面量提取，不依赖系统语言；也不要求邻居有 MAC —— 虚拟网卡
    （UU Lanplay / Tailscale）的邻居条目常常没有 MAC，而手机正好可能挂在
    这些网段上。
    """
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, timeout=8,
                           encoding="utf-8", errors="replace",
                           startupinfo=get_startupinfo(), creationflags=0x08000000)
        out = (r.stdout or "") + (r.stderr or "")
    except Exception:
        return [], []
    local, hosts = [], []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if "---" in s:                       # 形如 "Interface: 172.19.163.2 --- 0x2a"
            m = _IPV4_RE.search(s)
            if m and _usable_ip(m.group(0)) and m.group(0) not in local:
                local.append(m.group(0))
            continue
        m = _IPV4_RE.match(s)                # 邻居条目行首即 IP
        if m and _usable_ip(m.group(0)) and m.group(0) not in hosts:
            hosts.append(m.group(0))
    return local, hosts

def _local_prefixes():
    """要按 /24 扫一遍的网段前缀。

    一台电脑常有多张网卡（物理网卡 + UU Lanplay / Tailscale 之类的虚拟网卡），
    手机连在哪个网段上并不确定，所以把本机各网卡、ARP 邻居以及配置里上次连过的
    地址所在网段全部覆盖到。
    """
    local, hosts = _arp_table()
    ips = list(local) + list(hosts) + [str(load_config().get("ip", ""))]
    prefixes = []
    for ip in ips:
        if not ip or not _usable_ip(ip):
            continue
        p = ip.rsplit(".", 1)[0]
        if p not in prefixes:
            prefixes.append(p)
    return prefixes

def _probe_5555(ips):
    """并发探测一批 IP 的 5555 端口，返回开放的 IP 集合。"""
    if not ips:
        return set()
    hits = set()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as ex:
            for ip, ok in zip(ips, ex.map(lambda a: _probe_port(a, CLASSIC_ADB_PORT),
                                          ips, chunksize=8)):
                if ok:
                    hits.add(ip)
    except Exception:
        pass
    return hits

def discover_devices():
    """发现可连接的设备：已连接设备 + ARP 已知主机 + 同网段 /24，只探测 5555 端口。

    扫描阶段不做任何 adb connect，用户点选后才去连接，因此不会把第二台设备
    意外连上、也就不会再出现 'Multiple ADB devices'。
    """
    t0 = time.time()
    entries, order = {}, []

    def add(ip, source, connected=False, port=None, usb=False):
        if ip not in entries:
            entries[ip] = {"ip": ip, "port": str(port or CLASSIC_ADB_PORT),
                           "addr": "%s:%s" % (ip, port or CLASSIC_ADB_PORT),
                           "source": source, "connected": connected, "usb": usb}
            order.append(ip)
        else:
            if connected:
                entries[ip]["connected"] = True
            if usb:
                entries[ip]["usb"] = True
        return entries[ip]

    # 1) adb 已在线的设备：直接列出（USB 直连的序列号不带端口，无线的是 ip:port）
    for d in get_devices():
        info = device_info(d)
        usb = ":" not in d
        add(info["ip"], "USB 有线" if usb else "已连接",
            connected=True, port=info["port"] or CLASSIC_ADB_PORT, usb=usb)
        entries[info["ip"]]["addr"] = d

    # 2) ARP 邻居（标为"已知设备"），3) 各本机网段 /24 内的其余主机
    local_ips, arp = _arp_table()
    known = set(local_ips) | set(arp)
    for ip in arp:
        add(ip, "已知设备")
    for p in _local_prefixes():
        for h in range(1, 255):
            add("%s.%d" % (p, h), "同网段")

    candidates = [entries[i]["ip"] for i in order if not entries[i]["connected"]]
    open5555 = _probe_5555(candidates)

    found = []
    for ip in order:
        e = entries[ip]
        if e["connected"]:
            found.append(e)                      # 已连接的照常显示
        elif ip in open5555:
            e["source"] = "已知设备" if ip in known else "同网段"
            found.append(e)
    found.sort(key=lambda e: (not e["connected"], not e.get("usb"),
                              e["source"] != "已知设备", _ip_sort_key(e)))
    return {"found": found, "elapsed": round(time.time() - t0, 1)}

# ---------- 设备发现：mDNS ----------
# Android 11+ 打开无线调试后，手机会广播 _adb-tls-connect（可直连）和
# _adb-tls-pairing（等配对）两种服务。mDNS 走组播，能发现和电脑不同 /24 网段、
# 但在同一个广播域里的设备。
def mdns_available():
    """adb 的 mDNS 后端是否可用。打包内置的 adb 37 默认可用，老版本可能不行。"""
    out, err, rc = run_adb(["mdns", "check"], timeout=8)
    msg = ((out or "") + (err or "")).strip()
    return "daemon version" in msg, msg

def mdns_services(timeout=8):
    """解析 `adb mdns services`：返回 ([(实例名, 服务类型, IP, 端口)], 错误文本)。

    输出形如 `adb-XXXX-HgzRvA\t_adb-tls-connect._tcp\t192.168.1.39:42865`，
    也容错空白分隔；只收 `_adb-tls-*` 服务。
    """
    out, err, rc = run_adb(["mdns", "services"], timeout=timeout)
    rows = []
    for line in (out or "").splitlines():
        line = line.rstrip("\r").strip()
        if not line:
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 3 or not parts[1].strip().startswith("_adb-tls-"):
            continue
        instance, svc, endpoint = parts[0].strip(), parts[1].strip(), parts[2].strip()
        ip, _, port = endpoint.rpartition(":")
        if ip and port.isdigit():
            rows.append((instance, svc, ip, port))
    return rows, ((out or "") + (err or "")).strip()

def collect_mdns(window=5.0, interval=1.2):
    """在时间窗内反复查 `adb mdns services`，把中途出现的服务合并起来。

    mDNS 是异步发现的：adb 服务刚起来时列表常是空的，手机广播也要几秒才会进缓存，
    所以单次查询很容易"一个都搜不到"。按 interval 轮询整个 window，同一地址只留
    一条（优先可直连的 _adb-tls-connect）。
    """
    box, deadline = {}, time.time() + window
    while True:
        rows, _ = mdns_services()
        for instance, svc, ip, port in rows:
            addr = "%s:%s" % (ip, port)
            old = box.get(addr)
            if old is None or (old[1].startswith("_adb-tls-pairing")
                               and svc.startswith("_adb-tls-connect")):
                box[addr] = (instance, svc, ip, port)
        left = deadline - time.time()
        if left <= 0:
            break
        time.sleep(min(interval, left))
    return list(box.values())

# 深度搜索分三级，逐级变慢、逐级更全：
#   1) 局域网：ARP 邻居 + 各网段 /24 探 5555 —— 几秒出结果，覆盖经典 tcpip 模式
#   2) mDNS：adb 自带 mDNS 发现「无线调试」广播（Android 11+ 官方发现方式）
#   3) 端口扫描：对存活主机扫 30000~50000 —— 「无线调试」的随机端口就落在这个区间，
#      mDNS 被防火墙 / 路由器隔离挡住时，只有这一路能捞到设备
# 全程在后台线程跑，前端轮询进度，界面不会卡住。参数都设了上限，防止在大网络里失控。
_DEEP_PORT_LO = 30000
_DEEP_PORT_HI = 50000          # Android 11+ 无线调试随机端口区间（不含上界）
_DEEP_MAX_HOSTS = 12           # 单次最多扫多少台存活主机（12 × 9s ≈ 110s，留足预算余量）
_DEEP_BUDGET = 150.0           # 端口扫描总时间预算（秒），到点收工返回已找到的
_DEEP_PROBE_TIMEOUT = 0.12     # 局域网 RTT 极低，0.12s 足够；缩短能大幅提速
# 每条扫描线程同时挂起的连接数。两个硬约束：
#   1) Windows 的 select() 一次只能收 512 个句柄（超过就 ValueError: too many file
#      descriptors / OSError 10022），所以必须 <512；
#   2) 实测在飞连接超过约 800 个时，手机 / AP 会开始成片丢掉 SYN —— 扫得"越快"
#      反而一个 adb 端口都扫不到（3840 在飞时命中 0，640 在飞时稳定命中）。
# 80 × 8 线程 = 640 在飞，是实测既不丢包又够快的点。
_SCAN_WINDOW = 80
# 一趟扫描约 5% 的开放端口会被随机丢包漏掉（实测 20 趟漏 1 趟）；同一段扫两趟
# 几乎不漏（实测 20 趟 0 漏）。两趟的代价是耗时翻倍，所以主机数上限相应收紧。
_SCAN_PASSES = 2
_MDNS_WINDOW = 6.0
# 端口命中的复核：adb connect 打在"开放但不回话"的端口上要等约 10 秒，必须设上限。
# 经验上手机在这个区间最多开 1~2 个端口；普通电脑 / 路由器会一口气开十几个
# （Windows RPC、Steam 之类），用 _PORT_NOISE_MAX 这类"噪音主机"直接跳过复核。
_PORT_NOISE_MAX = 4            # 一段时间内开放端口超过这个数，就当普通电脑，不复核
_VERIFY_MAX_PER_HOST = 4       # 每台主机最多复核几个候选端口
_VERIFY_TIMEOUT = 3            # 单个候选端口的 adb connect 超时（秒）
_VERIFY_BUDGET = 25.0          # 整轮复核的总时间预算（秒）

def _deep_threads():
    """端口扫描线程数：跟着 CPU 走（线程数不固定），下限 4、上限 8。

    每个线程内部用非阻塞 connect + selectors，一次能挂几十个连接在飞，
    所以几线程就顶替"几百个阻塞线程"：既快，也不会把系统拖垮。
    上限必须压在 8：在飞连接 = 线程数 × _SCAN_WINDOW，再多手机就开始丢 SYN 了。
    """
    cpu = os.cpu_count() or 4
    return max(4, min(8, cpu // 2))

_deep_lock = threading.Lock()
_deep_state = {
    "running": False, "phase": "idle", "text": "", "progress": 0.0,
    "found": [], "elapsed": 0.0, "error": "", "hosts": 0, "scanned": 0,
    "mdns_count": 0, "lan_count": 0, "port_count": 0,
}

def _deep_publish(**kw):
    with _deep_lock:
        _deep_state.update(kw)

def deep_state():
    """给前端轮询用的进度快照。"""
    with _deep_lock:
        st = dict(_deep_state)
    st["found"] = list(st["found"])
    return st

def _merge_found(lan, mdns_rows):
    """把局域网结果和 mDNS 结果按地址合并去重（mDNS 补充实例名 / 待配对信息）。"""
    box = {}
    for e in lan.get("found", []):
        box[e["addr"]] = dict(e, pairing=False, instance="", port_scan=False)
    # 必须是"这个地址"真的在线才算已连接：同一 IP 的另一个随机端口并不代表它也连上了
    online = set(get_devices())
    for instance, svc, ip, port in mdns_rows:
        addr = "%s:%s" % (ip, port)
        pairing = "pairing" in svc
        item = {"ip": ip, "port": port, "addr": addr, "instance": instance,
                "pairing": pairing, "usb": False, "port_scan": False,
                "source": "待配对（mDNS）" if pairing else "深度搜索（mDNS）",
                "connected": (not pairing) and addr in online}
        old = box.get(addr)
        if old is None:
            box[addr] = item
        else:                       # 同一台设备：补上 mDNS 信息，保留原有的来源标记
            old["pairing"] = pairing
            old["instance"] = instance
            old["connected"] = old["connected"] or item["connected"]
            if not old.get("source", "").startswith(("USB", "已知设备", "已连接")):
                old["source"] = item["source"]
    return box

def _alive_hosts(exclude):
    """存活主机清单 = ARP 邻居表里的地址（本机各网卡地址除外）。

    局域网扫描阶段已经对每个 /24 都发过 TCP SYN，凡是有回应的主机（哪怕端口全关）
    都会进 ARP 表，所以扫完 5555 再读一次 ARP，就等于拿到了"活着的主机"清单。
    """
    local, neighbors = _arp_table()
    mine = set(local)
    hosts = []
    for ip in neighbors + local:
        if ip in mine or ip in exclude or ip in hosts:
            continue
        hosts.append(ip)
    return hosts, mine

def _scan_shard(ip, ports, deadline, out, lock):
    """一个扫描线程：非阻塞 connect + selectors，最多同时挂 _SCAN_WINDOW 个连接。

    关键是超时收尾：对方丢包/被防火墙过滤时，connect 永远不会返回事件，必须自己
    按 _DEEP_PROBE_TIMEOUT 把过期连接剔掉再补新的，否则窗口会卡死在同一批端口上。
    「先剔旧、再补新」的顺序不能反：反过来的话整批同时超时会让 live 瞬间空掉，
    扫描会误判成"扫完了"直接结束（手机对关闭端口大多不回 RST，走的正是超时这条路）。
    """
    sel = selectors.DefaultSelector()
    it = iter(ports)
    live, exp = {}, deque()          # live: socket->port；exp: (到期时间, socket) 按序
    timeout = _DEEP_PROBE_TIMEOUT
    exhausted = False
    try:
        while True:
            now = time.time()
            if now > deadline:
                break
            while exp and exp[0][0] <= now:      # 超时的按顺序剔掉
                _, s = exp.popleft()
                if s in live:
                    try:
                        sel.unregister(s)
                    except Exception:
                        pass
                    live.pop(s, None)
                    s.close()
            while not exhausted and len(live) < _SCAN_WINDOW:
                p = next(it, None)
                if p is None:
                    exhausted = True
                    break
                s = socket.socket()
                s.setblocking(False)
                try:
                    if s.connect_ex((ip, p)) == 0:   # 极快的主机会立刻连上
                        with lock:
                            out.append(p)
                        s.close()
                        continue
                except Exception:
                    s.close()
                    continue
                try:
                    sel.register(s, selectors.EVENT_WRITE, p)
                except Exception:
                    s.close()
                    continue
                live[s] = p
                exp.append((now + timeout, s))
            if exhausted and not live:
                break
            try:
                events = sel.select(timeout=min(timeout, max(0.005, deadline - now)))
            except Exception:
                events = []
            for key, _m in events:
                s = key.fileobj
                try:
                    if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                        with lock:
                            out.append(key.data)
                except Exception:
                    pass
                try:
                    sel.unregister(s)
                except Exception:
                    pass
                live.pop(s, None)
                s.close()
    finally:
        for s in list(live):
            try:
                sel.unregister(s)
            except Exception:
                pass
            s.close()
        sel.close()

def _scan_ports(ip, ports, deadline):
    """扫一台主机的一段端口，返回开放的端口号列表。

    端口分片给几个线程，每片内部走非阻塞 selectors：在飞连接几十个就能顶替
    "几百个阻塞线程"，且线程只有个位数。同一段扫 _SCAN_PASSES 趟：SYN 在手机 /
    AP 侧有一定概率被随机丢掉，单趟实测约 5% 漏报，扫两趟基本不漏。
    """
    ports = list(ports)
    n = _deep_threads()
    found = set()
    for attempt in range(_SCAN_PASSES):
        if attempt and time.time() > deadline:
            break
        out, lock = [], threading.Lock()
        threads = [threading.Thread(target=_scan_shard,
                                    args=(ip, ports[i::n], deadline, out, lock), daemon=True)
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        found |= set(out)
    return sorted(found)

def _try_connect_addr(addr, timeout=_VERIFY_TIMEOUT):
    """端口命中后用 adb connect 复核，并以 adb devices 的最终状态为准。

    不能只看 adb connect 打印什么：只要 TCP 通它就回 "connected to …"，而且上一次
    失败的尝试会在 adb 里留下一条 offline 记录 —— 下一次就直接变成
    "already connected to …"，于是普通电脑的随机开放端口也被当成"已连接"。
    所以只有 adb devices 里真是 device 才算连上；unauthorized 是"真设备但手机上
    还没点允许"；其余一律 disconnect 摘掉，既不留垃圾条目，也保证下次扫描不会
    再看到那句误导的 "already connected"。
    """
    out, err, _ = run_adb(["connect", addr], timeout=timeout)
    text = ((out or "") + (err or "")).lower()
    if any(x in text for x in ("cannot", "failed", "refused", "timeout")):
        _drop_addr(addr)
        return None
    states = device_states()
    if addr in states["device"]:
        _skip_reconnect.discard(addr)
        _remember_device(addr)
        return "device"
    if addr in states["unauthorized"]:
        _remember_device(addr)
        return "unauthorized"
    _drop_addr(addr)                 # offline / 不在列表：不是能用的 adb 端点
    return None

def _drop_addr(addr):
    """摘掉一次失败的 connect 尝试，避免它在 adb 设备列表里留下 offline 条目。"""
    try:
        run_adb(["disconnect", addr], timeout=5)
    except Exception:
        pass

def _deep_job():
    t0 = time.time()
    mDNS_err = ""
    try:
        # 1) 局域网 + mDNS 并行：先尽快把"看得见"的设备报出来
        _deep_publish(phase="lan", text="正在搜索局域网设备 + mDNS …", progress=0.05)
        ok_mdns, msg = mdns_available()
        mdns_rows, lan = [], {"found": []}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_mdns = ex.submit(collect_mdns, _MDNS_WINDOW) if ok_mdns else None
            f_lan = ex.submit(discover_devices)
            try:
                lan = f_lan.result(timeout=60)
            except Exception as e:
                mDNS_err = "局域网扫描失败：%s" % e
            if f_mdns is not None:
                try:
                    mdns_rows = f_mdns.result(timeout=_MDNS_WINDOW + 15)
                except Exception:
                    mdns_rows = []
            else:
                mDNS_err = mDNS_err or "adb mDNS 不可用（%s）" % (msg or "无输出")

        box = _merge_found(lan, mdns_rows)
        mdns_n = sum(1 for e in box.values() if e.get("instance"))
        _deep_publish(found=list(box.values()), mdns_count=mdns_n,
                      lan_count=len(box) - mdns_n, progress=0.15,
                      elapsed=round(time.time() - t0, 1),
                      text="局域网找到 %d 个目标，开始端口扫描 …" % len(box))

        # 2) 端口扫描：对所有存活主机扫 30000~50000（已连接的设备跳过，它已经能用）
        already = {e["ip"] for e in box.values() if e.get("connected")}
        hosts, _mine = _alive_hosts(already)
        prefer = [e["ip"] for e in box.values()
                  if _usable_ip(e.get("ip") or "") and e["ip"] not in hosts]
        hosts = [ip for ip in prefer + hosts if _usable_ip(ip)][:_DEEP_MAX_HOSTS]
        deadline = t0 + _DEEP_BUDGET
        threads = _deep_threads()
        ports = range(_DEEP_PORT_LO, _DEEP_PORT_HI)
        verify_left = _VERIFY_BUDGET
        _deep_publish(phase="ports", hosts=len(hosts), scanned=0)
        for idx, ip in enumerate(hosts):
            if time.time() > deadline:
                break
            _deep_publish(
                progress=0.15 + 0.85 * idx / max(len(hosts), 1),
                text="端口扫描 %d/%d：%s（%d 线程）…" % (idx + 1, len(hosts), ip, threads))
            try:
                hits = _scan_ports(ip, ports, deadline)
            except Exception:
                hits = []
            # 开放端口一堆的主机基本是电脑 / 路由器（Windows RPC、Steam…），
            # 逐个 adb connect 复核又慢又没意义，直接跳过。
            if 0 < len(hits) <= _PORT_NOISE_MAX and verify_left > 0:
                _deep_publish(text="复核候选 %s（%d 个端口）…" % (ip, len(hits)))
                for p in hits[:_VERIFY_MAX_PER_HOST]:
                    if verify_left <= 0 or time.time() > deadline:
                        break
                    t_v = time.time()
                    addr = "%s:%d" % (ip, p)
                    state = _try_connect_addr(addr)
                    verify_left -= time.time() - t_v
                    if state:
                        # 复核通过才算真设备：device=可直接用；unauthorized=真端口，
                        # 但手机上还没点「允许调试」，标成待授权而不是已连接
                        box[addr] = {
                            "ip": ip, "port": str(p), "addr": addr,
                            "instance": "", "pairing": False, "usb": False,
                            "port_scan": True, "pending": state == "unauthorized",
                            "connected": state == "device",
                            "source": "深度搜索（端口扫描）"}
            _deep_publish(
                found=list(box.values()), scanned=idx + 1,
                port_count=sum(1 for x in box.values() if x.get("port_scan")),
                elapsed=round(time.time() - t0, 1))

        if not box and mDNS_err and "List of discovered" not in mDNS_err:
            _deep_publish(error=mDNS_err)
    except Exception as e:
        _deep_publish(error="深度搜索出错：%s" % e)
    finally:
        with _deep_lock:
            _deep_state["running"] = False
            _deep_state["phase"] = "done"
            _deep_state["progress"] = 1.0
            _deep_state["elapsed"] = round(time.time() - t0, 1)
            _deep_state["text"] = "深度搜索完成：共 %d 个目标（耗时 %ss）" % (
                len(_deep_state["found"]), _deep_state["elapsed"])

def deep_discover():
    """启动一次深度搜索；已在跑就直接返回当前进度。结果由前端轮询 deep_state() 取。"""
    with _deep_lock:
        if not _deep_state["running"]:
            _deep_state.update({"running": True, "phase": "lan", "progress": 0.0,
                                "text": "开始深度搜索 …", "found": [], "error": "",
                                "elapsed": 0.0, "hosts": 0, "scanned": 0,
                                "mdns_count": 0, "lan_count": 0, "port_count": 0})
            threading.Thread(target=_deep_job, name="deep-scan", daemon=True).start()
    return deep_state()

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

# ============ 本地图标素材库：建索引 + 多策略匹配 ============
# 素材库（icons/）的文件名就是包名。换手机后包名常与库键有细微差异（大小写、
# 分隔符，或多了/少了末段，如库键 cn.amazon.mShop.android 对设备的
# cn.amazon.mShop.android.shopping），所以用多级容错匹配代替精确命中。
_icon_index = {}                 # 包名(小写) -> 图标路径（可为 EXE 数据流路径）
_icon_index_lock = threading.Lock()
_icon_ads_lock = threading.Lock()   # 串行读改写「已缓存图标包名」索引流

def _icon_ads_keys():
    """读回已缓存到 EXE 数据流的图标包名列表。"""
    raw = storage_read(ICON_INDEX_STREAM)
    if not raw:
        return []
    try:
        keys = (json.loads(raw) or {}).get("packages")
        if isinstance(keys, list):
            return [k for k in keys if isinstance(k, str) and k]
    except Exception:
        pass
    return []

def _icon_ads_register(pkg):
    """新抓到的图标写进数据流后，把包名登记到索引流（幂等，加锁避免并发丢更新）。"""
    with _icon_ads_lock:
        keys = _icon_ads_keys()
        if pkg in keys:
            return
        keys.append(pkg)
        storage_write(ICON_INDEX_STREAM,
                      json.dumps({"packages": keys}, ensure_ascii=False))

def _icon_index_scan():
    idx = {}
    # 1) EXE 数据流里的图标（当前写入位置；流不在目录里，只能靠索引流找回）
    for pkg in _icon_ads_keys():
        p = ads_path(ICON_STREAM_PREFIX + pkg + ".webp")
        try:
            if os.path.getsize(p) > 0:
                idx[pkg.lower()] = p
        except OSError:
            continue
    # 2) 目录里的图标（内置只读素材库 + ADS 不可用时的落盘兜底）
    for d in ICON_SEARCH_DIRS:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext.lower() not in ICON_EXTS:
                continue
            p = os.path.join(d, name)
            try:
                if os.path.getsize(p) <= 0:
                    continue
            except OSError:
                continue
            idx.setdefault(stem.lower(), p)      # 可写目录优先
    return idx

def _icon_index_add(pkg, path):
    """新抓到的图标写好后登记进索引，后续请求即可立刻命中。"""
    if pkg and path:
        with _icon_index_lock:
            _icon_index.setdefault(pkg.lower(), path)

def _write_icon(pkg, data):
    """图标写入：优先写进 EXE 数据流 icon_<包名>.webp，不支持时退回 icons/ 目录。"""
    if _ads_usable():
        p = ads_path(ICON_STREAM_PREFIX + pkg + ".webp")
        try:
            with open(p, "wb") as f:
                f.write(data)
            _icon_ads_register(pkg)
            return p
        except Exception:
            pass
    try:
        os.makedirs(ICON_DIR, exist_ok=True)
        p = os.path.join(ICON_DIR, pkg + ".webp")
        with open(p, "wb") as f:
            f.write(data)
        return p
    except Exception:
        return ""

def _norm_icon_key(s):
    return re.sub(r'[^a-z0-9]', '', (s or "").lower())

def find_cached_icon(pkg):
    """从本地素材库匹配图标：精确 → 归一化 → 点分段互为前后缀 → 尾段重合 → 相似兜底。

    库键是包名，但换手机后常与设备包名有细微差异（大小写、分隔符，或多/少一段，
    例如库键 cn.amazon.mShop.android 对设备 cn.amazon.mShop.android.shopping），
    所以按点分段做双向容错匹配。全程只查启动时建好的索引，不碰网络也不碰 adb。
    """
    key = (pkg or "").strip().lower()
    if not key:
        return None
    segs = key.split(".")
    nk = _norm_icon_key(key)
    best, best_score = None, -1
    with _icon_index_lock:                   # 与 _icon_index_add 同锁，迭代时不会被改
        idx = _icon_index
        hit = idx.get(key)
        if hit:
            return hit
        for k, p in idx.items():
            if nk and _norm_icon_key(k) == nk:
                return p
            ks = k.split(".")
            if len(ks) < 2 or len(segs) < 2:
                continue
            # 点分段互为前缀/后缀：库键比设备包名多一段 or 少一段时都能命中
            if (key.startswith(k + ".") or k.startswith(key + ".")
                    or key.endswith("." + k) or k.endswith("." + key)):
                score = 100 + min(len(ks), len(segs))
                if score > best_score:
                    best, best_score = p, score
                continue
            if segs[-1] == ks[-1] and segs[-2] == ks[-2]:
                # 尾段重合：最后两段一致时，按公共后缀长度定优劣
                common = 0
                while (common < min(len(segs), len(ks))
                       and segs[-1 - common] == ks[-1 - common]):
                    common += 1
                if common > best_score:
                    best, best_score = p, common
    if best is not None:
        return best
    # 末级兜底：归一化后高度相似（换皮包名只差个别字母，如 com.foo.bar 对 com.foo.bars）
    fuzzy, fuzzy_score = None, 0.9
    with _icon_index_lock:
        for k, p in _icon_index.items():
            ks = k.split(".")
            if len(ks) < 2 or len(segs) < 2:
                continue
            r = SequenceMatcher(None, nk, _norm_icon_key(k)).ratio()
            if r >= fuzzy_score:
                fuzzy, fuzzy_score = p, r
    return fuzzy

# 名称别名表：同名不同包名（厂商换皮 / 应用改名）时，按应用名兜底命中素材库。
# 只收录素材库里确实有、且名称足够独特的常用应用；匹配不上就自然跳过，不会误配。
_ICON_NAME_ALIASES = {
    "微信": "com.tencent.mm",
    "QQ": "com.tencent.mobileqq",
    "学习强国": "cn.xuexi.android",
    "支付宝": "com.eg.android.AlipayGphone",
    "淘宝": "com.taobao.taobao",
    "京东": "com.jingdong.app.mall",
    "拼多多": "com.xunmeng.pinduoduo",
    "美团": "com.sankuai.meituan",
    "饿了么": "me.ele",
    "大众点评": "com.dianping.v1",
    "抖音": "com.ss.android.ugc.aweme",
    "快手": "com.smile.gifmaker",
    "哔哩哔哩": "tv.danmaku.bili",
    "微博": "com.sina.weibo",
    "小红书": "com.xingin.xhs",
    "知乎": "com.zhihu.android",
    "豆瓣": "com.douban.frodo",
    "贴吧": "com.baidu.tieba",
    "百度": "com.baidu.searchbox",
    "高德地图": "com.autonavi.minimap",
    "百度地图": "com.baidu.BaiduMap",
    "腾讯视频": "com.tencent.qqlive",
    "爱奇艺": "com.qiyi.video",
    "优酷视频": "com.youku.phone",
    "芒果TV": "com.hunantv.imgo.activity",
    "网易云音乐": "com.netease.cloudmusic",
    "QQ音乐": "com.tencent.qqmusic",
    "酷狗音乐": "com.kugou.android",
    "喜马拉雅": "com.ximalaya.ting.android",
    "微信读书": "com.tencent.weread",
    "掌阅": "com.zhangyue.read",
    "番茄小说": "com.dragon.read",
    "今日头条": "com.ss.android.article.news",
    "钉钉": "com.alibaba.android.rimet",
    "企业微信": "com.tencent.wework",
    "飞书": "com.ss.android.lark",
    "腾讯会议": "com.tencent.wemeet.app",
    "WPS Office": "cn.wps.moffice_eng",
    "百度网盘": "com.baidu.netdisk",
    "迅雷": "com.xunlei.downloadprovider",
    "剪映": "com.lemon.lv",
    "美图秀秀": "com.mt.mtxx.mtxx",
    "滴滴出行": "com.sdu.didi.psnger",
    "12306": "com.MobileTicket",
    "携程旅行": "ctrip.android.view",
    "去哪儿旅行": "com.Qunar",
    "铁路12306": "com.MobileTicket",
    "唯品会": "com.achievo.vipshop",
    "闲鱼": "com.taobao.idlefish",
    "得物": "com.shizhuang.duapp",
    "懂车帝": "com.ss.android.auto",
    "汽车之家": "com.cubic.autohome",
    "58同城": "com.wuba",
    "BOSS直聘": "com.hpbr.bosszhipin",
    "同花顺": "com.hexin.plat.android",
    "东方财富": "com.eastmoney.android.berlin",
    "雪球": "com.xueqiu.android",
    "云闪付": "com.unionpay",
    "中国移动": "com.greenpoint.android.mc10086.activity",
    "中国联通": "com.sinovatech.unicom.ui",
    "中国电信": "com.ct.client",
    "招商银行": "cmb.pb",
    "中国银行": "com.chinamworld.bocmbci",
    "中国建设银行": "com.chinamworld.main",
    "QQ邮箱": "com.tencent.androidqqmail",
    "网易邮箱大师": "com.netease.mail",
    "Keep": "com.gotokeep.keep",
    "学习通": "com.chaoxing.mobile",
    "作业帮": "com.baidu.homework",
    "网易有道词典": "com.youdao.dict",
    "墨墨背单词": "com.maimemo.android.momo",
    "扫描全能王": "com.intsig.camscanner",
    "夸克": "com.quark.browser",
    "UC浏览器": "com.UCMobile",
    "QQ浏览器": "com.tencent.mtt",
    "火狐浏览器": "org.mozilla.firefox",
    "Chrome": "com.android.chrome",
    "酷安": "com.coolapk.market",
    "TapTap": "com.taptap",
    "4399游戏盒": "com.m4399.gamecenter",
    "萤石云视频": "com.videogo",
    "米家": "com.xiaomi.smarthome",
    "智慧生活": "com.huawei.smarthome",
    "vivo官网": "com.vivo.space",
    "抖音极速版": "com.ss.android.ugc.aweme.lite",
    "快手极速版": "com.kuaishou.nebula",
    "今日头条极速版": "com.ss.android.article.lite",
    "全民K歌": "com.tencent.karaoke",
    "唱吧": "com.changba",
    "虎牙直播": "com.duowan.kiwi",
    "斗鱼直播": "air.tv.douyu.android",
    "和平精英": "com.tencent.tmgp.pubgmhd",
    "王者荣耀": "com.tencent.tmgp.sgame",
    "原神": "com.miHoYo.Yuanshen",
    "第五人格": "com.netease.dwrg",
    "开心消消乐": "com.happyelements.AndroidAnimal",
    "百度贴吧": "com.baidu.tieba",
    "孔夫子旧书网": "com.kongfz.app",
    "豆包": "com.larus.nova",
    "通义": "com.aliyun.tongyi",
    "文心一言": "com.baidu.newapp",
}

def _alias_icon(name):
    """按应用名找别名包名对应的素材；查不到返回 None。"""
    alias = _ICON_NAME_ALIASES.get((name or "").strip())
    return find_cached_icon(alias) if alias else None

# ============ 在线图标源（应用宝 / 小米商店 / iTunes）============
# 只补充素材库缺失的图标；不再从手机 APK 提取（那条路径要反复跑 adb，
# 会把 adb 信号量占满、拖死整个界面）。
_UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_online_lock = threading.Lock()
_online_cooldown = {}          # 源 -> 冷却截止时间戳（网络/限流故障，短冷却自愈）
_ONLINE_COOLDOWN_SEC = 300
_XIAOMI_PLACEHOLDER = "02f4849db3f7e487599e257b336d57b159d425b04"

# 系统通用名在 App Store 必然错配（如“设置/信息/电话”），iTunes 源直接跳过
_ITUNES_GENERIC_NAMES = {
    "设置", "信息", "电话", "相机", "相册", "浏览器", "音乐", "视频", "日历", "天气",
    "计算器", "录音机", "文件管理", "主题", "钱包", "互传", "邮件", "电子邮件", "联系人",
    "时钟", "闹钟时钟", "指南针", "手机管家", "扫描", "翻译机", "开关控制", "一键锁屏",
    "意见反馈", "游戏中心", "应用商店", "vivo摄影", "vivo健康", "vivo官网", "原子笔记",
    "电话与联系人", "智能遥控", "智慧生活", "系统跟踪", "Android System Angle",
}

def _http_get(url, timeout=12, referer=None):
    headers = {"User-Agent": _UA_BROWSER, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def _online_ok(src):
    with _online_lock:
        return time.time() > _online_cooldown.get(src, 0)

def _online_fail(src):
    with _online_lock:
        _online_cooldown[src] = time.time() + _ONLINE_COOLDOWN_SEC

def _yyb_icon_url(pkg):
    """应用宝详情页 SSR 内嵌记录，按包名精确取图标（Android 原生方形，256px）。"""
    if not _online_ok("yyb"):
        return None
    try:
        html = _http_get("https://sj.qq.com/appdetail/" + urllib.parse.quote(pkg),
                         timeout=12).decode("utf-8", "replace")
        m = re.search(
            r'"pkg_name":"' + re.escape(pkg)
            + r'","app_id":"\d+","name":"[^"]*","icon":"([^"]+)"', html)
        if not m:
            return None  # 未上架 / 页面无该包记录（非故障，不冷却）
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("http://"):
            u = "https://" + u[7:]
        if u.endswith(".svg") or "yyb-icon" in u:
            return None
        return u
    except urllib.error.HTTPError as e:
        if e.code not in (404, 400):   # 404=未上架；403/5xx=限流或故障，冷却
            _online_fail("yyb")
        return None
    except Exception:
        _online_fail("yyb")
        return None

def _xiaomi_icon_url(pkg):
    """小米应用商店详情页，取第一个 PNG 缩略图 hash 拼 l360 直链（包名必须出现在页面中防重定向占位）。"""
    if not _online_ok("mi"):
        return None
    try:
        url = "https://app.mi.com/details?id=" + urllib.parse.quote(pkg)
        html = _http_get(url, timeout=10).decode("utf-8", "replace")
        if pkg not in html:
            return None
        m = re.search(r'thumbnail/PNG/l\d+/AppStore/([0-9a-f]{40})', html)
        if not m or m.group(1) == _XIAOMI_PLACEHOLDER:
            return None
        return "https://file.market.xiaomi.com/thumbnail/PNG/l360/AppStore/" + m.group(1)
    except urllib.error.HTTPError as e:
        if e.code not in (404, 400):
            _online_fail("mi")
        return None
    except Exception:
        _online_fail("mi")
        return None

def _norm_app_name(s):
    return re.sub(r'[\s\-_·.，。,：:！!？?（）()]+', '', (s or "")).lower()

def _itunes_icon_url(name):
    """iTunes Search API 按应用名取图（bb 方形满版，无 iOS 圆角）；名称相似度校验防同名错配。"""
    if not name or name in _ITUNES_GENERIC_NAMES or not _online_ok("itunes"):
        return None
    try:
        u = ("https://itunes.apple.com/search?term=" + urllib.parse.quote(name)
             + "&country=cn&entity=software&limit=1")
        j = json.loads(_http_get(u, timeout=10).decode("utf-8", "replace"))
        res = j.get("results") or []
        if not res:
            return None
        art = res[0].get("artworkUrl512") or ""
        a, b = _norm_app_name(name), _norm_app_name(res[0].get("trackName", ""))
        if not a or not b:
            return None
        if not (a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.72):
            return None
        return re.sub(r'/\d+x\d+bb\.(?:jpg|png)$', '/512x512bb.jpg', art) or None
    except urllib.error.HTTPError as e:
        if e.code not in (404, 400):
            _online_fail("itunes")
        return None
    except Exception:
        _online_fail("itunes")
        return None

def _icon_bytes_to_webp(raw):
    """下载的图标统一为 256x256 webp；校验分辨率/单色/透明，不合格返回 None。"""
    from PIL import Image, ImageStat
    im = Image.open(io.BytesIO(raw))
    im.load()
    if min(im.size) < 96:
        return None
    if im.mode in ("P", "LA"):
        im = im.convert("RGBA")
    elif im.mode == "CMYK":
        im = im.convert("RGB")
    w, h = im.size
    if w != h:
        s = min(w, h)
        im = im.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    if im.size != (256, 256):
        im = im.resize((256, 256), Image.LANCZOS)
    if im.mode == "RGBA":
        alpha = im.getchannel("A")
        if alpha.getextrema()[0] < 250:
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=alpha)
            im = bg
        else:
            im = im.convert("RGB")
    elif im.mode != "RGB":
        im = im.convert("RGB")
    sd = ImageStat.Stat(im.resize((24, 24))).stddev
    if sum(sd) / 3 < 6:
        return None
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=82, method=4)
    return buf.getvalue()

def fetch_online_icon(pkg, name=None):
    """按 应用宝 → 小米 → iTunes 顺序在线获取图标，转 webp 永久缓存。返回缓存路径或 None。"""
    cached = find_cached_icon(pkg) or _alias_icon(name)
    if cached:
        return cached
    for url in (_yyb_icon_url(pkg), _xiaomi_icon_url(pkg), _itunes_icon_url(name)):
        if not url:
            continue
        try:
            raw = _http_get(url, timeout=15, referer="https://sj.qq.com/")
            if not raw or len(raw) < 1500:
                continue
            webp = _icon_bytes_to_webp(raw)
            if not webp:
                continue
            path = _write_icon(pkg, webp)
            if not path:
                continue
            _icon_index_add(pkg, path)
            return path
        except Exception:
            continue
    return None

# 图标获取调度：只剩在线队列（4 worker，秒级，完全不占用 adb）。
# 关键点：任何请求都不再阻塞 —— 命中素材库立即返回；否则入队后立即返回 None，
# 前端按延迟重试，抓到之后下一次请求自然命中。
_online_heap = []
_in_online = set()
_icon_cooldown = {}         # pkg -> 冷却截止；抓不到时短冷却，避免反复入队
_ICON_COOLDOWN_SEC = 600
_icon_cv = threading.Condition()
_icon_seq = 0

def _app_name(pkg):
    with _apps_lock:
        for apps in (_apps_cache or {}).values():
            for a in apps:
                if a.get("package") == pkg:
                    return a.get("name")
    return None

def _online_worker_loop():
    while True:
        with _icon_cv:
            while not _online_heap:
                _icon_cv.wait()
            _seq, pkg, qname = heapq.heappop(_online_heap)
            if pkg not in _in_online:
                continue                    # 重复入队产生的旧条目
        try:
            path = fetch_online_icon(pkg, qname or _app_name(pkg))
        except Exception:
            path = None
        with _icon_cv:
            _in_online.discard(pkg)
            if path:
                _icon_cooldown.pop(pkg, None)
            else:
                _icon_cooldown[pkg] = time.time() + _ICON_COOLDOWN_SEC

for _i in range(4):
    threading.Thread(target=_online_worker_loop,
                     name="icon-online-%d" % _i, daemon=True).start()

def submit_icon(pkg, name=None):
    """把包名投进在线图标队列。已缓存 / 冷却中 / 已在队列里则跳过。"""
    global _icon_seq
    if not pkg or find_cached_icon(pkg):
        return False
    with _icon_cv:
        if time.time() < _icon_cooldown.get(pkg, 0) or pkg in _in_online:
            return False
        _in_online.add(pkg)
        heapq.heappush(_online_heap, (_icon_seq, pkg, name))
        _icon_seq += 1
        _icon_cv.notify()
        return True

def request_icon(pkg, name=None):
    """只读素材库：命中就返回路径；否则后台补抓并立即返回 None。

    绝不等待网络，避免 HTTP 线程被拖住几十秒把界面卡死。
    """
    cached = find_cached_icon(pkg)
    if not cached:
        cached = _alias_icon(name)       # 换皮 / 改名包：按应用名兜底命中素材库
    if cached:
        return cached
    submit_icon(pkg, name=name)
    return None

def prefetch_icons(items):
    """items: pkg 字符串或 {"package":..,"name":..} 字典列表。只入在线队列，秒级返回。"""
    for it in items:
        if isinstance(it, dict):
            submit_icon(it.get("package"), name=it.get("name"))
        else:
            submit_icon(it)

_apps_cache = {}             # {设备序列号: [{"name":..,"package":..}]}（内存缓存，按设备各一份）
_apps_lock = threading.Lock()
# 首次扫描需推送 scrcpy-server 并在设备端起 Java 进程逐个取应用名，无线 adb 下明显偏慢，
# 超时值给足，避免扫到一半被中断成空结果。
APPS_SCAN_TIMEOUT = 120

def _load_apps_cache():
    """启动时读取上次扫描结果；同一台设备可直接出列表，不必每次启动都重扫。

    结构 {"devices": {序列号: {"apps": [...]}}}；旧版单设备格式直接忽略
    （重扫一次即可），不做兼容转换。
    """
    global _apps_cache
    raw = storage_read(APPS_CACHE_STREAM)
    if not raw:
        return
    try:
        devs = (json.loads(raw) or {}).get("devices")
        if isinstance(devs, dict):
            cache = {}
            for serial, item in devs.items():
                apps = (item or {}).get("apps")
                if isinstance(apps, list) and apps:
                    cache[serial] = apps
            _apps_cache = cache
    except Exception:
        pass

def _save_apps_cache(devices):
    """把全部设备的缓存写回存储。devices 为已拍好的快照，避免在锁外遍历活字典。"""
    storage_write(APPS_CACHE_STREAM, json.dumps({"devices": devices}, ensure_ascii=False))

def _write_text(stream, text):
    """文本类小文件（错误日志 / 扫描日志）：优先写进 EXE 数据流，返回实际路径。"""
    path, _ = storage_write(stream, text)
    return path

def _read_log_tail(stream, limit=1200):
    return (storage_read(stream) or "")[-limit:]

def _log_scan_failure(msg):
    """扫描异常时记一份小日志：窗口程序没有控制台，出问题只能靠日志排查。"""
    _write_text(SCAN_LOG_STREAM, time.strftime("%Y-%m-%d %H:%M:%S ") + msg)

def _scan_apps(serial):
    """真正执行一次扫描。返回 None 表示扫描失败（未连接/超时/报错），
    与"扫描成功但设备上确实没有应用"（返回空列表）区分开，避免用失败结果覆盖缓存。"""
    try:
        r = subprocess.run([SCRCPY_PATH] + _serial_args(serial) + ["--list-apps"], capture_output=True,
                          encoding="utf-8", errors="replace",
                          timeout=APPS_SCAN_TIMEOUT, cwd=os.path.dirname(SCRCPY_PATH),
                          startupinfo=get_startupinfo(), creationflags=0x08000000)
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        _log_scan_failure("scan exception: %r" % (e,))
        return None
    apps = []
    seen = set()
    for line in out.split('\n'):
        line = line.strip()
        if line.startswith('-') or line.startswith('*'):
            line = line.lstrip('-*').strip()
            parts = re.split(r'\s{2,}', line)
            if len(parts) >= 2:
                label = parts[0].strip()
                pkg = parts[-1].strip()
                if label and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+', pkg) \
                        and pkg not in seen:
                    seen.add(pkg)
                    apps.append({"name": label, "package": pkg})
    if not apps:
        _log_scan_failure("rc=%s | no apps parsed | tail:\n%s" % (r.returncode, out[-2000:]))
        if r.returncode != 0:
            return None
    apps.sort(key=lambda x: x["name"].lower())
    return apps

def list_apps(serial, force=False):
    """获取指定设备的应用列表（按序列号各缓存一份）。并发请求共享同一次扫描
    （单飞），避免重复拉起 scrcpy 把首次连接拖慢。"""
    if not serial:
        devs = get_devices()
        serial = devs[0] if devs else None
    if not serial:
        return []
    if not force:
        with _apps_lock:
            cached = _apps_cache.get(serial)
        if cached is not None:
            return cached
    with _apps_lock:
        # 等锁期间其他请求可能已经扫完，直接复用
        if not force:
            cached = _apps_cache.get(serial)
            if cached is not None:
                return cached
        apps = _scan_apps(serial)
        if apps is None:
            return _apps_cache.get(serial, [])   # 扫描失败则退回已有缓存，绝不用空结果顶替
        _apps_cache[serial] = apps
        snapshot = {s: {"apps": a} for s, a in _apps_cache.items()}
    _save_apps_cache(snapshot)
    # 后台预取：素材库直接命中；缺失的进在线队列，不阻塞按需请求
    prefetch_icons(apps)
    return apps

_icon_index = _icon_index_scan()
_load_apps_cache()

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
    except:
        return -1

def set_media_volume(vol, serial=None):
    run_adb(["shell", "media", "volume", "--stream", "3", "--set", str(vol)],
            timeout=5, serial=serial)

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

_adb_server_ours = False   # 由 __main__ 在首次调用 adb 之前置位

def cleanup_scrcpy():
    try:
        subprocess.run(["taskkill", "/f", "/im", "scrcpy.exe"], 
                      capture_output=True, startupinfo=get_startupinfo(), creationflags=0x08000000)
    except Exception:
        pass

def shutdown_all():
    """关闭应用时统一收尾：托盘图标、投屏进程、本进程拉起的子进程，以及本次由我们启动的 adb 服务。"""
    _tray_stop()
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

# ---------- 扫码连接（二维码配对，Android 11+）----------
# 电脑显示二维码（WIFI:T:ADB;S:<名字>;P:<密码>;;），手机在「无线调试 → 使用二维码
# 配对设备」里扫一下：手机扫到后会按码里的名字广播 _adb-tls-pairing._tcp，我们等它
# 出现，再用码里的密码执行 `adb pair`，最后等 _adb-tls-connect 出现并 adb connect。
# 不用手输 IP / 端口 / 配对码；配对协议仍由 adb 客户端完成，不自己实现。
_qr_lock = threading.Lock()
_qr_state = {"state": "idle", "message": "", "name": "", "password": "",
             "addr": "", "tok": 0}
_QR_TIMEOUT = 120          # 等手机扫码的最长时间（秒）

def _qr_payload(name, password):
    return "WIFI:T:ADB;S:%s;P:%s;;" % (name, password)

def _qr_svg(payload):
    """把二维码画成白底 SVG（界面里自适应缩放）。qrcode 库缺失时返回空串。"""
    try:
        import qrcode
        import qrcode.image.svg
        qr = qrcode.QRCode(image_factory=qrcode.image.svg.SvgPathImage,
                           border=2, box_size=10)
        qr.add_data(payload)
        buf = io.BytesIO()
        qr.make_image().save(buf)
        return buf.getvalue().decode("utf-8", "replace")
    except Exception:
        return ""

def _qr_set(**kw):
    with _qr_lock:
        _qr_state.update(kw)

def _qr_snapshot():
    with _qr_lock:
        return dict(_qr_state)

def _qr_worker(tok, name, password):
    """等手机广播配对服务 → adb pair → 等连接服务出现 → adb connect。"""
    def alive():
        with _qr_lock:
            return _qr_state["tok"] == tok
    ok, msg = mdns_available()
    if not ok:
        _qr_set(state="failed",
                message="当前 adb 的 mDNS 不可用，无法扫码配对：%s" % (msg or "请更新 platform-tools"))
        return
    _qr_set(state="waiting", message="等待手机扫描二维码…")
    deadline, target = time.time() + _QR_TIMEOUT, None
    while time.time() < deadline and alive():
        rows, _ = mdns_services()
        pairing = [r for r in rows if r[1].startswith("_adb-tls-pairing")]
        # 优先按码里的名字匹配；个别机型不回显名字时，只有一个待配对服务也认
        match = [r for r in pairing if r[0] == name] or (pairing if len(pairing) == 1 else [])
        if match:
            target = match[0]
            break
        time.sleep(1.5)
    if not alive():
        return
    if not target:
        _qr_set(state="timeout",
                message="等待超时：没等到手机扫描。请确认手机与电脑在同一 Wi-Fi（mDNS 不跨网段），再重新扫一次。")
        return
    _, _, ip, port = target
    addr = "%s:%s" % (ip, port)
    _qr_set(state="pairing", addr=addr, message="已发现手机 %s，正在配对…" % addr)
    out, err, _ = run_adb(["pair", addr, password], timeout=20)
    text = ((out or "") + " " + (err or "")).strip()
    if "Successfully paired" not in text:
        _qr_set(state="failed", message="配对失败：" + (text or "adb pair 没有返回结果"))
        return
    _qr_set(state="connecting", message="配对成功，正在建立连接…")
    deadline = time.time() + 30
    while time.time() < deadline and alive():
        for d in get_devices():
            if device_info(d)["ip"] == ip:
                _qr_finish(ip, d)
                return
        rows, _ = mdns_services()
        for _, svc, cip, cport in rows:
            if svc.startswith("_adb-tls-connect") and cip == ip:
                run_adb(["connect", "%s:%s" % (cip, cport)], timeout=15)
                break
        time.sleep(1.5)
    if alive():
        _qr_set(state="failed",
                message="已配对成功，但自动连接超时：请在手机保持「无线调试」开着，再用「按地址连接」连 %s。" % ip)

def _qr_finish(ip, serial):
    info = device_info(serial) if serial else {"port": ""}
    port = info.get("port") or str(CLASSIC_ADB_PORT)
    addr = serial or ("%s:%s" % (ip, port))
    _skip_reconnect.discard(addr)
    _remember_device(addr)
    save_config({"ip": ip, "port": port})
    _qr_set(state="done", addr=addr, message="已连接 %s" % addr)

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    
    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        
        if path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            with open(os.path.join(RES_DIR, 'index.html'), 'r', encoding='utf-8') as f:
                self.wfile.write(f.read().encode())
        
        elif path == '/api/status':
            st = device_states()
            cfg = load_config()
            self.send_json({
                "version": APP_VERSION,
                # devices=可用设备；pending=手机还停在「允许调试」弹窗；offline=掉线但 adb 还挂着
                # usb=数据线直连（序列号不带端口），界面据此标成「USB 有线」并优先使用
                "devices": [dict(device_info(d), model=_device_model(d), connected=True,
                                 usb=(":" not in d))
                            for d in st["device"]],
                "pending": [dict(device_info(d), model=None, usb=(":" not in d))
                            for d in st["unauthorized"]],
                "offline": [dict(device_info(d), model=None, usb=(":" not in d))
                            for d in (st["offline"] + st["other"])],
                "config": cfg
            })
        
        elif path == '/api/apps':
            qs = urllib.parse.parse_qs(parsed.query)
            force = qs.get('force', ['0'])[0] == '1'
            serial = qs.get('device', [''])[0]
            apps = list_apps(serial, force=force)
            self.send_json({"apps": apps, "device": serial})

        elif path == '/api/discover':
            self.send_json(discover_devices())

        elif path == '/api/discover/deep':
            self.send_json(deep_discover())

        elif path == '/api/discover/deep/status':
            self.send_json(deep_state())

        elif path == '/api/qr/status':
            self.send_json(_qr_snapshot())
        
        elif path == '/api/diag':
            qs = urllib.parse.parse_qs(parsed.query)
            self.send_json(run_diag(deep=qs.get('deep', ['0'])[0] == '1'))

        elif path == '/api/log':
            # 只把日志文本交给前端，保存位置由用户在弹出的「另存为」里自选
            self.send_json({"text": collect_logs()})

        elif path == '/api/launch':
            qs = urllib.parse.parse_qs(parsed.query)
            pkg = qs.get('pkg', [''])[0]
            serial = qs.get('device', [''])[0] or None
            if pkg:
                proc, focused = launch_app(pkg, serial=serial)
                if focused:
                    # 该应用已经在投屏：没有重复拉起 scrcpy，只把已有窗口唤到前台
                    self.send_json({"ok": True, "focused": True})
                else:
                    self.send_json(_launch_result(proc))
            else:
                self.send_json({"ok": False, "error": "no package"}, 400)
        
        elif path == '/api/icon':
            qs = urllib.parse.parse_qs(parsed.query)
            pkg = qs.get('pkg', [''])[0]
            if not re.fullmatch(r'[A-Za-z0-9_.]+', pkg):
                self.send_response(400)
                self.end_headers()
                return
            icon = request_icon(pkg, name=_app_name(pkg))
            if icon and os.path.exists(icon):
                ctype = {'.png': 'image/png', '.webp': 'image/webp',
                         '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}[os.path.splitext(icon)[1]]
                with open(icon, 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'private, max-age=86400')
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()

        elif path == '/api/desktop':
            qs = urllib.parse.parse_qs(parsed.query)
            serial = qs.get('device', [''])[0] or None
            self.send_json(_launch_result(launch_desktop(serial=serial)))
    
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length).decode() or '{}')
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "invalid json"}, 400)
            return
        
        if path == '/api/connect':
            ip = body.get('ip', '')
            port = str(body.get('port', '') or CLASSIC_ADB_PORT)
            addr = "%s:%s" % (ip, port)
            out, err, _ = run_adb(["connect", addr], timeout=15)
            text = ((out or "") + (err or "")).lower()
            ok = "connected" in text and not any(x in text for x in ("cannot", "failed", "refused"))
            if not ok:
                # adb connect 报失败、但设备其实已经在线或正等授权时，也算连上：
                # 否则界面会出现"明明连上了却显示连接失败"的误报。
                st = device_states()
                ips = [device_info(d)["ip"] for d in (st["device"] + st["unauthorized"])]
                ok = ip in ips
            if ok:
                _skip_reconnect.discard(addr)
                save_config({"ip": ip, "port": port})
                _remember_device(addr)
            self.send_json({"success": ok, "serial": addr, "out": out, "err": err})
        
        elif path == '/api/disconnect':
            dev = body.get('device', '')
            if dev:
                _skip_reconnect.add(dev)     # 用户主动断开：自动重连不要再把它拉回来
                run_adb(["disconnect", dev], timeout=10)
            self.send_json({"ok": True})

        elif path == '/api/usb2wifi':
            self.send_json(usb_to_wifi())

        elif path == '/api/qr/start':
            name = "kuaitou-" + secrets.token_hex(4)
            password = "".join(secrets.choice(string.ascii_letters + string.digits)
                               for _ in range(12))
            payload = _qr_payload(name, password)
            with _qr_lock:
                _qr_state["tok"] += 1
                tok = _qr_state["tok"]
                _qr_state.update({"state": "starting", "message": "", "addr": "",
                                  "name": name, "password": password})
            threading.Thread(target=_qr_worker, args=(tok, name, password),
                             daemon=True).start()
            self.send_json({"ok": True, "name": name, "password": password,
                            "payload": payload, "svg": _qr_svg(payload)})

        elif path == '/api/qr/cancel':
            with _qr_lock:
                _qr_state["tok"] += 1
                _qr_state.update({"state": "idle", "message": ""})
            self.send_json({"ok": True})
        
        elif path == '/api/pair':
            ip = body.get('ip', '')
            port = body.get('port', '')
            code = body.get('code', '')
            addr = f"{ip}:{port}"
            out, err, _ = run_adb(["pair", addr, code], timeout=15)
            self.send_json({"out": out + err, "success": "Successfully paired" in out + err})
        
        elif path == '/api/config':
            save_config(body)
            if 'minimize_to_tray' in body or 'autostart' in body:
                _tray_sync()          # 开关一变就启停托盘图标
            resp = {"ok": True}
            if 'autostart' in body:
                resp["autostart"] = _apply_autostart(bool(body['autostart']))
            self.send_json(resp)

# ---------- 启动自检 / 错误上报 ----------
# 打包后没有控制台，出错就"闪一下没了"，换台电脑根本查不到原因。
# 所以：致命错误写日志文件并弹窗；界面依赖的 WebView2 运行时缺失时给出明确提示。
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

def _show_message(text, style=0x44):
    """不依赖界面控件弹窗（0x44 = 信息图标 + 是/否），返回是否点了“是”。"""
    try:
        return ctypes.windll.user32.MessageBoxW(None, text, "快投", style) == 6
    except Exception:
        return False

def _webview2_available():
    """界面由 Edge WebView2 渲染，精简版 / 老系统的电脑可能没装这个运行时。"""
    import winreg
    client = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
                      (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients")):
        try:
            with winreg.OpenKey(root, sub + "\\" + client) as k:
                pv = winreg.QueryValueEx(k, "pv")[0]
                if pv and pv != "0.0.0.0":
                    return True
        except Exception:
            continue
    return False

def _os_desc():
    try:
        v = sys.getwindowsversion()
        return "Windows %d.%d build %d" % (v.major, v.minor, v.build)
    except Exception:
        return "未知系统"

def _write_error_log(text):
    """错误日志写进 EXE 数据流（ADS 不可用时退回 exe 同目录文件），返回实际路径。"""
    return _write_text(ERROR_LOG_STREAM, text) or "(日志写入失败)"

# ---------- 一键诊断 ----------
# 换台电脑后"adb 连得上、却扫不出应用 / 投不出画面"时，把每个环节的实际输出
# 汇总成一份报告，直接指出卡在哪一步。诊断只在内存里生成文本，保存位置由用户自选。
def _run_capture(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=os.path.dirname(SCRCPY_PATH),
                           startupinfo=get_startupinfo(), creationflags=0x08000000)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return -1, "异常：%r" % (e,)

def _fps_report():
    """从投屏日志里提取 scrcpy --print-fps 的输出（形如 `60 fps` / `58 fps (+2 frames skipped)`）。

    投屏命令都带 --print-fps，所以最近一次投屏的实测帧率就躺在日志里；
    诊断报告直接引用它，比"看命令参数"更能说明实际流畅度。
    """
    log = storage_read(LAUNCH_LOG_STREAM) or ""
    vals = [float(v) for v in re.findall(r'(\d+)\s*fps\b', log, re.I)][-15:]
    if not vals:
        return "（日志里还没有帧率记录：先用「镜像桌面 / 镜像应用」投一次屏再看）"
    return ("最近 %d 次采样：平均 %.0f FPS，最低 %.0f，最高 %.0f，末次 %.0f"
            % (len(vals), sum(vals) / len(vals), min(vals), max(vals), vals[-1]))

def run_diag(deep=False):
    lines = []
    def add(k, v=""):
        lines.append(("%s: %s" % (k, v)) if v else k)

    add("== 环境 ==")
    add("时间", time.strftime("%Y-%m-%d %H:%M:%S"))
    add("快投版本", APP_VERSION)
    add("系统", _os_desc())
    add("打包运行", "是" if getattr(sys, "frozen", False) else "否")
    add("Python", sys.version.split()[0])
    add("默认编码", locale.getpreferredencoding(False))
    add("只读资源目录", RES_DIR)
    add("可写数据目录", DATA_DIR)
    add("ANDROID_ADB_SERVER_PORT", os.environ.get("ANDROID_ADB_SERVER_PORT", "(未设置)"))
    add("adb 服务是本次启动的", str(_adb_server_ours))
    add("scrcpy.exe 存在", str(os.path.exists(SCRCPY_PATH)))
    add("scrcpy-server 存在",
        str(os.path.exists(os.path.join(os.path.dirname(SCRCPY_PATH), "scrcpy-server"))))
    add("已连接设备", ", ".join(get_devices()) or "(无)")

    add("")
    add("== 存储位置 ==")
    for line in storage_location().split("\n"):
        add(line)

    for name, cmd, to in (("scrcpy --version", [SCRCPY_PATH, "--version"], 20),
                          ("adb version", [ADB_PATH, "version"], 20),
                          ("adb devices -l", [ADB_PATH, "devices", "-l"], 20)):
        rc, out = _run_capture(cmd, to)
        add("")
        add("== %s  rc=%s ==" % (name, rc))
        add(out.replace("\n", "\n  ") if out else "(无输出)")

    rc, out = _run_capture([ADB_PATH] + _serial_args() + ["shell", "getprop", "ro.product.model"], 20)
    add("")
    add("== adb shell getprop  rc=%s ==" % rc)
    add(out.replace("\n", "\n  ") if out else "(无输出)")

    cfg = load_config()
    add("")
    add("== 画面设置 ==")
    add("视频编码", str(cfg.get("video_codec", "auto")))
    add("最大尺寸", str(cfg.get("max_size", "0")))
    add("目的帧率上限", str(cfg.get("fps", "")))
    add("码率(Mbps)", str(cfg.get("bitrate", "")))
    add("掉线自动重连", str(cfg.get("reconnect_enabled", True)))
    add("实测帧率", _fps_report())

    if deep:
        add("")
        add("== scrcpy --list-apps（扫应用用的就是这一步）==")
        rc, out = _run_capture([SCRCPY_PATH] + _serial_args() + ["--list-apps"], APPS_SCAN_TIMEOUT)
        add("rc=%s" % rc)
        add(out[-1500:].replace("\n", "\n  ") if out else "(无输出)")

    text = "\n".join(lines)
    return {"text": text}

def collect_logs():
    """汇总可导出的日志：投屏日志 + 扫描异常日志 + 启动错误日志（有哪份取哪份）。"""
    parts = []
    for stream, title in ((LAUNCH_LOG_STREAM, "投屏日志 scrcpy_launch.log"),
                          (SCAN_LOG_STREAM, "应用扫描日志 apps_scan.log"),
                          (ERROR_LOG_STREAM, "启动错误日志")):
        content = storage_read(stream)
        if content and content.strip():
            parts.append("===== %s =====\n%s" % (title, content.strip()))
    if not parts:
        parts.append("（暂无可导出的日志：本次运行还没有产生投屏 / 扫描 / 错误记录）")
    return "\n\n".join(parts)

def _open_webview2_download():
    try:
        import webbrowser
        webbrowser.open(WEBVIEW2_URL)
    except Exception:
        pass

def _report_fatal(exc):
    import traceback
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    path = _write_error_log("%s\n%s\nPython %s\n\n%s" % (
        _os_desc(), sys.executable, sys.version.split()[0], detail))
    if _show_message("快投启动失败：\n%s\n\n详细信息已写入：\n%s\n\n"
                     "常见原因：缺少 Microsoft Edge WebView2 运行时，或系统低于 Windows 10。\n"
                     "点“是”打开 WebView2 运行时的官方下载页。" % (exc, path)):
        _open_webview2_download()

# ---------- 系统托盘 ----------
# 开启"关闭时缩小到托盘"后，点关闭只是把主界面收起来，应用继续在托盘待命：
# 右键托盘图标可以重新打开主界面、按设备启动镜像桌面或快捷启动的应用，或退出应用。
_tray_icon = None
_tray_thread = None
_tray_lock = threading.Lock()
_tray_exiting = False        # 真：本次关闭是"退出应用"，放行窗口关闭而不是收进托盘
_window = None               # 主窗口引用（pywebview 的 Window 对象）

def _tray_enabled():
    return bool(load_config().get("minimize_to_tray"))

def _tray_image():
    """托盘图标：优先用 appicon.ico（打包时随包），取不到就画一个占位图标。"""
    from PIL import Image, ImageDraw
    for path in (os.path.join(DATA_DIR, "appicon.ico"),
                 os.path.join(RES_DIR, "appicon.ico")):
        try:
            if os.path.exists(path):
                return Image.open(path)
        except Exception:
            continue
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(37, 99, 235, 255))
    d.rounded_rectangle((20, 12, 34, 52), radius=4, fill=(255, 255, 255, 255))
    d.rounded_rectangle((38, 22, 52, 52), radius=3, fill=(255, 255, 255, 255))
    return img

def _quick_pkgs(serial):
    """该设备的快捷启动包名；没有专属列表时用 "*" 兜底。"""
    ql = load_config().get("quick_launch") or {}
    return ql.get(serial) or ql.get("*") or []

def _tray_show():
    try:
        if _window:
            _window.show()
    except Exception:
        pass

def _tray_quit():
    global _tray_exiting
    _tray_exiting = True
    shutdown_all()
    os._exit(0)

def _act(fn):
    """pystray 的动作回调签名是 (icon, item)，这里包一层并吞掉异常。"""
    def run(*_):
        try:
            fn()
        except Exception:
            pass
    return run

def _tray_items():
    """每次弹出菜单时现取设备列表，连上/掉线都能立刻反映出来。"""
    import pystray
    items = [pystray.MenuItem("打开主界面", _act(_tray_show), default=True)]
    devs = get_devices()
    if devs:
        items.append(pystray.Menu.SEPARATOR)
        for d in devs:
            sub = [pystray.MenuItem("镜像桌面", _act(lambda d=d: launch_desktop(serial=d)))]
            for pkg in _quick_pkgs(d)[:12]:
                # 应用列表还没加载过时 _app_name 拿不到中文名，退回包名，别让菜单空着
                sub.append(pystray.MenuItem(
                    _app_name(pkg) or pkg,
                    _act(lambda p=pkg, d=d: launch_app(p, serial=d))))
            items.append(pystray.MenuItem("%s (%s)" % (_device_model(d), d),
                                          pystray.Menu(*sub)))
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("退出应用", _act(_tray_quit)))
    return tuple(items)

def _tray_start():
    global _tray_icon, _tray_thread
    with _tray_lock:
        if _tray_icon is not None:
            return
        try:
            import pystray
            icon = pystray.Icon("kuaitou", _tray_image(), "快投",
                                pystray.Menu(_tray_items))
        except Exception:
            return
        _tray_icon = icon
        _tray_thread = threading.Thread(target=icon.run, daemon=True)
        _tray_thread.start()

def _tray_stop():
    global _tray_icon, _tray_thread
    with _tray_lock:
        icon, _tray_icon, _tray_thread = _tray_icon, None, None
    if icon:
        try:
            icon.stop()
        except Exception:
            pass

def _tray_sync():
    """配置里的托盘开关变化后调用：按需启停托盘图标。"""
    if _tray_enabled():
        _tray_start()
    else:
        _tray_stop()

# ---------- 开机自启动 ----------
# 只写当前用户的 HKCU\...\Run（不需要管理员），命令带 --silent：开机后静默启动，
# 只在托盘待命，不弹主界面。源码直跑（开发态）时不碰注册表，避免污染开发机。
_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "快投"

def _apply_autostart(on):
    """写 / 删开机启动项，返回 {"ok": bool, "note"/"error"}。"""
    if not getattr(sys, "frozen", False):
        return {"ok": True, "note": "开发模式不写注册表"}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  '"%s" --silent' % EXE_PATH)
            else:
                try:
                    winreg.DeleteValue(k, _AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- 原生「另存为」对话框（pywebview js_api）----------
# 诊断报告与日志导出都走这里：由用户在弹出的原生对话框里自选保存位置，
# 而不是应用偷偷往某个固定目录写文件。js_api 的回调本身跑在独立线程上，
# 正好满足 pywebview「文件对话框不能占用 GUI 线程」的要求。
class Api:
    def save_text(self, default_name, content):
        if not _window:
            return {"ok": False, "error": "窗口未就绪"}
        try:
            result = _window.create_file_dialog(
                webview.FileDialog.SAVE, directory="", allow_multiple=False,
                save_filename=default_name or "快投_导出.txt",
                file_types=("文本文件 (*.txt)",))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not result:                       # 用户取消
            return {"canceled": True}
        path = result[0] if isinstance(result, (list, tuple)) else str(result)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content or "")
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

# ---------- 单实例保护 ----------
# 两个「快投」同时跑，会各自拉一份 adb、各自往 exe 的数据流里写配置和日志（互相覆盖），
# 还会抢同一块虚拟屏。所以同一时间只允许一个实例，重复启动就把已有窗口唤到前台。
_MUTEX_HANDLE = None
_MUTEX_NAME = "Local\\KuaitouSingleInstance"

def _focus_existing_window():
    """唤起已运行实例的主窗口。它可能正收在托盘里，所以用 SW_RESTORE 而不是 SW_SHOW。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "快投")
        if not hwnd:
            return False
        user32.AllowSetForegroundWindow(0xFFFFFFFF)   # ASFW_ANY：否则只能让它在任务栏闪一下
        user32.ShowWindow(hwnd, 9)                    # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False

def _acquire_single_instance():
    """抢占单实例互斥体。已经有实例在跑时返回 False。"""
    global _MUTEX_HANDLE
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        # ERROR_ALREADY_EXISTS：互斥体早就存在，说明另一个实例正在运行
        return ctypes.get_last_error() != 183
    except Exception:
        return True     # 互斥体本身出问题不该导致应用打不开

def _run():
    global _adb_server_ours, _window

    silent = "--silent" in sys.argv        # 开机自启：静默启动，只驻托盘不弹主界面

    # 已经在运行就直接唤起旧窗口后退出，别再拉一份 adb / 再写一份配置
    if not _acquire_single_instance():
        if not silent and not _focus_existing_window():
            _show_message("快投已经在运行了。", 0x40)
        return

    # 首次调用 adb 之前记下 adb 服务是否已在运行，退出时只关我们自己拉起来的那个
    _adb_server_ours = not _adb_server_running()

    if not _webview2_available():
        if _show_message("缺少 Microsoft Edge WebView2 运行时，界面无法显示。\n\n"
                         "点“是”打开官方下载页，安装后重新运行快投即可。"):
            _open_webview2_download()
        return

    # 端口交给系统分配：固定端口在别人的电脑上可能被其它程序占用
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    window = webview.create_window(
        '快投',
        f'http://127.0.0.1:{port}',
        width=950,
        height=850,
        min_size=(700, 600),
        hidden=silent,          # 静默启动时不显示主界面（托盘右键可随时打开）
        js_api=Api()            # 暴露原生「另存为」对话框给前端
    )
    _window = window

    def on_closing():
        # 开了"缩小到托盘"（或本次是静默自启）且不是从托盘选的"退出应用"：点关闭只把
        # 窗口藏起来，应用继续在托盘待命（返回 False 取消这次关闭）。
        if (_tray_enabled() or silent) and not _tray_exiting and _tray_icon is not None:
            try:
                window.hide()
            except Exception:
                pass
            return False
        return True

    def on_closed():
        # 真正退出：收尾所有相关进程后硬退出。
        # 扫描用的线程池是非守护线程，解释器退出时会等它们跑完（最长 20 多秒），
        # 只靠 return 会出现"窗口关了、进程还在"的情况。
        shutdown_all()
        os._exit(0)

    window.events.closing += on_closing
    window.events.closed += on_closed
    if _tray_enabled() or silent:
        _tray_start()
        # 静默启动时如果托盘起不来（pystray 缺失等），窗口必须露出来，否则应用"消失"了
        if silent and _tray_icon is None:
            try:
                window.show()
            except Exception:
                pass
    try:
        webview.start(debug=False)
    finally:
        shutdown_all()
        os._exit(0)

if __name__ == '__main__':
    try:
        _run()
    except Exception as e:
        _report_fatal(e)
    os._exit(0)
