"""系统集成。

扫码配对（二维码 + adb pair）、启动自检与致命错误上报、诊断报告与日志导出、
托盘图标与开机自启，以及主窗口引用的持有者。
依赖：storage、device、discover、apps。
"""


import ctypes
import io
import locale
import os
import re
import subprocess
import sys
import threading
import time

from .storage import (
    APP_VERSION, ADB_PATH, DATA_DIR, ERROR_LOG_STREAM, EXE_PATH,
    LAUNCH_LOG_STREAM, RES_DIR, SCAN_LOG_STREAM,
    load_config, save_config, storage_location, storage_read, _write_text,
)
from .device import (
    CLASSIC_ADB_PORT, SCRCPY_PATH, adb_server_ours, device_info, get_devices,
    get_startupinfo, launch_app, launch_desktop, run_adb, shutdown_all,
    _device_model, _remember_device, _serial_args, _skip_reconnect,
)
from .discover import mdns_available, mdns_services
from .apps import APPS_SCAN_TIMEOUT, _app_name

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
    add("adb 服务是本次启动的", str(adb_server_ours()))
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
    _tray_stop()
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


# ---------- 供入口 / HTTP 层访问的接口 ----------
# 主窗口引用与托盘状态是跨模块共享的可变状态：赋值发生在运行期（入口创建窗口、
# 托盘启停），所以用函数取实时值，不能用 from ... import 取快照。
def set_window(window):
    global _window
    _window = window

def get_window():
    return _window

def tray_ready():
    return _tray_icon is not None

def tray_exiting():
    return _tray_exiting

def tray_stop():
    _tray_stop()
